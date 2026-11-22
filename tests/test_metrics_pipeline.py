from dataclasses import replace
from importlib.util import find_spec
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from netradar.contracts import IqChunk, ModeSFrame
from netradar.decoder.decode import ADSBDecoder
from netradar.metrics import FunnelMetrics, STAGE_COUNTERS
from tests.test_decoder import make_identification_frame

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
HAS_DSP = find_spec("numpy") is not None


class DecoderMetricsTest(unittest.TestCase):
    def test_bad_crc_and_one_bit_repair_follow_decoder_policy(self):
        bad_fixture = json.loads((FIXTURES / "frames_bad.json").read_text())["frames"][0]
        bad = ModeSFrame(hex=bad_fixture["hex"], sample_index=0, source="synthetic")
        clean = replace(bad, hex="8D40621D58C382D690C8AC2863A7", sample_index=10)
        metrics = FunnelMetrics("synthetic", capture_id="decoder-test")
        decoder = ADSBDecoder(metrics=metrics)

        self.assertEqual(decoder.decode(bad), [])
        first = metrics.snapshot()["stages"]["decoder"]
        self.assertEqual(first["counters"]["crc_rejected"], 1)
        self.assertIsNone(first["last_activity_unix_ms"])

        self.assertEqual(len(decoder.decode(clean)), 1)
        repaired, = decoder.decode(replace(bad, sample_index=11))
        self.assertEqual(repaired.metadata["received_frame"], bad.hex)
        self.assertEqual(repaired.metadata["capture_id"], "decoder-test")

        unsupported = make_identification_frame([1] * 8, type_code=31)
        self.assertEqual(decoder.decode(unsupported), [])
        self.assertEqual(decoder.decode(replace(clean, hex="00" + clean.hex[2:], sample_index=12)), [])
        with self.assertRaisesRegex(ValueError, "sources must match"):
            decoder.decode(replace(clean, source="rf"))

        report = metrics.snapshot()
        self.assertEqual(report["distinct_icao_windows"]["windows_seconds"]["60"]["count"], 2)
        self.assertEqual(report["stages"]["decoder"]["counters"], {
            "frames": 5, "supported_df": 4, "unsupported_df": 1,
            "crc_clean": 2, "crc_corrected_one_bit": 1, "crc_rejected": 1,
            "supported_tc": 2, "unsupported_tc": 1, "decoded_events": 2,
            "cpr_successes": 0, "cpr_failures": 2,
        })


class PipelineMetricsTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.report = self.directory / "metrics.json"

    def command(self, *options):
        return [sys.executable, "-m", "netradar.pipeline", "--metrics-json", str(self.report), *options]

    def run_capture(self, *options):
        result = subprocess.run(self.command(*options), cwd=ROOT, capture_output=True, text=True,
                                timeout=10, check=True)
        return json.loads(self.report.read_text()), [json.loads(line) for line in result.stdout.splitlines()]

    @unittest.skipUnless(HAS_DSP, "Install requirements-dsp.txt for metrics integration")
    def test_seven_frame_iq_fixture(self):
        report, events = self.run_capture("--iq", str(FIXTURES / "synthetic_adsb_2msps.cu8"),
                                          "--capture-id", "fixture-seven")
        self.assertEqual(report["source"], "synthetic")
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["capture_head_sample_index"], 560_000)
        self.assertEqual(report["sample_rate_hz"], 2_000_000)
        self.assertEqual(len(events), 7)
        self.assertEqual({item["event"]["metadata"]["capture_id"] for item in events}, {"fixture-seven"})
        stages = report["stages"]
        self.assertEqual(stages["acquisition"]["counters"], {
            "rf_bytes": 1_120_000, "complex_samples": 560_000, "emitted_samples": 560_000,
            "emitted_chunks": 69, "queue_drop_events": 0, "queue_dropped_chunks": 0, "queue_dropped_samples": 0,
        })
        self.assertEqual(stages["dsp"]["counters"], {
            "samples": 560_000, "chunk_gaps": 0, "gap_samples": 0, "preamble_candidates": 774,
            "df17_prefixes": 27, "ppm_tied_rejects": 20, "demodulated_frames": 7,
        })
        self.assertEqual(stages["decoder"]["counters"], {
            "frames": 7, "supported_df": 7, "unsupported_df": 0, "crc_clean": 7,
            "crc_corrected_one_bit": 0, "crc_rejected": 0, "supported_tc": 7, "unsupported_tc": 0,
            "decoded_events": 7, "cpr_successes": 2, "cpr_failures": 2,
        })
        for name, samples in (("acquisition", 3), ("dsp", 3), ("decoder", 1)):
            self.assertEqual(stages[name]["latency"]["sample_count"], samples)
            self.assertIsNotNone(stages[name]["last_activity_unix_ms"])
        self.assertEqual({window["count"] for window in
                          report["distinct_icao_windows"]["windows_seconds"].values()}, {5})

    @unittest.skipUnless(HAS_DSP, "Install requirements-dsp.txt for metrics integration")
    def test_empty_iq_and_dsp_gap(self):
        path = self.directory / "empty.cu8"
        path.touch()
        report, events = self.run_capture("--iq", str(path))
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["source"], "replay")
        self.assertEqual(events, [])
        self.assertIsNone(report["capture_head_sample_index"])
        for name, stage in report["stages"].items():
            self.assertEqual(stage["counters"], dict.fromkeys(STAGE_COUNTERS[name], 0))
            expected = "measured" if name in ("acquisition", "dsp", "decoder") else "unavailable"
            self.assertEqual(stage["status"], expected)
            self.assertEqual(stage["latency"]["sample_count"], 0)

        from netradar.dsp.detector import detect_frames
        metrics = FunnelMetrics("synthetic")
        chunks = (IqChunk(b"\x7f\x7f" * 240, index) for index in (0, 255))
        self.assertEqual(list(detect_frames(chunks, source="synthetic", metrics=metrics)), [])
        counters = metrics.snapshot()["stages"]["dsp"]["counters"]
        self.assertEqual(counters["chunk_gaps"], 1)
        self.assertEqual(counters["gap_samples"], 15)

    def test_idle_stream_reports_before_eof(self):
        cmd = self.command("--source", "replay", "--metrics-interval-seconds", "0.05")
        with subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if self.report.is_file():
                        report = json.loads(self.report.read_text())
                        decoder = report["stages"]["decoder"]
                        if report["elapsed_seconds"] >= .05 and decoder["status"] == "measured":
                            break
                    time.sleep(.02)
                else:
                    self.fail("periodic report was not written while input was idle")
                self.assertEqual(report["run_status"], "running")
                self.assertIsNone(decoder["last_activity_unix_ms"])
                process.stdin.close()
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertEqual(json.loads(self.report.read_text())["run_status"], "completed")
            finally:
                if process.poll() is None:
                    process.kill()
