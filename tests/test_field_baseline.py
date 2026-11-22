import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.field_report import coverage
from tools.rtl_field_tap import relay


class SplitReads(io.BytesIO):
    def read1(self, size):
        return super().read1(min(size, 7))


class FieldBaselineTest(unittest.TestCase):
    def test_tap_forwards_bytes_and_keeps_only_complete_contiguous_windows(self):
        payload = bytes(range(60))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sink = io.BytesIO()
            forwarded = relay(SplitReads(payload), sink, directory, capture_id="field", rate=2,
                              frequency=1_090_000_000, period=3, length=2, slots=4, max_samples=22)
            self.assertEqual(forwarded, 22)
            self.assertEqual(sink.getvalue(), payload[:44])
            for slot in range(4):
                cu8 = directory / f"snippet-{slot:03}.cu8"
                expected_start = slot * 12
                self.assertEqual(cu8.read_bytes(), payload[expected_start:expected_start + 8])
                sidecar = json.loads(cu8.with_suffix(".json").read_text())
                self.assertEqual(sidecar["capture_id"], "field")
                self.assertEqual(sidecar["start_sample_index"], expected_start // 2)
            self.assertEqual(len(list(directory.glob("*.cu8"))), 4)

    def test_incomplete_window_has_no_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            relay(SplitReads(b"1234"), io.BytesIO(), directory, capture_id="field", rate=2,
                  frequency=1_090_000_000, period=3, length=2, slots=4, max_samples=22)
            self.assertEqual(list(directory.glob("*.json")), [])

    def test_long_sample_bound_is_not_forwarded_as_overflowing_rtl_limit(self):
        tap = Path(__file__).resolve().parents[1] / "tools" / "rtl_field_tap.py"
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            source = run / "fake-rtl-sdr"
            source.write_text(
                f"#!{sys.executable}\nimport sys\n"
                "assert '-n' not in sys.argv[1:]\n"
                "assert sys.argv[-1] == '-'\n"
                "sys.stdout.buffer.write(bytes(range(8)))\n",
                encoding="utf-8",
            )
            source.chmod(0o700)
            for native_limit in ([], ["-n", "3600000000"]):
                with self.subTest(native_limit=native_limit):
                    snippets = run / f"snippets-{len(native_limit)}"
                    snippets.mkdir()
                    settings = {"directory": str(snippets), "executable": str(source),
                                "capture_id": "field", "rate": 2_000_000, "frequency": 1_090_000_000,
                                "period": 60, "length": 2, "slots": 30, "max_samples": 3_600_000_000}
                    result = subprocess.run([sys.executable, str(tap), "-s", "2000000", *native_limit, "-"],
                                            env=dict(os.environ, NETRADAR_FIELD_TAP=json.dumps(settings)),
                                            check=True, capture_output=True, timeout=5)
                    self.assertEqual(result.stdout, bytes(range(8)))
                    self.assertEqual(list(snippets.glob("*.json")), [])

    def test_coverage_uses_station_coordinates_and_distinguishes_empty_sectors(self):
        fixes = {("ABCDEF", 100): ((0.0, 1.0), 6_000), ("ABCDEF", 200): ((0.0, 2.0), 7_000)}
        missing = coverage({}, fixes)
        self.assertEqual(missing["status"], "unavailable")
        self.assertIsNone(missing["distance"]["max_km"])
        report = coverage({"latitude_deg": 0, "longitude_deg": 0}, fixes)
        self.assertEqual(report["status"], "measured")
        self.assertAlmostEqual(report["distance"]["p50_km"], 166.79, places=2)
        self.assertAlmostEqual(report["distance"]["p95_km"], 222.39, places=2)
        sampled, = [sector for sector in report["sectors"] if sector["samples"]]
        self.assertEqual((sampled["from_deg"], sampled["altitude_bin"], sampled["icao"]), (90, "5,000–14,999 ft", 1))
        self.assertEqual(len(report["sectors"]), 60)
