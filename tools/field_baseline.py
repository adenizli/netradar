"""Collect a 10–30 minute live NetRadar baseline with one RTL-SDR receiver."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

from tools.field_report import load_site, write_reports

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 2_000_000
FREQUENCY = 1_090_000_000
SNIPPET_PERIOD = 60
SNIPPET_LENGTH = 2
SNAPSHOT_INTERVAL = 5


def read_system_file(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip("\0\n ")
    except OSError:
        return None


def temperature():
    value = read_system_file("/sys/class/thermal/thermal_zone0/temp")
    return int(value) / 1000 if value and value.isdigit() else None


def receiver_details():
    usb_root = Path("/sys/bus/usb/devices")
    devices = []
    for device in sorted(usb_root.glob("*/idVendor")):
        if read_system_file(device) == "0bda" and read_system_file(device.parent / "idProduct") in ("2832", "2838"):
            devices.append({
                "usb_product": read_system_file(device.parent / "product"),
                "tuner_serial": read_system_file(device.parent / "serial"),
            })
    return devices[0] if len(devices) == 1 else {}


def revision(explicit):
    if explicit:
        return explicit
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def capture_snapshot(metrics_path, backend_url):
    sample = {"observed_at": datetime.now(timezone.utc).isoformat(), "cpu_temperature_c": temperature()}
    if metrics_path.is_file():
        sample["metrics"] = json.loads(metrics_path.read_text(encoding="utf-8"))
    try:
        with urlopen(backend_url.rstrip("/") + "/api/aircraft", timeout=2) as response:
            devices = json.load(response)
        # Store only data needed for capture matching and coverage, avoiding repeatedly
        # recording the entire backend snapshot and decoded message payloads.
        sample["aircraft"] = [
            {
                "icao": item["icao"],
                "capture_ids": sorted({
                    event["metadata"]["capture_id"]
                    for event in item["latest_events"].values()
                    if event["metadata"].get("source") == "rf"
                    and event["metadata"].get("df") == 17
                    and event["metadata"].get("crc_valid") is True
                    and event["metadata"].get("capture_id")
                }),
                "position_event": item["position_event"],
                "position_received_at_unix_ms": item["position_received_at_unix_ms"],
                "position_age_ms": item["position_age_ms"],
            }
            for item in devices
        ]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        sample["backend_error"] = str(exc)
    return sample


def record(args, run, metadata):
    snippets = run / "snippets"
    snippets.mkdir()
    env = dict(os.environ, NETRADAR_FIELD_TAP=json.dumps({
        "directory": str(snippets), "executable": args.rtl_sdr_command,
        "capture_id": metadata["capture_id"], "rate": SAMPLE_RATE, "frequency": FREQUENCY,
        "period": SNIPPET_PERIOD, "length": SNIPPET_LENGTH, "slots": args.minutes,
    }))
    metrics = run / "metrics-latest.json"
    cmd = [sys.executable, "-m", "netradar.pipeline", "--rtl-sdr",
           "--max-samples", str(SAMPLE_RATE * 60 * args.minutes),
           "--capture-id", metadata["capture_id"], "--metrics-json", str(metrics),
           "--metrics-interval-seconds", str(SNAPSHOT_INTERVAL),
           "--rtl-sdr-command", str(ROOT / "tools/rtl_field_tap.py"),
           "--gain-db", args.gain_db, "--ppm", str(args.ppm), "--device-index", str(args.device_index),
           "--backend-url", args.backend_url]
    with (run / "pipeline.log").open("wb") as log, (run / "snapshots.jsonl").open("w", encoding="utf-8") as snapshots:
        pipeline = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        start = time.monotonic()
        try:
            for slot in range(args.minutes * 60 // SNAPSHOT_INTERVAL + 1):
                print(json.dumps(capture_snapshot(metrics, args.backend_url)), file=snapshots, flush=True)
                remaining = start + (slot + 1) * SNAPSHOT_INTERVAL - time.monotonic()
                if remaining > 0:
                    try:
                        pipeline.wait(timeout=remaining)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            # A device running behind its sample clock still gets polled. Bound the
            # extension and label an overlong or interrupted collection incomplete.
            deadline = start + args.minutes * 60 + 30
            while pipeline.poll() is None and time.monotonic() < deadline:
                print(json.dumps(capture_snapshot(metrics, args.backend_url)), file=snapshots, flush=True)
                try:
                    pipeline.wait(timeout=SNAPSHOT_INTERVAL)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if pipeline.poll() is None:
                pipeline.send_signal(signal.SIGINT)
                try:
                    pipeline.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(pipeline.pid, signal.SIGKILL)
                    pipeline.wait()
            metadata["window"]["elapsed_wall_seconds"] = time.monotonic() - start
            metadata["window"]["ended_at"] = datetime.now().astimezone().isoformat()
            metadata["pipeline_returncode"] = pipeline.returncode
            print(json.dumps(capture_snapshot(metrics, args.backend_url)), file=snapshots, flush=True)
            diagnostics = read_system_file(run / "rtl-sdr.log")
            if diagnostics:
                model = re.search(r"^Found (.+) tuner$", diagnostics, re.MULTILINE)
                gain = re.search(r"^Tuner gain set to ([+-]?[\d.]+) dB\.$", diagnostics, re.MULTILINE)
                if model and metadata["receiver"]["tuner_model"] is None:
                    metadata["receiver"]["tuner_model"] = model.group(1)
                if gain:
                    metadata["receiver"]["reported_gain_db"] = float(gain.group(1))


def gain(value):
    if value == "auto":
        return value
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("gain must be a finite number or auto") from None
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("gain must be a finite number or auto")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10, choices=range(10, 31), metavar="10..30")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".netradar-run/field")
    parser.add_argument("--site", type=Path, help="Private site metadata JSON, see docs/FIELD_BASELINE.md")
    parser.add_argument("--gain-db", type=gain, default="35")
    parser.add_argument("--ppm", type=int, default=0)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--tuner-serial", help="Override USB serial auto-discovery")
    parser.add_argument("--tuner-model", help="Optional verified tuner model if known")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rtl-sdr-command", default="rtl_sdr")
    parser.add_argument("--code-commit", help="Recorded Git revision when running an exported checkout")
    args = parser.parse_args()
    if args.device_index < 0:
        parser.error("--device-index must be nonnegative")
    executable = shutil.which(args.rtl_sdr_command)
    if executable is None:
        parser.error(f"RTL-SDR command unavailable: {args.rtl_sdr_command}")
    args.rtl_sdr_command = str(Path(executable).resolve())
    try:
        site = load_site(args.site)
        commit = revision(args.code_commit)
        with urlopen(args.backend_url.rstrip("/") + "/healthz", timeout=3) as response:
            if json.load(response).get("status") != "ok":
                raise ValueError("Backend health check returned a status other than ok")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))

    started = datetime.now().astimezone()
    capture_id = started.astimezone(timezone.utc).strftime("rf-%Y%m%dT%H%M%S%fZ")
    run = (args.output_dir / capture_id).resolve()
    run.mkdir(mode=0o700, parents=True)
    device = receiver_details()
    metadata = {
        "capture_id": capture_id, "source": "rf", "code_commit": commit,
        "window": {"started_at": started.isoformat(), "requested_seconds": args.minutes * 60},
        "receiver": {
            "sample_rate_hz": SAMPLE_RATE, "center_frequency_hz": FREQUENCY,
            "requested_gain_db": float(args.gain_db) if args.gain_db != "auto" else "auto",
            "reported_gain_db": None,
            "ppm": args.ppm, "device_index": args.device_index,
            "usb_product": device.get("usb_product") if args.device_index == 0 else None,
            "tuner_serial": args.tuner_serial or (device.get("tuner_serial") if args.device_index == 0 else None),
            "tuner_model": args.tuner_model,
            "pi_model": read_system_file("/proc/device-tree/model"),
        },
        "capture": {
            "snippet_period_seconds": SNIPPET_PERIOD, "snippet_length_seconds": SNIPPET_LENGTH,
            "snippet_limit": args.minutes, "poll_interval_seconds": SNAPSHOT_INTERVAL,
        },
        "site": site,
    }
    print(f"Collecting {args.minutes} minutes of real RF in {run}", flush=True)
    interrupted = False
    try:
        record(args, run, metadata)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        (run / "run.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report = write_reports(run)
    print(f"Wrote {run / 'report.json'} and {run / 'report.md'}", flush=True)
    raise SystemExit(130 if interrupted else (0 if report["status"] == "completed" else 1))


if __name__ == "__main__":
    main()
