"""Analyze sampled CU8 and live metric snapshots from a field baseline."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import median

from tools import iq_health, rf_funnel

SITE_FIELDS = (
    "location_description", "latitude_deg", "longitude_deg", "site_elevation_m",
    "antenna_model", "antenna_height_agl_m", "antenna_orientation", "placement",
    "cable_type", "cable_length_m", "cable_description", "connectors", "filter", "lna",
)
ALTITUDE_BINS = ("below 5,000 ft", "5,000–14,999 ft", "15,000–29,999 ft", "30,000 ft and above", "unknown")
SNIPPET_KEYS = ("candidates", "df17_prefix", "tied", "demodulated", "clean", "clean_duplicate", "fixed", "bad")


def load_site(path):
    supplied = json.loads(path.read_text(encoding="utf-8")) if path is not None else {}
    if not isinstance(supplied, dict) or supplied.keys() - set(SITE_FIELDS):
        raise ValueError("Site metadata must be an object containing only documented fields")
    site = {key: supplied.get(key) for key in SITE_FIELDS}
    latitude, longitude = site["latitude_deg"], site["longitude_deg"]
    if (latitude is not None or longitude is not None) and not valid_coordinates(latitude, longitude):
        raise ValueError("Site coordinates must be a complete, finite latitude/longitude pair")
    return site


def valid_coordinates(lat, lon):
    return (type(lat) in (float, int) and type(lon) in (float, int)
            and -90 <= lat <= 90 and -180 <= lon <= 180)


def polar(origin, coords):
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, coords)
    delta_lon = lon2 - lon1
    partial = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    distance = 2 * 6371.0088 * math.asin(math.sqrt(min(1, max(0, partial))))
    bearing = math.degrees(math.atan2(
        math.sin(delta_lon) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon),
    )) % 360
    return distance, bearing


def altitude_bin(altitude):
    if altitude is None:
        return ALTITUDE_BINS[-1]
    index = sum(altitude >= boundary for boundary in (5_000, 15_000, 30_000))
    return ALTITUDE_BINS[index]


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50_km": None, "p95_km": None, "max_km": None}
    return {"samples": len(ordered), "p50_km": median(ordered),
            "p95_km": ordered[math.ceil(0.95 * len(ordered)) - 1], "max_km": ordered[-1]}


def load_snapshots(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stage_counter(metric, stage, counter):
    details = metric.get("stages", {}).get(stage, {})
    return details.get("counters", {}).get(counter) if details.get("status") == "measured" else None


def collect_aircraft(snapshots, capture_id, window_start):
    observed, positioned, fixes = set(), set(), {}
    hours = {}
    for snapshot in snapshots:
        if "aircraft" not in snapshot:
            continue
        timestamp = datetime.fromisoformat(snapshot["observed_at"]).astimezone(window_start.tzinfo)
        hour = timestamp.replace(minute=0, second=0, microsecond=0).isoformat()
        stats = hours.setdefault(hour, {"snapshots": 0, "new_icao": set(), "positioned_icao": set()})
        stats["snapshots"] += 1
        for aircraft in snapshot["aircraft"]:
            icao = aircraft["icao"]
            if capture_id in aircraft["capture_ids"] and icao not in observed:
                stats["new_icao"].add(icao)
                observed.add(icao)
            event = aircraft["position_event"] or {}
            fields, event_meta = event.get("fields", {}), event.get("metadata", {})
            coords = fields.get("latitude"), fields.get("longitude")
            if (event_meta.get("source") != "rf" or event_meta.get("capture_id") != capture_id
                    or event_meta.get("df") != 17 or event_meta.get("crc_valid") is not True
                    or not valid_coordinates(*coords)):
                continue
            if icao not in observed:
                stats["new_icao"].add(icao)
                observed.add(icao)
            key = icao, event_meta["sample_index"]
            if key in fixes:
                continue
            positioned.add(icao)
            stats["positioned_icao"].add(icao)
            altitude = fields.get("altitude_ft")
            if altitude is None:
                altitude = fields.get("gnss_height_ft")
            fixes[key] = coords, altitude
    summary = [{"local_hour": hour, "successful_snapshots": entry["snapshots"],
                "first_seen_icao": len(entry["new_icao"]), "positioned_icao": len(entry["positioned_icao"])}
               for hour, entry in hours.items()]
    return observed, positioned, fixes, summary


def coverage(site, fixes):
    origin = site.get("latitude_deg"), site.get("longitude_deg")
    if not valid_coordinates(*origin):
        return {"status": "unavailable", "reason": "Station coordinates were not supplied.",
                "distance": distribution([]), "sectors": None}
    if not fixes:
        return {"status": "unavailable", "reason": "No decoded coordinates were observed in the capture.",
                "distance": distribution([]), "sectors": None}
    distances = defaultdict(list)
    aircraft = defaultdict(set)
    all_distances = []
    for (icao, _sample), (coords, altitude) in fixes.items():
        distance, bearing = polar(origin, coords)
        key = (int(bearing // 30) % 12 * 30, altitude_bin(altitude))
        distances[key].append(distance)
        aircraft[key].add(icao)
        all_distances.append(distance)
    sectors = [{"from_deg": start, "to_deg_exclusive": start + 30, "altitude_bin": band,
                "icao": len(aircraft[start, band]), **distribution(distances[start, band])}
               for start in range(0, 360, 30) for band in ALTITUDE_BINS]
    return {"status": "measured", "reason": None, "distance": distribution(all_distances), "sectors": sectors}


def analyze_snippets(run, capture_id):
    results = []
    totals, unique_clean, unique_fixable = Counter(), set(), set()
    for path in sorted((run / "snippets").glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        cu8 = path.with_suffix(".cu8")
        if (meta.get("capture_id") != capture_id or meta.get("kind") != "real_receiver_capture"
                or meta.get("file") != cu8.name or meta["complex_samples"] * 2 != cu8.stat().st_size):
            raise ValueError(f"Inconsistent RF snippet metadata in {path}")
        _, _, _, health = iq_health.analyze(cu8)
        seconds, counts, clean, fixable, types = rf_funnel.analyze(cu8)
        totals.update(counts)
        unique_clean.update(clean)
        unique_fixable.update(fixable)
        if not math.isfinite(health["peak_to_noise_db"]):
            health["peak_to_noise_db"] = None
        checksum = hashlib.sha256()
        with cu8.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                checksum.update(block)
        results.append({**meta, "duration_seconds": seconds, "sha256": checksum.hexdigest(), "iq_health": health,
                        "funnel": {key: counts[key] for key in SNIPPET_KEYS}, "clean_type_codes": dict(types)})
    return {
        "snippets": results, "total_seconds": sum(item["duration_seconds"] for item in results),
        "totals": {key: totals[key] for key in SNIPPET_KEYS},
        "distinct_crc_clean_icao": len(unique_clean), "distinct_single_bit_fixable_icao": len(unique_fixable),
    }


def get_summary(run):
    metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
    capture_id = metadata["capture_id"]
    snapshots = load_snapshots(run / "snapshots.jsonl")
    metrics = [item["metrics"] for item in snapshots if item.get("metrics", {}).get("source") == "rf"
               and item["metrics"].get("capture_id") == capture_id]
    final = metrics[-1] if metrics else {}
    start = datetime.fromisoformat(metadata["window"]["started_at"])
    observed, positioned, fixes, hourly = collect_aircraft(snapshots, capture_id, start)
    backend_ok = sum("aircraft" in item for item in snapshots)
    positions = coverage(metadata["site"], fixes)
    if backend_ok == 0:
        positions["reason"] = "Aircraft snapshots were unavailable during this capture."
    clean = stage_counter(final, "decoder", "crc_clean")
    corrected = stage_counter(final, "decoder", "crc_corrected_one_bit")
    elapsed = final.get("elapsed_seconds")
    wall = metadata["window"].get("elapsed_wall_seconds")
    samples = stage_counter(final, "acquisition", "complex_samples")
    snippets = analyze_snippets(run, capture_id)
    measured_seconds = samples / metadata["receiver"]["sample_rate_hz"] if samples is not None else None
    complete = (metadata.get("pipeline_returncode") == 0 and final.get("run_status") == "completed"
                and wall is not None and wall >= metadata["window"]["requested_seconds"]
                and measured_seconds is not None and measured_seconds >= metadata["window"]["requested_seconds"]
                and len(snippets["snippets"]) == metadata["capture"]["snippet_limit"])
    temperatures = [s["cpu_temperature_c"] for s in snapshots if s.get("cpu_temperature_c") is not None]
    valid = clean + corrected if clean is not None and corrected is not None else None
    windows = final.get("distinct_icao_windows", {})
    largest = windows.get("windows_seconds", {}).get("900", {})
    sample_head = final.get("capture_head_sample_index")
    rate = final.get("sample_rate_hz")
    distinct = largest.get("count") if sample_head is not None and rate and sample_head <= 900 * rate else None
    diagnostics = [sample["diagnostics"] for sample in snapshots
                   if sample.get("diagnostics", {}).get("capture_id") == capture_id]
    return {
        "schema_version": "1.0", "status": "completed" if complete else "incomplete",
        "provenance": metadata,
        "summary": {
            "wall_seconds": wall, "measured_sample_seconds": measured_seconds,
            "metrics_elapsed_seconds": elapsed, "valid_df17": valid,
            "valid_df17_per_second": valid / measured_seconds if valid is not None and measured_seconds else None,
            "distinct_valid_icao": distinct,
            "distinct_valid_icao_lower_bound": largest.get("lower_bound") if distinct is not None else None,
            "backend_visible_distinct_icao": len(observed) if backend_ok else None,
            "positioned_icao": len(positioned) if backend_ok else None,
            "backend_visible_without_observed_position": len(observed - positioned) if backend_ok else None,
            "distinct_position_fixes_observed": len(fixes) if backend_ok else None,
            "backend_snapshots_ok": backend_ok,
            "backend_snapshots_failed": sum("backend_error" in item for item in snapshots),
            "metric_snapshots": len({item["reported_at_unix_ms"] for item in metrics}),
            "cpu_temperature_c": {"min": min(temperatures, default=None), "max": max(temperatures, default=None)},
        },
        "final_metrics": final, "coverage": positions, "hourly_observed_traffic": hourly, "sampled_rf": snippets,
        "backend_diagnostics": {
            "matching_snapshots": len(diagnostics), "failed_polls": sum("diagnostics_error" in s for s in snapshots),
            "latest": diagnostics[-1] if diagnostics else None,
        },
    }


def human(value):
    if type(value) is float:
        return f"{value:.3f}"
    return "unknown" if value is None else str(value)


def markdown(report):
    meta = report["provenance"]
    summary = report["summary"]
    sampled = report["sampled_rf"]
    metrics = report["final_metrics"]
    positions = report["coverage"]
    diagnostics = report["backend_diagnostics"]
    distinct = summary["distinct_valid_icao"]
    distinct_text = human(distinct) if distinct is not None else "unavailable from the final 15-minute metric window"
    if summary["distinct_valid_icao_lower_bound"]:
        distinct_text += " (lower bound at the metric capacity)"
    diagnostics_lines = []
    if meta["capture"].get("backend_diagnostics_requested"):
        diagnostics_lines = [
            f"Backend diagnostics yielded {diagnostics['matching_snapshots']} capture-matched samples and "
            f"{diagnostics['failed_polls']} failed polls.", "",
        ]
    lines = [
        f"# NetRadar field baseline {meta['capture_id']}", "",
        f"Collection status **{report['status']}**.", "",
        "## Run and receiver", "",
        f"- Code revision `{meta['code_commit']}`",
        *[f"- {key.replace('_', ' ')} — {human(value)}" for key, value in meta["window"].items()],
        *[f"- {key.replace('_', ' ')} — {human(value)}" for key, value in meta["receiver"].items()],
        f"- CPU temperature min/max — {human(summary['cpu_temperature_c']['min'])} / "
        f"{human(summary['cpu_temperature_c']['max'])} °C", "",
        "## Site", "",
        *[f"- {key.replace('_', ' ')} — {human(value)}" for key, value in meta["site"].items()], "",
        "## Live funnel", "",
        f"- {human(summary['valid_df17'])} valid DF17 frames at {human(summary['valid_df17_per_second'])} "
        f"frames/s using {human(summary['measured_sample_seconds'])} s of acquired RTL samples.",
        f"- Receiver-wide distinct valid ICAO for the capture — {distinct_text}.",
        f"- {human(summary['backend_visible_distinct_icao'])} distinct ICAO visible in capture-matched backend snapshots. "
        "This is a lower bound on receiver-wide distinct ICAO.",
        f"- {human(summary['positioned_icao'])} of the backend-visible ICAO had observed coordinates. "
        f"{human(summary['backend_visible_without_observed_position'])} had no observed position, so their range is unavailable.",
        f"- {summary['metric_snapshots']} distinct funnel snapshots, {summary['backend_snapshots_ok']} successful "
        f"aircraft API polls and {summary['backend_snapshots_failed']} failed polls.", "",
        "| Stage | Status | Cumulative counters |", "| --- | --- | --- |",
        *[f"| {name} | {stage['status']} | " +
          (", ".join(f"{key.replace('_', ' ')} {value}" for key, value in stage["counters"].items())
           if stage["status"] == "measured" else "Unavailable") + " |"
          for name, stage in metrics.get("stages", {}).items()], "",
        *diagnostics_lines,
        "## Sampled CU8", "",
        f"{len(sampled['snippets'])} contiguous snippets cover {sampled['total_seconds']:.1f} s of raw RTL output. "
        "Each snippet has a capture sample offset and SHA-256 in the JSON report. "
        "Offline 1-bit fixable counts describe CRC possibilities. Actual live corrections are in the decoder metrics.", "",
        "| Offset (s) | IQ clip (%) | Peak/noise (dB) | Preambles | DF17 prefix | Tie rejects | Demodulated | Clean CRC | 1-bit fixable | Bad CRC |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for snippet in sampled["snippets"]:
        health, funnel = snippet["iq_health"], snippet["funnel"]
        offset = snippet["start_sample_index"] / snippet["sample_rate_hz"]
        lines.append(f"| {offset:.1f} | {human(health['clip_pct'])} | {human(health['peak_to_noise_db'])} | "
                     f"{funnel['candidates']} | {funnel['df17_prefix']} | {funnel['tied']} | "
                     f"{funnel['demodulated']} | {funnel['clean']} | "
                     f"{funnel['fixed']} | {funnel['bad']} |")
    lines.extend(["", "## Range and 360° coverage", ""])
    if positions["status"] == "measured":
        d = positions["distance"]
        lines.extend([
            f"{d['samples']} distinct coordinate fixes sampled from the backend. Great-circle distance "
            f"p50 / p95 / max — {d['p50_km']:.1f} / {d['p95_km']:.1f} / {d['max_km']:.1f} km.",
            "Sectors and altitudes without observed fixes have empty distance metrics in JSON.", "",
            "| Bearing sector | Altitude | ICAO | Fixes | p50 km | p95 km | Max km |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
            *[f"| {s['from_deg']:03}–{s['to_deg_exclusive']:03}° | {s['altitude_bin']} | {s['icao']} | "
              f"{s['samples']} | {s['p50_km']:.1f} | {s['p95_km']:.1f} | {s['max_km']:.1f} |"
              for s in positions["sectors"] if s["samples"]],
        ])
    else:
        lines.append(f"Distance percentiles and directional range are unavailable. {positions['reason']}")
    lines.extend(["", "## Hourly observed traffic", "",
                  "These rows describe the portions of local hours sampled during this run. "
                  "Traffic outside NetRadar's reception is unmeasured.", "",
                  "| Local hour | Successful polls | First-seen ICAO | ICAO with a newly sampled position |",
                  "| --- | ---: | ---: | ---: |"])
    for row in report["hourly_observed_traffic"]:
        lines.append(f"| {row['local_hour']} | {row['successful_snapshots']} | {row['first_seen_icao']} | "
                     f"{row['positioned_icao']} |")
    lines.extend(["", "## RF limited vs decoder limited vs UI limited", "",
                  *[f"- {claim}" for claim in interpretation(report)], ""])
    return "\n".join(lines)


def interpretation(report):
    metrics, sampled = report["final_metrics"], report["sampled_rf"]
    totals = sampled["totals"]
    drops = stage_counter(metrics, "acquisition", "queue_dropped_samples")
    crc_rejected = stage_counter(metrics, "decoder", "crc_rejected")
    cpr_good = stage_counter(metrics, "decoder", "cpr_successes")
    cpr_bad = stage_counter(metrics, "decoder", "cpr_failures")
    delivery_ok = stage_counter(metrics, "delivery", "accepted")
    delivery_bad = stage_counter(metrics, "delivery", "rejected")
    display = metrics.get("stages", {}).get("display", {})
    clipped = sum(s["iq_health"]["clip_pct"] > 1 for s in sampled["snippets"])
    return [
        f"**RF and acquisition.** The sample snippets include {totals['clean']} clean CRC hits and "
        f"{clipped} snippets above 1% clipped bytes. The live receive queue dropped {human(drops)} "
        "samples. Propagation and antenna effects require a station survey or controlled comparison.",
        f"**Decoder.** The live decoder rejected {human(crc_rejected)} frames at CRC and recorded "
        f"{human(cpr_good)} CPR successes and {human(cpr_bad)} CPR failures. "
        "CRC loss can arise from interference, weak reception or demodulation.",
        f"**Delivery and UI.** The producer recorded {human(delivery_ok)} accepted and {human(delivery_bad)} "
        f"rejected deliveries. Positioned ICAO exposed by the backend — {human(report['summary']['positioned_icao'])}. "
        + ("Display metrics are present in the final funnel." if display.get("status") == "measured"
           else "Browser display and a UI-side bottleneck are unmeasured."),
    ]


def write_reports(run):
    report = get_summary(run)
    (run / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (run / "report.md").write_text(markdown(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    write_reports(args.run_directory)
