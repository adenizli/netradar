"""Field A/B comparison (issues #8, #10) and 1090 MHz station estimates (issue #10)."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import field_compare, rf_link

ROOT = Path(__file__).resolve().parents[1]


def report(*, started="2026-11-22T14:00:00+03:00", antenna="stock whip", height=1.5, gain=33.8,
           valid=2.0, distinct=12, positioned=8, p95=60.0, max_km=90.0, drops=0, status="completed",
           source="rf", seconds=600, sector_max=None):
    sectors = []
    for start in range(0, 360, 30):
        for band in ("15,000–29,999 ft", "30,000 ft and above"):
            value = (sector_max or {}).get(start)
            sectors.append({"from_deg": start, "altitude_bin": band, "icao": 1 if value else 0,
                            "samples": 2 if value else 0, "p50_km": value, "p95_km": value,
                            "max_km": value if band == "30,000 ft and above" else (value / 2 if value else None)})
    return {
        "schema_version": "1.0", "status": status,
        "provenance": {
            "capture_id": f"rf-{started}", "source": source,
            "window": {"started_at": started, "requested_seconds": seconds},
            "receiver": {"requested_gain_db": gain, "ppm": 0, "device_index": 0},
            "site": {"antenna_model": antenna, "antenna_height_agl_m": height, "placement": "window",
                     "latitude_deg": 41.08, "longitude_deg": 29.05},
        },
        "summary": {"valid_df17_per_second": valid, "distinct_valid_icao": distinct,
                    "positioned_icao": positioned, "cpu_temperature_c": {"min": 50, "max": 58}},
        "final_metrics": {"stages": {
            "acquisition": {"status": "measured", "counters": {"queue_dropped_samples": drops}},
            "decoder": {"status": "measured", "counters": {"crc_rejected": 40}},
        }},
        "coverage": {"status": "measured", "distance": {"samples": 30, "p50_km": 30.0, "p95_km": p95, "max_km": max_km},
                     "sectors": sectors},
        "sampled_rf": {"snippets": [{"iq_health": {"clip_pct": 0.01}}, {"iq_health": {"clip_pct": 0.03}}]},
    }


class FieldCompareTest(unittest.TestCase):
    def test_single_antenna_change_is_reported_with_deltas(self):
        a = report(sector_max={0: 50.0, 90: 80.0})
        b = report(antenna="1090 MHz collinear", valid=3.0, distinct=15, positioned=11, p95=95.0,
                   max_km=140.0, sector_max={0: 70.0, 180: 40.0})
        result = field_compare.compare(a, b)
        self.assertEqual([c["factor"] for c in result["changed_factors"]], ["site.antenna_model"])
        self.assertEqual(result["warnings"], [])
        self.assertAlmostEqual(result["metrics"]["valid_df17_per_second"]["ratio"], 1.5)
        self.assertEqual(result["metrics"]["range_p95_km"]["delta"], 35.0)
        self.assertAlmostEqual(result["metrics"]["mean_snippet_clip_pct"]["a"], 0.02)
        by_sector = {row["from_deg"]: row for row in result["sectors"]}
        self.assertEqual(by_sector[0]["a"], {"samples": 4, "max_km": 50.0})
        self.assertEqual(by_sector[180]["a"], {"samples": 0, "max_km": None})
        self.assertEqual(by_sector[180]["b"]["max_km"], 40.0)

    def test_confounded_comparisons_are_flagged(self):
        a = report()
        b = report(antenna="collinear", height=6.0, gain=40.2, seconds=1200, drops=8192,
                   started="2026-11-22T21:30:00+03:00", status="incomplete")
        text = " ".join(field_compare.compare(a, b)["warnings"])
        self.assertIn("More than one factor changed", text)
        self.assertIn("Different durations", text)
        self.assertIn("7.5 h apart", text)
        self.assertIn("Run B is incomplete", text)
        self.assertIn("Run B dropped IQ samples", text)

    def test_identical_conditions_and_synthetic_source_are_flagged(self):
        a, b = report(), report(source="synthetic")
        text = " ".join(field_compare.compare(a, b)["warnings"])
        self.assertIn("No recorded condition changed", text)
        self.assertIn("not real RF", text)

    def test_time_of_day_wraps_around_midnight(self):
        a = report(started="2026-11-22T23:30:00+03:00")
        b = report(antenna="x", started="2026-11-23T00:30:00+03:00")
        self.assertFalse(any("apart" in w for w in field_compare.compare(a, b)["warnings"]))

    def test_cli_reads_capture_directories_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name, data in (("a", report()), ("b", report(antenna="collinear"))):
                (tmp / name).mkdir()
                (tmp / name / "report.json").write_text(json.dumps(data))
            out = tmp / "compare.md"
            result = subprocess.run(
                [sys.executable, "-m", "tools.field_compare", str(tmp / "a"), str(tmp / "b"), "--output", str(out)],
                cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertIn("site.antenna_model", result.stdout)
            self.assertTrue(out.with_suffix(".json").exists())
            bad = tmp / "bad.json"
            bad.write_text(json.dumps({"schema_version": "1.0"}))
            failed = subprocess.run([sys.executable, "-m", "tools.field_compare", str(bad), str(bad)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("not a field baseline report", failed.stderr)

    def test_inputs_are_not_modified(self):
        a, b = report(), report(antenna="collinear")
        before = copy.deepcopy((a, b))
        field_compare.compare(a, b)
        self.assertEqual((a, b), before)


class RfLinkTest(unittest.TestCase):
    def test_line_of_sight_matches_the_standard_4_3_earth_rule(self):
        # d ≈ 4.12 (√h_antenna + √h_aircraft) km with heights in metres
        expected = 4.12 * (10 ** 0.5 + (40_000 * 0.3048) ** 0.5)
        self.assertAlmostEqual(rf_link.line_of_sight_km(10, 40_000), expected, delta=1.5)
        self.assertEqual(rf_link.horizon_km(0), 0)

    def test_feedline_loss_and_range_factor(self):
        self.assertAlmostEqual(rf_link.feedline_loss_db("RG58", 5, 2, 0.1, None), 3.7)
        self.assertAlmostEqual(rf_link.feedline_loss_db("anything", 10, 0, 0.1, 0.2), 2.0)
        self.assertIsNone(rf_link.feedline_loss_db("unknown", 10, 0, 0.1, None))
        self.assertAlmostEqual(rf_link.range_factor(6.0206), 0.5, places=3)

    def test_element_lengths_at_1090_mhz(self):
        lengths = rf_link.element_lengths_cm()
        self.assertAlmostEqual(lengths["quarter_wave_cm"], 6.876, places=2)
        self.assertAlmostEqual(lengths["half_wave_cm"], 13.75, places=1)

    def test_cli_markdown_and_unknown_cable_message(self):
        out = subprocess.run([sys.executable, "-m", "tools.rf_link", "--antenna-height-m", "10",
                              "--cable", "rg58", "--cable-length-m", "5"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertIn("3.7 dB", out)
        self.assertIn("40,000 ft", out)
        data = json.loads(subprocess.run(
            [sys.executable, "-m", "tools.rf_link", "--antenna-height-m", "3", "--json"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        self.assertIsNone(data["feedline"]["loss_db"])


if __name__ == "__main__":
    unittest.main()
