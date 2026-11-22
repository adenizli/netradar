#!/usr/bin/env python3
"""Compare two field baseline reports (A = before, B = after) for one controlled change.

Used for antenna/cable/placement A/B tests (issue #10) and to check that two baseline
windows are comparable (issue #8). The tool lists every condition that differs between
the runs, warns when the comparison is confounded (several factors changed, different
duration, different time of day, incomplete run, not real RF) and reports the measured
differences. One pair of windows is evidence, not proof: repeat A/B/A/B before buying
hardware.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import mean

# Conditions that define "the same experiment". Differences are listed as changed factors.
RECEIVER_FACTORS = ("requested_gain_db", "ppm", "device_index", "tuner_serial", "tuner_model", "pi_model")
SITE_FACTORS = (
    "location_description", "latitude_deg", "longitude_deg", "site_elevation_m", "antenna_model",
    "antenna_height_agl_m", "antenna_orientation", "placement", "cable_type", "cable_length_m",
    "cable_description", "connectors", "filter", "lna",
)
MAX_HOUR_GAP = 2  # hours of local time-of-day difference before warning about traffic confounding


def load(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema_version") != "1.0" or "provenance" not in report or "summary" not in report:
        raise ValueError(f"{path} is not a field baseline report.json")
    return report


def _counter(report, stage, counter):
    stages = (report.get("final_metrics") or {}).get("stages", {})
    data = stages.get(stage, {})
    return data.get("counters", {}).get(counter) if data.get("status") == "measured" else None


def _clip(report):
    values = [s["iq_health"]["clip_pct"] for s in (report.get("sampled_rf") or {}).get("snippets", [])
              if s.get("iq_health") and s["iq_health"].get("clip_pct") is not None]
    return mean(values) if values else None


def metrics(report: dict) -> dict:
    summary, coverage = report["summary"], report.get("coverage") or {}
    distance = coverage.get("distance") or {}
    distinct = summary.get("distinct_valid_icao")
    if distinct is None:
        distinct = summary.get("backend_visible_distinct_icao")
    return {
        "valid_df17_per_second": summary.get("valid_df17_per_second"),
        "distinct_icao": distinct,
        "positioned_icao": summary.get("positioned_icao"),
        "range_p50_km": distance.get("p50_km"),
        "range_p95_km": distance.get("p95_km"),
        "range_max_km": distance.get("max_km"),
        "position_fixes": distance.get("samples"),
        "crc_rejected": _counter(report, "decoder", "crc_rejected"),
        "dropped_iq_samples": _counter(report, "acquisition", "queue_dropped_samples"),
        "mean_snippet_clip_pct": _clip(report),
        "max_cpu_temperature_c": (summary.get("cpu_temperature_c") or {}).get("max"),
    }


def sectors(report: dict) -> dict:
    """Per 30° bearing sector across altitude bins: fixes and maximum range."""
    result = {}
    for row in (report.get("coverage") or {}).get("sectors") or []:
        item = result.setdefault(row["from_deg"], {"samples": 0, "max_km": None})
        item["samples"] += row["samples"]
        if row["max_km"] is not None:
            item["max_km"] = row["max_km"] if item["max_km"] is None else max(item["max_km"], row["max_km"])
    return result


def changed_factors(a: dict, b: dict) -> list[dict]:
    changes = []
    for group, keys in (("receiver", RECEIVER_FACTORS), ("site", SITE_FACTORS)):
        before, after = a["provenance"].get(group) or {}, b["provenance"].get(group) or {}
        for key in keys:
            if before.get(key) != after.get(key):
                changes.append({"factor": f"{group}.{key}", "a": before.get(key), "b": after.get(key)})
    return changes


def _local_hour(report):
    try:
        started = datetime.fromisoformat(report["provenance"]["window"]["started_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return started.hour + started.minute / 60


def warnings(a: dict, b: dict, changes: list[dict]) -> list[str]:
    found = []
    for name, report in (("A", a), ("B", b)):
        if report.get("status") != "completed":
            found.append(f"Run {name} is incomplete; its counts may be low for reasons other than the change.")
        if report["provenance"].get("source") != "rf":
            found.append(f"Run {name} is not real RF (source {report['provenance'].get('source')!r}).")
        if (metrics(report)["dropped_iq_samples"] or 0) > 0:
            found.append(f"Run {name} dropped IQ samples; fix that before comparing reception.")
    seconds = [r["provenance"]["window"].get("requested_seconds") for r in (a, b)]
    if seconds[0] != seconds[1]:
        found.append(f"Different durations ({seconds[0]} s vs {seconds[1]} s); compare equal windows.")
    # Lat/lon are recorded once per site; a coordinate edit alone is not an RF change.
    rf_changes = [c for c in changes if c["factor"] not in ("site.latitude_deg", "site.longitude_deg",
                                                             "site.location_description")]
    if not rf_changes:
        found.append("No recorded condition changed; the difference reflects air traffic and noise only.")
    elif len(rf_changes) > 1:
        found.append("More than one factor changed (" + ", ".join(c["factor"] for c in rf_changes)
                     + "); the effect cannot be attributed to one of them.")
    hours = [_local_hour(r) for r in (a, b)]
    if None not in hours:
        gap = abs(hours[0] - hours[1])
        gap = min(gap, 24 - gap)
        if gap > MAX_HOUR_GAP:
            found.append(f"Runs started {gap:.1f} h apart in local time of day; air traffic may differ a lot.")
    return found


def compare(a: dict, b: dict) -> dict:
    changes = changed_factors(a, b)
    ma, mb = metrics(a), metrics(b)
    rows = {}
    for key in ma:
        before, after = ma[key], mb[key]
        delta = after - before if None not in (before, after) else None
        ratio = after / before if None not in (before, after) and before else None
        rows[key] = {"a": before, "b": after, "delta": delta, "ratio": ratio}
    sa, sb = sectors(a), sectors(b)
    sector_rows = [{"from_deg": start, "a": sa.get(start), "b": sb.get(start)} for start in range(0, 360, 30)]
    return {
        "schema_version": "1.0", "kind": "field_compare",
        "a": {"capture_id": a["provenance"].get("capture_id"), "started_at": a["provenance"]["window"].get("started_at")},
        "b": {"capture_id": b["provenance"].get("capture_id"), "started_at": b["provenance"]["window"].get("started_at")},
        "changed_factors": changes, "warnings": warnings(a, b, changes),
        "metrics": rows, "sectors": sector_rows,
    }


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 10 else f"{value:.1f}"
    return str(value)


def markdown(result: dict) -> str:
    lines = ["# NetRadar field A/B comparison", "",
             f"- A: `{result['a']['capture_id']}` started {result['a']['started_at']}",
             f"- B: `{result['b']['capture_id']}` started {result['b']['started_at']}", "",
             "## Changed conditions", ""]
    if result["changed_factors"]:
        lines += ["| Factor | A | B |", "|---|---|---|"]
        lines += [f"| {c['factor']} | {_fmt(c['a'])} | {_fmt(c['b'])} |" for c in result["changed_factors"]]
    else:
        lines.append("None recorded.")
    lines += ["", "## Warnings", ""]
    lines += [f"- ⚠️ {w}" for w in result["warnings"]] or ["- None."]
    lines += ["", "## Measured difference", "", "| Metric | A | B | B − A | B / A |", "|---|---:|---:|---:|---:|"]
    for key, row in result["metrics"].items():
        lines.append(f"| {key} | {_fmt(row['a'])} | {_fmt(row['b'])} | {_fmt(row['delta'])} | {_fmt(row['ratio'])} |")
    lines += ["", "## Bearing sectors (all altitudes)", "",
              "| Sector | A fixes | A max km | B fixes | B max km |", "|---|---:|---:|---:|---:|"]
    for row in result["sectors"]:
        a, b = row["a"] or {}, row["b"] or {}
        lines.append(f"| {row['from_deg']}–{row['from_deg'] + 30}° | {_fmt(a.get('samples'))} | "
                     f"{_fmt(a.get('max_km'))} | {_fmt(b.get('samples'))} | {_fmt(b.get('max_km'))} |")
    lines += ["", "One A/B pair is evidence, not proof. Repeat A/B/A/B in similar traffic hours",
              "before changing or buying hardware.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("a", type=Path, help="Report A (before): report.json or its capture directory")
    parser.add_argument("b", type=Path, help="Report B (after)")
    parser.add_argument("--output", type=Path, help="Write Markdown here and JSON next to it")
    args = parser.parse_args(argv)
    paths = [p / "report.json" if p.is_dir() else p for p in (args.a, args.b)]
    try:
        result = compare(*(load(p) for p in paths))
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    text = markdown(result)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
