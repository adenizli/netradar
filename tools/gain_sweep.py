#!/usr/bin/env python3
"""Compare RTL-SDR gain/PPM settings at one site with repeated, shuffled windows (issue #11).

Each window first records a short raw CU8 snippet for clipping and noise statistics,
then runs the production pipeline for a fixed number of samples with that setting.
Settings are rated by CRC-valid frames per minute, distinct and positioned ICAOs,
with zero IQ loss and tolerable clipping, never by raw preamble candidates.

Air traffic changes between windows, so every setting is measured in several rounds
in a shuffled order. A setting is recommended only when it is eligible and beats the
baseline in every round by a clear margin; otherwise the baseline is kept.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import iq_health  # noqa: E402

RATE = 2_000_000
FREQ = 1_090_000_000
SERVICE = "netradar.service"
# Default sweep targets in dB; each is snapped to the nearest gain the tuner supports.
DEFAULT_TARGETS = (22.0, 29.0, 35.0, 40.0, 45.0, 49.6)


def parse_supported_gains(text: str) -> list[float] | None:
    """Read 'Supported gain values (29): 0.0 0.9 ...' from rtl_test output."""
    for line in text.splitlines():
        if "Supported gain values" in line and ":" in line:
            try:
                return [float(value) for value in line.split(":", 1)[1].split()]
            except ValueError:
                return None
    return None


def supported_gains(rtl_test: str, device_index: int) -> list[float] | None:
    try:
        result = subprocess.run(
            [rtl_test, "-d", str(device_index), "-t"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_supported_gains(result.stdout + result.stderr)


def snap(targets, supported):
    """Nearest supported gain for each target, without duplicates, in target order."""
    chosen = []
    for target in targets:
        value = min(supported, key=lambda gain: (abs(gain - target), gain))
        if value not in chosen:
            chosen.append(value)
    return chosen


def gain_label(gain) -> str:
    return "auto" if gain == "auto" else f"{gain:g}"


def setting_key(setting) -> str:
    gain, ppm = setting
    return f"gain={gain_label(gain)} ppm={ppm}"


def schedule(settings, rounds: int, seed: int):
    """Each round measures every setting once, in its own shuffled order."""
    rng = random.Random(seed)
    order = []
    for round_index in range(rounds):
        block = list(settings)
        rng.shuffle(block)
        if order and len(block) > 1 and block[0] == order[-1][1]:
            block.append(block.pop(0))  # never measure one setting twice in a row
        order.extend((round_index, setting) for setting in block)
    return order


def window_score(metrics: dict, events: list[dict], snippet: dict | None) -> dict:
    stages = metrics["stages"]
    samples = stages["acquisition"]["counters"]["complex_samples"]
    decoder = stages["decoder"]["counters"]
    minutes = samples / RATE / 60 if samples else 0.0
    valid = decoder["crc_clean"] + decoder["crc_corrected_one_bit"]
    icaos, positioned = set(), set()
    for update in events:
        data = update.get("data") or {}
        if not data.get("icao"):
            continue
        icaos.add(data["icao"])
        if data.get("latitude") is not None and data.get("longitude") is not None:
            positioned.add(data["icao"])
    dropped = (stages["acquisition"]["counters"]["queue_dropped_samples"]
               + stages["dsp"]["counters"]["gap_samples"])
    return {
        "samples": samples,
        "minutes": minutes,
        "valid_frames": valid,
        "valid_per_minute": valid / minutes if minutes else 0.0,
        "crc_rejected": decoder["crc_rejected"],
        "preamble_candidates": stages["dsp"]["counters"]["preamble_candidates"],
        "distinct_icao": len(icaos),
        "positioned_icao": len(positioned),
        "dropped_samples": dropped,
        "clip_pct": None if snippet is None else snippet["clip_pct"],
        "peak_to_noise_db": None if snippet is None else snippet["peak_to_noise_db"],
    }


def summarize(windows: list[dict], baseline_key: str, *, max_clip_pct: float,
              min_improvement_pct: float) -> dict:
    """Per-setting medians and a conservative recommendation."""
    by_setting: dict[str, list[dict]] = {}
    for window in windows:
        if window.get("score") is not None:
            by_setting.setdefault(window["setting"], []).append(window)
    rows = {}
    for key, items in by_setting.items():
        scores = [item["score"] for item in items]
        clips = [s["clip_pct"] for s in scores if s["clip_pct"] is not None]
        rows[key] = {
            "windows": len(items),
            "median_valid_per_minute": statistics.median(s["valid_per_minute"] for s in scores),
            "median_distinct_icao": statistics.median(s["distinct_icao"] for s in scores),
            "median_positioned_icao": statistics.median(s["positioned_icao"] for s in scores),
            "max_dropped_samples": max(s["dropped_samples"] for s in scores),
            "max_clip_pct": max(clips) if clips else None,
            "per_round_valid_per_minute": {
                str(item["round"]): item["score"]["valid_per_minute"] for item in items
            },
        }
        reasons = []
        if rows[key]["max_dropped_samples"] > 0:
            reasons.append("IQ samples were dropped")
        if rows[key]["max_clip_pct"] is None:
            reasons.append("no clipping measurement")
        elif rows[key]["max_clip_pct"] > max_clip_pct:
            reasons.append(f"clipping above {max_clip_pct:g}%")
        rows[key]["eligible"] = not reasons
        rows[key]["ineligible_reasons"] = reasons

    decision = {"recommended": baseline_key, "baseline": baseline_key, "changed": False}
    base = rows.get(baseline_key)
    eligible = {k: r for k, r in rows.items() if r["eligible"]}
    if not eligible:
        decision["reason"] = "No setting had zero IQ loss and tolerable clipping; keep the baseline and fix that first."
        return {"settings": rows, "decision": decision}
    best_key = max(eligible, key=lambda k: eligible[k]["median_valid_per_minute"])
    if base is None or not base["eligible"]:
        decision.update(recommended=best_key, changed=best_key != baseline_key,
                        reason="The baseline was not eligible; this is the best eligible setting. Repeat the sweep to confirm.")
        return {"settings": rows, "decision": decision}
    if best_key == baseline_key:
        decision["reason"] = "The baseline had the most CRC-valid frames per minute."
        return {"settings": rows, "decision": decision}
    best = eligible[best_key]
    paired = [
        (best["per_round_valid_per_minute"][r], base["per_round_valid_per_minute"][r])
        for r in best["per_round_valid_per_minute"] if r in base["per_round_valid_per_minute"]
    ]
    floor = 1 + min_improvement_pct / 100
    wins_every_round = len(paired) >= 2 and all(b > a * floor for b, a in paired)
    if wins_every_round:
        decision.update(recommended=best_key, changed=True, reason=(
            f"Beat the baseline by more than {min_improvement_pct:g}% in all {len(paired)} paired rounds."))
    else:
        decision["reason"] = (
            f"{best_key} had a higher median, but did not beat the baseline by more than "
            f"{min_improvement_pct:g}% in every paired round (needs at least 2 rounds). "
            "The difference may be air traffic; keep the baseline.")
    decision["best_median"] = best_key
    return {"settings": rows, "decision": decision}


def markdown(report: dict) -> str:
    lines = [
        "# NetRadar gain/PPM sweep", "",
        f"- Started: {report['started_utc']}  ·  finished: {report['finished_utc']}",
        f"- Window: {report['window_seconds']:g} s per setting, {report['rounds']} rounds, seed {report['seed']}",
        f"- Source: `{report['source']}`  ·  device index {report['device_index']}  ·  code `{report['code_commit']}`",
        f"- Eligibility: zero dropped IQ samples and clipping ≤ {report['max_clip_pct']:g}%", "",
        "| Setting | Windows | Valid frames/min (median) | Distinct ICAO | Positioned ICAO | Max dropped | Max clip % | Eligible |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for key, row in sorted(report["summary"]["settings"].items(),
                           key=lambda item: -item[1]["median_valid_per_minute"]):
        clip = "—" if row["max_clip_pct"] is None else f"{row['max_clip_pct']:.3f}"
        eligible = "yes" if row["eligible"] else "no: " + "; ".join(row["ineligible_reasons"])
        lines.append(
            f"| {key} | {row['windows']} | {row['median_valid_per_minute']:.1f} | "
            f"{row['median_distinct_icao']:g} | {row['median_positioned_icao']:g} | "
            f"{row['max_dropped_samples']} | {clip} | {eligible} |")
    decision = report["summary"]["decision"]
    lines += ["", "## Decision", "",
              f"**Use {decision['recommended']}**" + (" (changed from the baseline)" if decision["changed"] else " (baseline kept)"),
              "", decision["reason"], "", "## Windows in measurement order", "",
              "| # | Round | Setting | UTC start | Valid frames/min | Distinct ICAO | Dropped | Clip % | Error |",
              "|---:|---:|---|---|---:|---:|---:|---:|---|"]
    for index, window in enumerate(report["windows"], 1):
        score = window.get("score") or {}
        clip = score.get("clip_pct")
        lines.append(
            f"| {index} | {window['round'] + 1} | {window['setting']} | {window['started_utc']} | "
            f"{score.get('valid_per_minute', 0):.1f} | {score.get('distinct_icao', '—')} | "
            f"{score.get('dropped_samples', '—')} | {'—' if clip is None else f'{clip:.3f}'} | "
            f"{window.get('error') or ''} |")
    lines += ["", "Air traffic differs between windows. Compare settings only within this report,",
              "and repeat the sweep at another time of day before changing hardware.", ""]
    return "\n".join(lines)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_window(args, directory: Path, index: int, round_index: int, setting) -> dict:
    gain, ppm = setting
    window_dir = directory / f"{index:02d}-{gain_label(gain)}-ppm{ppm}"
    window_dir.mkdir()
    record = {"round": round_index, "setting": setting_key(setting), "gain": gain_label(gain),
              "ppm": ppm, "started_utc": utc_now(), "directory": window_dir.name}
    snippet_stats = None
    if args.snippet_seconds > 0:
        snippet = window_dir / "snippet.cu8"
        command = [args.rtl_sdr_command, "-d", str(args.device_index), "-f", str(FREQ),
                   "-s", str(RATE), "-g", "0" if gain == "auto" else f"{gain:g}",
                   "-p", str(ppm), "-n", str(int(RATE * args.snippet_seconds)), str(snippet)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and snippet.exists() and snippet.stat().st_size:
            snippet.with_suffix(".json").write_text(json.dumps({
                "kind": "real_receiver_capture" if args.source == "rf" else "synthetic_test_capture",
                "file": snippet.name, "center_frequency_hz": FREQ, "sample_rate_hz": RATE,
                "sample_format": "cu8_interleaved_iq", "requested_gain": gain_label(gain), "ppm": ppm,
            }, indent=2) + "\n")
            snippet_stats = iq_health.analyze(snippet)[3]
            if not args.keep_snippets:
                snippet.unlink()
        else:
            record["snippet_error"] = (result.stderr.strip().splitlines() or ["rtl_sdr failed"])[-1]
    metrics_path = window_dir / "metrics.json"
    command = [sys.executable, "-m", "netradar.pipeline", "--rtl-sdr",
               "--rtl-sdr-command", args.rtl_sdr_command, "--device-index", str(args.device_index),
               "--gain-db", gain_label(gain), "--ppm", str(ppm), "--source", args.source,
               "--max-samples", str(int(RATE * args.window_seconds)),
               "--metrics-json", str(metrics_path)]
    with (window_dir / "events.jsonl").open("w") as events_out, \
            (window_dir / "pipeline.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=events_out, stderr=log)
    record["finished_utc"] = utc_now()
    if result.returncode != 0 or not metrics_path.exists():
        tail = (window_dir / "pipeline.log").read_text().strip().splitlines()
        record["error"] = tail[-1] if tail else f"pipeline exited {result.returncode}"
        record["score"] = None
        return record
    events = [json.loads(line) for line in (window_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    record["score"] = window_score(json.loads(metrics_path.read_text()), events, snippet_stats)
    record["snippet"] = snippet_stats
    return record


def service_active() -> bool:
    if shutil.which("systemctl") is None:
        return False
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode == 0


def code_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def parse_gain(text: str):
    if text == "auto":
        return "auto"
    value = float(text)
    if not 0 <= value <= 60:
        raise argparse.ArgumentTypeError("gain must be auto or 0..60 dB")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gains", nargs="+", type=parse_gain,
                        help="Gains to compare (dB or 'auto'). Default: auto + supported gains near "
                             + ", ".join(f"{t:g}" for t in DEFAULT_TARGETS))
    parser.add_argument("--ppm", nargs="+", type=int, default=[0], help="PPM corrections to compare")
    parser.add_argument("--baseline-gain", type=parse_gain, default=35.0,
                        help="Current setting; snapped to the nearest compared gain (default 35)")
    parser.add_argument("--baseline-ppm", type=int, default=0)
    parser.add_argument("--window-seconds", type=float, default=180.0)
    parser.add_argument("--snippet-seconds", type=float, default=1.0,
                        help="Raw CU8 recorded before each window for clipping/noise (0 disables)")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=None, help="Shuffle seed (default: from the clock)")
    parser.add_argument("--max-clip-pct", type=float, default=1.0)
    parser.add_argument("--min-improvement-pct", type=float, default=10.0)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--rtl-sdr-command", default="rtl_sdr")
    parser.add_argument("--rtl-test-command", default="rtl_test")
    parser.add_argument("--source", choices=("rf", "synthetic"), default="rf",
                        help="Use synthetic only with a fake rtl_sdr in tests")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".netradar-run" / "gain-sweep")
    parser.add_argument("--keep-snippets", action="store_true")
    parser.add_argument("--stop-service", action="store_true",
                        help=f"Stop {SERVICE} for the sweep and start it again afterwards")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule and exit")
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.window_seconds <= 0 or args.snippet_seconds < 0:
        parser.error("rounds must be ≥ 1, window-seconds > 0 and snippet-seconds ≥ 0")

    supported = None
    if args.gains is None:
        supported = supported_gains(args.rtl_test_command, args.device_index)
        if not supported:
            parser.error("Could not read supported gains from rtl_test; pass --gains explicitly")
        args.gains = ["auto", *snap(DEFAULT_TARGETS, supported)]
    numeric = [g for g in args.gains if g != "auto"]
    baseline_gain = args.baseline_gain
    if baseline_gain != "auto" and numeric:
        baseline_gain = min(numeric, key=lambda g: (abs(g - baseline_gain), g))
    settings = [(gain, ppm) for gain in args.gains for ppm in args.ppm]
    baseline = (baseline_gain, args.baseline_ppm)
    if baseline not in settings:
        settings.append(baseline)
    seed = args.seed if args.seed is not None else int(time.time())
    order = schedule(settings, args.rounds, seed)
    per_window = args.window_seconds + args.snippet_seconds + 5
    print(f"[gain-sweep] {len(settings)} settings × {args.rounds} rounds = {len(order)} windows, "
          f"about {len(order) * per_window / 60:.0f} min. Baseline: {setting_key(baseline)}", file=sys.stderr)
    if args.dry_run:
        for index, (round_index, setting) in enumerate(order, 1):
            print(f"{index:2d}. round {round_index + 1}: {setting_key(setting)}")
        return 0

    stopped_service = False
    if service_active():
        if not args.stop_service:
            print(f"[gain-sweep] {SERVICE} is using the dongle. Re-run with --stop-service "
                  "(it is started again at the end).", file=sys.stderr)
            return 2
        subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)
        stopped_service = True
    started = utc_now()
    directory = args.output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=False)
    windows = []
    try:
        for index, (round_index, setting) in enumerate(order, 1):
            print(f"[gain-sweep] {index}/{len(order)} round {round_index + 1}: {setting_key(setting)}",
                  file=sys.stderr)
            windows.append(run_window(args, directory, index, round_index, setting))
            (directory / "windows.json").write_text(json.dumps(windows, indent=2) + "\n")
    finally:
        if stopped_service:
            subprocess.run(["systemctl", "--user", "start", SERVICE])
    report = {
        "schema_version": "1.0", "kind": "gain_ppm_sweep", "source": args.source,
        "started_utc": started, "finished_utc": utc_now(), "code_commit": code_commit(),
        "device_index": args.device_index, "supported_gains": supported,
        "window_seconds": args.window_seconds, "snippet_seconds": args.snippet_seconds,
        "rounds": args.rounds, "seed": seed, "max_clip_pct": args.max_clip_pct,
        "min_improvement_pct": args.min_improvement_pct, "windows": windows,
        "summary": summarize(windows, setting_key(baseline), max_clip_pct=args.max_clip_pct,
                             min_improvement_pct=args.min_improvement_pct),
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "report.md").write_text(markdown(report))
    decision = report["summary"]["decision"]
    print(f"[gain-sweep] Report: {directory / 'report.md'}", file=sys.stderr)
    print(f"[gain-sweep] Decision: use {decision['recommended']}. {decision['reason']}", file=sys.stderr)
    return 0 if all(window.get("score") for window in windows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
