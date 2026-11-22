"""Live RTL-SDR, saved IQ or frames -> DSP/decoder -> JSONL or FastAPI delivery."""
import argparse
from collections.abc import Callable
import json
import logging
import math
from pathlib import Path
import signal
import sys
from tempfile import NamedTemporaryFile
from threading import Event, Thread

from netradar.backend.state import AircraftStore
from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder
from netradar.metrics import FunnelMetrics


log = logging.getLogger(__name__)


class _MetricsFile:
    def __init__(self, path: Path, metrics: FunnelMetrics, interval: float | None):
        self.path = path
        self.metrics = metrics
        self.interval = interval
        self._stop = Event()
        self._thread: Thread | None = None

    def _write(self) -> None:
        pending = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=".netradar-metrics-", delete=False) as file:
                pending = Path(file.name)
                file.write(json.dumps(self.metrics.snapshot(), allow_nan=False, separators=(",", ":")) + "\n")
            pending.replace(self.path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _report_periodically(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except OSError as exc:
                log.warning("Could not write periodic metrics to %s: %s", self.path, exc)
                return

    def start(self) -> None:
        self._write()
        if self.interval is not None:
            thread = Thread(target=self._report_periodically, name="netradar-metrics-file", daemon=True)
            thread.start()
            self._thread = thread

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._write()


class DecoderPipeline:
    def __init__(
        self, *, decoder: ADSBDecoder | None = None,
        store: AircraftStore | None = None, publish: Callable[[dict], None] | None = None,
    ):
        self.decoder = decoder if decoder is not None else ADSBDecoder()
        self.store = store if store is not None else AircraftStore()
        self.publish = publish

    def handle_frame(self, frame: ModeSFrame) -> list[dict]:
        """Deliver every decoded event to the backend and then the publisher."""
        updates = []
        for event in self.decoder.decode(frame):
            update = self.store.apply(event)
            if self.publish is not None:
                self.publish(update)
            updates.append(update)
        return updates


def _prepend(first, chunks):
    try:
        if first is not None:
            yield first
        yield from chunks
    finally:
        chunks.close()


def _gain(value):
    if value.lower() == "auto":
        return None
    try:
        gain = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("gain must be a number or auto") from None
    if not math.isfinite(gain):
        raise argparse.ArgumentTypeError("gain must be finite")
    return gain


def _positive_count(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be positive")
    return count


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a positive number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("interval must be a positive, finite number of seconds")
    return seconds


def _read_frames(stream, source_if_missing: str | None):
    for line in stream:
        if line.strip():
            payload = json.loads(line)
            if source_if_missing is not None and isinstance(payload, dict):
                payload.setdefault("source", source_if_missing)
            yield ModeSFrame(**payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--fixture", type=Path, help="Synthetic expected-frame metadata; bypass IQ detection")
    inputs.add_argument("--input", type=Path, help="Frame JSONL file; defaults to stdin")
    inputs.add_argument("--iq", type=Path, help="Detect DF17 from CU8 bytes, honoring sidecar rate/frequency")
    inputs.add_argument("--rtl-sdr", action="store_true", help="Read live CU8 from the rtl_sdr executable")
    parser.add_argument("--sample-rate-hz", type=int, default=None)
    parser.add_argument("--center-frequency-hz", type=int, default=None)
    parser.add_argument("--chunk-samples", type=int, default=8192)
    parser.add_argument("--source", choices=("rf", "replay", "synthetic"), default=None)
    parser.add_argument("--capture-start-unix-ms", type=int, help="Measured time of IQ sample zero, only if known")
    parser.add_argument("--gain-db", type=_gain, default=35.0, help="RTL gain in dB, or auto")
    parser.add_argument("--ppm", type=int, default=0)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--max-samples", type=int, help="Stop live acquisition after N complex samples")
    parser.add_argument("--queue-max-chunks", type=int, default=64)
    parser.add_argument("--rtl-sdr-command", default="rtl_sdr", help="RTL executable path")
    parser.add_argument("--backend-url", help="Send full decoded events directly to FastAPI instead of stdout")
    parser.add_argument("--delivery-max-events", type=_positive_count, default=256,
                        help="Maximum pending events for live HTTP delivery")
    parser.add_argument("--delivery-request-timeout-seconds", type=_positive_seconds, default=5.0)
    parser.add_argument("--delivery-drain-timeout-seconds", type=_positive_seconds, default=10.0)
    parser.add_argument("--capture-id", help="Capture ID shared by events and metrics (default: generated ID)")
    parser.add_argument("--metrics-json", type=Path, help="Write a loss-funnel JSON report to this file")
    parser.add_argument("--metrics-interval-seconds", type=_positive_seconds,
                        help="Refresh the report at this interval while running")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Send producer heartbeat and loss-funnel metrics to the backend")
    args = parser.parse_args()
    if args.capture_start_unix_ms is not None and not (args.iq or args.rtl_sdr):
        parser.error("--capture-start-unix-ms applies only to --iq or --rtl-sdr")
    if args.max_samples is not None and not args.rtl_sdr:
        parser.error("--max-samples applies only to --rtl-sdr")

    if args.diagnostics and not args.backend_url:
        parser.error("--diagnostics requires --backend-url")
    if args.metrics_interval_seconds is not None and args.metrics_json is None:
        parser.error("--metrics-interval-seconds requires --metrics-json")
    if args.metrics_json is not None:
        input_paths = [path for path in (args.iq, args.fixture, args.input) if path is not None]
        if args.iq is not None:
            input_paths.append(args.iq.with_suffix(".json"))
        if any(args.metrics_json.resolve() == path.resolve() for path in input_paths):
            parser.error("--metrics-json must name a different file from the input or IQ sidecar")
    if args.iq is not None and args.source == "rf":
        parser.error("Saved IQ is replay or synthetic")
    if args.rtl_sdr and args.source == "replay":
        parser.error("Live RTL-SDR uses source rf or a synthetic receiver")
    if args.fixture is not None and args.source not in (None, "synthetic"):
        parser.error("--fixture has source synthetic")

    frames = chunks = stream = stats = None
    metrics = metrics_file = telemetry = delivery = None
    interrupted = False
    previous_sigint = None
    run_status = "failed"
    collect_metrics = args.metrics_json is not None or args.diagnostics

    def start_metrics(source: str) -> None:
        nonlocal metrics, metrics_file, telemetry
        if not collect_metrics:
            return
        metrics = FunnelMetrics(source, capture_id=args.capture_id)
        if args.metrics_json is not None:
            reporter = _MetricsFile(args.metrics_json, metrics, args.metrics_interval_seconds)
            reporter.start()
            metrics_file = reporter
        if args.diagnostics:
            from netradar.telemetry import TelemetrySender
            telemetry = TelemetrySender(args.backend_url, metrics)
            telemetry.start()

    def request_stop(signum, frame):
        nonlocal interrupted
        interrupted = True

    def check_consumer():
        if interrupted:
            raise KeyboardInterrupt
        if delivery is not None:
            delivery.check()

    try:
        if args.iq is not None or args.rtl_sdr:
            try:
                from netradar.acquisition.receiver import StreamStats, replay_cu8, replay_source, stream_rtlsdr
                from netradar.dsp.detector import detect_frames
            except ModuleNotFoundError as exc:
                if exc.name != "numpy":
                    raise
                raise ValueError("IQ detection needs NumPy: install requirements-dsp.txt") from None
            if args.rtl_sdr:
                source = args.source or "rf"
                start_metrics(source)
                sample_rate = args.sample_rate_hz if args.sample_rate_hz is not None else 2_000_000
                frequency = args.center_frequency_hz if args.center_frequency_hz is not None else 1_090_000_000
                if sample_rate != 2_000_000 or frequency != 1_090_000_000:
                    raise ValueError("DSP requires 2,000,000 S/s at 1090 MHz; device was not started")
                stats = StreamStats()
                if args.backend_url:
                    from netradar.backend.live_delivery import LiveDelivery
                    delivery = LiveDelivery(
                        args.backend_url, metrics, capacity=args.delivery_max_events,
                        request_timeout=args.delivery_request_timeout_seconds,
                        drain_timeout=args.delivery_drain_timeout_seconds,
                    )
                    previous_sigint = signal.signal(signal.SIGINT, request_stop)
                chunks = stream_rtlsdr(
                    sample_rate_hz=sample_rate, center_frequency_hz=frequency,
                    chunk_samples=args.chunk_samples, gain_db=args.gain_db,
                    ppm=args.ppm, device_index=args.device_index,
                    max_samples=args.max_samples, queue_max_chunks=args.queue_max_chunks,
                    rtl_sdr_cmd=args.rtl_sdr_command, stats=stats, metrics=metrics,
                    check_consumer=check_consumer if delivery is not None else None,
                )
            else:
                source = args.source or replay_source(args.iq)
                start_metrics(source)
                chunks = replay_cu8(
                    args.iq, chunk_samples=args.chunk_samples, sample_rate_hz=args.sample_rate_hz,
                    center_frequency_hz=args.center_frequency_hz, strict=True, metrics=metrics,
                )
                first = next(chunks, None)
                if metrics is not None:
                    sample_rate = metrics.sample_rate_hz
                else:
                    sample_rate = first.sample_rate_hz if first is not None else (args.sample_rate_hz or 2_000_000)
                chunks = _prepend(first, chunks)
            frames = detect_frames(chunks, source=source, metrics=metrics,
                                   capture_start_unix_ms=args.capture_start_unix_ms)
        elif args.fixture is not None:
            start_metrics("synthetic")
            metadata = json.loads(args.fixture.read_text())
            if metadata.get("kind") != "synthetic_baseband_iq":
                raise ValueError("--fixture expects explicitly synthetic fixture metadata")
            sample_rate = metadata["sample_rate_hz"]
            if args.sample_rate_hz is not None and args.sample_rate_hz != sample_rate:
                raise ValueError("Sample rate does not match fixture metadata")
            frames = (
                ModeSFrame(hex=item["hex"], sample_index=item["sample_index"], source="synthetic")
                for item in metadata["expected_frames"]
            )
        else:
            sample_rate = args.sample_rate_hz if args.sample_rate_hz is not None else 2_000_000
            stream = args.input.open() if args.input is not None else sys.stdin
            frames = _read_frames(stream, args.source)
            if args.source is not None:
                start_metrics(args.source)
            elif collect_metrics:
                first = next(frames, None)
                start_metrics(first.source if first is not None else "replay")
                frames = _prepend(first, frames)

        if metrics is not None and args.backend_url:
            metrics.observe("delivery")

        def publish(update: dict) -> None:
            if args.backend_url:
                from netradar.backend.forward import send_event
                event = DecodedEvent.from_dict(update["event"])
                if metrics is None:
                    send_event(args.backend_url, event)
                    return
                started = metrics.begin_latency("delivery")
                outcome = "rejected"
                try:
                    send_event(args.backend_url, event)
                    outcome = "accepted"
                finally:
                    metrics.observe("delivery", counters={outcome: 1},
                                    duration_ns=metrics.elapsed_latency(started))
            else:
                print(json.dumps(update, ensure_ascii=False, allow_nan=False), flush=True)

        decoder = ADSBDecoder(sample_rate_hz=sample_rate, capture_id=args.capture_id, metrics=metrics)
        if delivery is not None:
            for frame in frames:
                for event in decoder.decode(frame):
                    delivery.submit(event)
                delivery.check()
        else:
            pipeline = DecoderPipeline(decoder=decoder, publish=publish)
            for frame in frames:
                pipeline.handle_frame(frame)
        check_consumer()
        if args.rtl_sdr and args.max_samples is None:
            raise RuntimeError("Live RTL-SDR stream ended; check the receiver before restarting")
        run_status = "completed"
    except KeyboardInterrupt:
        run_status = "interrupted"
        parser.exit(130, "netradar.pipeline: stopped\n")
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        parser.exit(2, f"netradar.pipeline: {exc}\n")
    finally:
        for resource in (frames, chunks, stream):
            if resource is not None and resource is not sys.stdin:
                close = getattr(resource, "close", None)
                if close is not None:
                    close()
        if stats is not None and stats.pid is not None:
            print(f"RTL-SDR: received={stats.samples_received} emitted={stats.samples_emitted} "
                  f"dropped={stats.dropped_samples} exit={stats.returncode}", file=sys.stderr)
        delivery_error = None
        if delivery is not None:
            try:
                delivery.close()
            except RuntimeError as exc:
                delivery_error = exc
                run_status = "failed"
            else:
                print(f"Aircraft HTTP: {delivery.summary()}", file=sys.stderr)
            finally:
                if previous_sigint is not None:
                    signal.signal(signal.SIGINT, previous_sigint)
        if interrupted and run_status == "completed":
            run_status = "interrupted"
        if metrics is not None:
            metrics.finish(run_status)
        try:
            if metrics_file is not None:
                metrics_file.close()
        except OSError as exc:
            parser.exit(2, f"netradar.pipeline: could not write metrics: {exc}\n")
        finally:
            if telemetry is not None:
                telemetry.close()
        if delivery_error is not None:
            parser.exit(2, f"netradar.pipeline: {delivery_error}\n")
    if run_status == "interrupted":
        parser.exit(130, "netradar.pipeline: stopped\n")


if __name__ == "__main__":
    main()
