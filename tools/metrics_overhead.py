#!/usr/bin/env python3
"""Measure paired CPU and RSS readings for the actual producer with metrics off/on."""
import argparse
from contextlib import redirect_stdout
import json
from math import comb
from pathlib import Path
import re
import resource
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE_HZ = 2_000_000
TARGET_PERCENT = 5.0
_RTL_STATS = re.compile(r"^RTL-SDR: received=(\d+) emitted=(\d+) dropped=(\d+)", re.M)


class _EventSink:
    """Count producer JSONL lines while avoiding terminal and disk I/O per event."""

    def __init__(self):
        self.events = 0

    def write(self, data):
        self.events += data.count("\n")
        return len(data)

    def flush(self):
        pass


def _worker(options: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    from netradar import pipeline
    from netradar.dsp import detector  # noqa: F401 (preload NumPy and the CU8 power lookup)

    sys.argv = ["netradar.pipeline", *options]
    sink = _EventSink()
    began, cpu_started = time.monotonic(), time.process_time()
    with redirect_stdout(sink):
        pipeline.main()
    cpu = time.process_time() - cpu_started
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({
        "cpu_seconds": cpu,
        "elapsed_seconds": time.monotonic() - began,
        "peak_rss_bytes": peak_rss * (1 if sys.platform == "darwin" else 1024),
        "decoded_events": sink.events,
    }))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def _run(options: list[str], *, metrics_path: Path | None, input_samples: int) -> dict:
    if metrics_path is not None:
        options = [
            *options, "--metrics-json", str(metrics_path), "--metrics-interval-seconds", "1",
        ]
    worker = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", *options],
        cwd=ROOT, capture_output=True, text=True,
    )
    if worker.returncode != 0:
        raise RuntimeError(f"Producer exited with code {worker.returncode}\n{worker.stderr.strip()}")
    result = json.loads(worker.stdout)
    stats = _RTL_STATS.search(worker.stderr)
    if stats is None and "--rtl-sdr" in options:
        raise RuntimeError("Live producer did not report acquisition statistics")
    received, emitted, dropped = map(int, stats.groups()) if stats else (input_samples, input_samples, 0)
    if received <= 0:
        raise RuntimeError("Benchmark producer received no IQ samples")
    result.update(
        enabled=metrics_path is not None, received_samples=received, emitted_samples=emitted,
        queue_dropped_samples=dropped,
        cpu_seconds_per_million_samples=result["cpu_seconds"] * 1_000_000 / received,
    )
    if metrics_path is not None:
        report = json.loads(metrics_path.read_text())
        result["capture_id"] = report["capture_id"]
        result["source"] = report["source"]
        result["counters"] = {name: stage["counters"] for name, stage in report["stages"].items()}
        if report["run_status"] != "completed":
            raise RuntimeError("The metrics capture did not complete")
    return result


def _median_interval(values: list[float]) -> list[float] | None:
    """Smallest order-statistic interval with at least 95% median coverage."""
    ordered = sorted(values)
    size = len(ordered)
    tail = 0
    interval = None
    for trim in range(size // 2):
        tail += comb(size, trim)
        if 2 * tail / 2**size > .05:
            break
        interval = [ordered[trim], ordered[-trim - 1]]
    return interval


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--rtl-sdr", action="store_true", help="Use the live SDR for each run")
    inputs.add_argument("--iq", type=Path, help="Replay the same CU8 file for each run")
    parser.add_argument("--seconds", type=_positive_int, default=60, help="Nominal seconds per live run")
    parser.add_argument("--pairs", type=_positive_int, default=3, help="Number of off/on capture pairs")
    parser.add_argument("--chunk-samples", type=_positive_int, default=8192)
    parser.add_argument("--queue-max-chunks", type=_positive_int, default=64)
    parser.add_argument("--gain-db", default="35")
    parser.add_argument("--ppm", type=int, default=0)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--rtl-sdr-command", default="rtl_sdr")
    parser.add_argument("--source", choices=("rf", "replay", "synthetic"))
    args = parser.parse_args()

    options = ["--chunk-samples", str(args.chunk_samples)]
    if args.source:
        options += ["--source", args.source]
    try:
        if args.rtl_sdr:
            input_samples = args.seconds * SAMPLE_RATE_HZ
            options += [
                "--rtl-sdr", "--max-samples", str(input_samples), "--gain-db", args.gain_db,
                "--ppm", str(args.ppm), "--device-index", str(args.device_index),
                "--queue-max-chunks", str(args.queue_max_chunks), "--rtl-sdr-command", args.rtl_sdr_command,
            ]
        else:
            path = args.iq.resolve()
            input_samples = path.stat().st_size // 2
            with path.open("rb") as capture:
                while capture.read(1024 * 1024):
                    pass  # Warm the file cache before measuring either mode.
            options += ["--iq", str(path)]

        pairs = []
        with TemporaryDirectory(prefix="netradar-overhead-") as directory:
            metrics_path = Path(directory) / "metrics.json"
            for index in range(args.pairs):
                order = (False, True) if index % 2 == 0 else (True, False)
                results = {
                    enabled: _run(options, metrics_path=metrics_path if enabled else None,
                                  input_samples=input_samples)
                    for enabled in order
                }
                before = results[False]["cpu_seconds_per_million_samples"]
                after = results[True]["cpu_seconds_per_million_samples"]
                pairs.append({
                    "order": ["enabled" if enabled else "disabled" for enabled in order],
                    "disabled": results[False], "enabled": results[True],
                    "relative_cpu_overhead_percent": 100 * (after / before - 1),
                })
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"metrics_overhead: {exc}\n")

    pair_overheads = [pair["relative_cpu_overhead_percent"] for pair in pairs]
    overhead = statistics.median(pair_overheads)
    print(json.dumps({
        "mode": "rtl_sdr" if args.rtl_sdr else "iq_replay",
        "pipeline_arguments": options,
        "enabled_metrics_report_interval_seconds": 1,
        "comparison": "median of paired relative process CPU seconds per million received samples",
        "measurement_scope": "producer CLI after pipeline, DSP and NumPy imports through final shutdown",
        "relative_cpu_overhead_percent": overhead,
        "median_relative_cpu_overhead_95_percent_interval": _median_interval(pair_overheads),
        "target_percent": TARGET_PERCENT,
        "target_met": overhead < TARGET_PERCENT,
        "pairs": pairs,
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        _worker(sys.argv[2:])
    else:
        main()
