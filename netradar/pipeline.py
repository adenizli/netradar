"""Live RTL-SDR, saved IQ or frames -> DSP/decoder -> JSONL or FastAPI delivery."""
import argparse
from collections.abc import Callable
import json
import math
from pathlib import Path
import sys

from netradar.backend.state import AircraftStore
from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder


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
    args = parser.parse_args()
    if args.capture_start_unix_ms is not None and not (args.iq or args.rtl_sdr):
        parser.error("--capture-start-unix-ms applies only to --iq or --rtl-sdr")
    if args.max_samples is not None and not args.rtl_sdr:
        parser.error("--max-samples applies only to --rtl-sdr")

    frames = chunks = stream = stats = None
    try:
        if args.iq is not None or args.rtl_sdr:
            try:
                from netradar.acquisition.receiver import StreamStats, replay_cu8, stream_rtlsdr
                from netradar.dsp.detector import detect_frames
            except ModuleNotFoundError as exc:
                if exc.name != "numpy":
                    raise
                raise ValueError("IQ detection needs NumPy: install requirements-dsp.txt") from None
            if args.rtl_sdr:
                sample_rate = args.sample_rate_hz if args.sample_rate_hz is not None else 2_000_000
                frequency = args.center_frequency_hz if args.center_frequency_hz is not None else 1_090_000_000
                if sample_rate != 2_000_000 or frequency != 1_090_000_000:
                    raise ValueError("DSP requires 2,000,000 S/s at 1090 MHz; device was not started")
                stats = StreamStats()
                chunks = stream_rtlsdr(
                    sample_rate_hz=sample_rate, center_frequency_hz=frequency,
                    chunk_samples=args.chunk_samples, gain_db=args.gain_db,
                    ppm=args.ppm, device_index=args.device_index,
                    max_samples=args.max_samples, queue_max_chunks=args.queue_max_chunks,
                    rtl_sdr_cmd=args.rtl_sdr_command, stats=stats,
                )
                source = args.source or "rf"
            else:
                chunks = replay_cu8(
                    args.iq, chunk_samples=args.chunk_samples, sample_rate_hz=args.sample_rate_hz,
                    center_frequency_hz=args.center_frequency_hz, strict=True,
                )
                first = next(chunks, None)
                sample_rate = first.sample_rate_hz if first is not None else (args.sample_rate_hz or 2_000_000)
                source = args.source or "replay"
                chunks = _prepend(first, chunks)
            frames = detect_frames(chunks, source=source,
                                   capture_start_unix_ms=args.capture_start_unix_ms)
        elif args.fixture is not None:
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
            frames = (ModeSFrame(**json.loads(line)) for line in stream if line.strip())

        def publish(update: dict) -> None:
            if args.backend_url:
                from netradar.backend.forward import send_event
                send_event(args.backend_url, DecodedEvent.from_dict(update["event"]))
            else:
                print(json.dumps(update, ensure_ascii=False, allow_nan=False), flush=True)

        pipeline = DecoderPipeline(decoder=ADSBDecoder(sample_rate_hz=sample_rate), publish=publish)
        for frame in frames:
            pipeline.handle_frame(frame)
        if args.rtl_sdr and args.max_samples is None:
            raise RuntimeError("Live RTL-SDR stream ended; check the receiver before restarting")
    except KeyboardInterrupt:
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


if __name__ == "__main__":
    main()
