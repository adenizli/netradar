"""Hardware-free checks of the real live-process -> DSP -> event delivery path."""
from importlib.util import find_spec
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from netradar.acquisition.receiver import replay_cu8, stream_rtlsdr
from tests.test_acquisition import FAKE_RTL_SDR, FIXTURE, TOTAL_SAMPLES
from tests.test_forward import receiver

ROOT = Path(__file__).resolve().parents[1]
HAS_DSP = find_spec("numpy") is not None


class AcquisitionValidationTest(unittest.TestCase):
    def test_strict_truncation_is_opt_in_and_preserves_default_warning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "odd.cu8"
            path.write_bytes(b"\x7f" * 9)
            with self.assertRaisesRegex(ValueError, "unpaired"):
                next(replay_cu8(path, strict=True, chunk_samples=2))
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(sum(c.sample_count for c in replay_cu8(path)), 4)

    def test_invalid_live_options_fail_before_starting_device(self):
        for options in ({"gain_db": float("nan")}, {"device_index": -1}, {"ppm": 1.5},
                        {"chunk_samples": True}, {"rtl_sdr_cmd": []}, {"rtl_sdr_cmd": [""]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                stream_rtlsdr(**options)

    def test_metadata_is_not_coerced_to_a_different_sampling_rate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.cu8"
            path.write_bytes(b"\x7f\x7f")
            for metadata in ({"sample_rate_hz": 2000000.5}, {"sample_rate_hz": True},
                             {"center_frequency_hz": -1}):
                path.with_suffix(".json").write_text(json.dumps(metadata))
                with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                    replay_cu8(path)


@unittest.skipUnless(HAS_DSP, "Install requirements-dsp.txt for live DSP integration")
class LivePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.args_out = self.directory / "rtl-args.txt"
        self.pid_out = self.directory / "rtl-pid.txt"

    def fake_executable(self, *, forever=False, fail=False):
        path = self.directory / "rtl_sdr_fake"
        options = ["--fixture", str(FIXTURE), "--args-out", str(self.args_out)]
        if forever:
            options += ["--forever", "--delay", "0.001"]
        if fail:
            options += ["--fail"]
        path.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"open({str(self.pid_out)!r}, 'w').write(str(os.getpid()))\n"
            f"sys.argv[1:1] = {options + ['--']!r}\n" + FAKE_RTL_SDR
        )
        path.chmod(0o755)
        return path

    def paced_executable(self, repeats, *, idle=False):
        path = self.directory / "rtl_sdr_paced"
        path.write_text(f"""#!{sys.executable}
import os, sys, time
open({str(self.pid_out)!r}, "w").write(str(os.getpid()))
data = open({str(FIXTURE)!r}, "rb").read()
began = time.monotonic()
for repeat in range({repeats}):
    for offset in range(0, len(data), 16384):
        sample_bytes = data[offset:offset + 16384]
        os.write(sys.stdout.fileno(), sample_bytes)
        target = began + (repeat * len(data) + offset + len(sample_bytes)) / 4_000_000
        time.sleep(max(0, target - time.monotonic()))
if {idle!r}:
    time.sleep(60)
""")
        path.chmod(0o755)
        return path

    def live_command(self, path, *options):
        return [sys.executable, "-m", "netradar.pipeline", "--rtl-sdr",
                "--rtl-sdr-command", str(path), "--queue-max-chunks", "256", *options]

    def assert_receiver_reaped(self):
        pid = int(self.pid_out.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_live_cli_detects_all_frames_and_forwards_tuner_options(self):
        path = self.fake_executable()
        result = subprocess.run(self.live_command(
            path, "--max-samples", str(TOTAL_SAMPLES), "--gain-db", "auto", "--ppm", "-3",
            "--device-index", "1", "--source", "synthetic",
        ), cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
        updates = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(updates), 7)
        self.assertTrue(all(u["event"]["metadata"]["source"] == "synthetic" for u in updates))
        self.assertTrue(all(u["event"]["observed_at_unix_ms"] is None for u in updates))
        self.assertIn("dropped=0", result.stderr)
        self.assertIn(f"received={TOTAL_SAMPLES}", result.stderr)
        self.assertEqual(self.args_out.read_text().splitlines(), [
            "-f", "1090000000", "-s", "2000000", "-g", "0", "-p", "-3", "-d", "1",
            "-n", str(TOTAL_SAMPLES), "-",
        ])
        self.assert_receiver_reaped()

    def test_live_cli_posts_events_directly_to_backend(self):
        path = self.fake_executable()
        report_path = self.directory / "metrics.json"
        with receiver() as (url, received):
            result = subprocess.run(self.live_command(
                path, "--max-samples", str(TOTAL_SAMPLES), "--backend-url", url,
                "--metrics-json", str(report_path),
            ), cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(received), 7)
        self.assertTrue(all(path == "/api/events" for path, event in received))
        self.assertTrue(all(event["metadata"]["source"] == "rf" for path, event in received))
        self.assertEqual(received[2][1]["fields"]["callsign"], "KLM1023")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["stages"]["delivery"]["counters"], {"accepted": 7, "rejected": 0, "undelivered": 0, "unconfirmed": 0})
        self.assertEqual(report["stages"]["delivery"]["latency"]["sample_count"], 1)
        self.assertTrue(all(event["metadata"]["capture_id"] == report["capture_id"] for _, event in received))
        self.assertIn("dropped=0", result.stderr)
        self.assert_receiver_reaped()

    def test_slow_http_drains_raw_events_in_order_without_iq_drops(self):
        path = self.paced_executable(6)
        report_path = self.directory / "metrics.json"
        with receiver(lambda: time.sleep(0.07)) as (url, received):
            result = subprocess.run(self.live_command(
                path, "--queue-max-chunks", "64", "--max-samples", str(6 * TOTAL_SAMPLES),
                "--backend-url", url, "--metrics-json", str(report_path),
            ), cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
        self.assertIn("dropped=0", result.stderr)
        events = [event for _, event in received]
        self.assertEqual(len(events), 42)
        indices = [event["metadata"]["sample_index"] for event in events]
        self.assertEqual(indices, sorted(indices))
        expected = json.loads(FIXTURE.with_suffix(".json").read_text())["expected_frames"]
        self.assertEqual([event["raw_frame"] for event in events], [item["hex"] for item in expected] * 6)
        stages = json.loads(report_path.read_text())["stages"]
        acquisition = stages["acquisition"]
        self.assertEqual(acquisition["counters"]["complex_samples"], 6 * TOTAL_SAMPLES)
        self.assertEqual(acquisition["counters"]["queue_dropped_samples"], 0)
        self.assertEqual(acquisition["queue"]["depth"], 0)
        self.assertGreater(acquisition["queue_dwell_latency"]["sample_count"], 0)
        self.assertGreater(acquisition["consumer_chunk_latency"]["sample_count"], 0)
        self.assertEqual(stages["delivery"]["counters"]["accepted"], 42)
        self.assert_receiver_reaped()

    def test_stalled_http_drain_reports_unconfirmed_and_undelivered(self):
        path = self.paced_executable(1)
        report_path = self.directory / "metrics.json"
        release = Event()
        with receiver(lambda: release.wait(4)) as (url, received):
            try:
                result = subprocess.run(self.live_command(
                    path, "--max-samples", str(TOTAL_SAMPLES), "--backend-url", url,
                    "--delivery-drain-timeout-seconds", "0.2", "--metrics-json", str(report_path),
                ), cwd=ROOT, capture_output=True, text=True, timeout=4)
            finally:
                release.set()
        self.assertEqual(result.returncode, 2)
        self.assertIn("drain interrupted or timed out", result.stderr)
        self.assertEqual(len(received), 1)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["run_status"], "failed")
        self.assertEqual(report["stages"]["delivery"]["counters"], {
            "accepted": 0, "rejected": 0, "undelivered": 6, "unconfirmed": 1,
        })
        self.assert_receiver_reaped()

    def test_saturated_delivery_queue_reports_failure_and_accounts_for_decoded_events(self):
        path = self.paced_executable(2)
        report_path = self.directory / "metrics.json"
        release = Event()
        with receiver(lambda: release.wait(4)) as (url, received):
            try:
                result = subprocess.run(self.live_command(
                    path, "--max-samples", str(2 * TOTAL_SAMPLES), "--backend-url", url,
                    "--delivery-max-events", "2", "--delivery-drain-timeout-seconds", "0.2",
                    "--metrics-json", str(report_path),
                ), cwd=ROOT, capture_output=True, text=True, timeout=4)
            finally:
                release.set()
        self.assertEqual(result.returncode, 2)
        self.assertIn("aircraft delivery queue full", result.stderr)
        self.assertEqual(len(received), 1)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["run_status"], "failed")
        counters = report["stages"]["delivery"]["counters"]
        self.assertEqual(counters, {"accepted": 0, "rejected": 0, "undelivered": 3, "unconfirmed": 1})
        self.assertEqual(sum(counters.values()), report["stages"]["decoder"]["counters"]["decoded_events"])
        self.assert_receiver_reaped()

    def test_sigint_drains_already_decoded_events_and_reaps_receiver(self):
        path = self.paced_executable(1, idle=True)
        report_path = self.directory / "metrics.json"
        release = Event()
        with receiver(lambda: release.wait(4)) as (url, received):
            process = subprocess.Popen(self.live_command(
                path, "--backend-url", url, "--metrics-json", str(report_path),
                "--metrics-interval-seconds", "0.05", "--chunk-samples", "8000",
            ), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if report_path.exists():
                        report = json.loads(report_path.read_text())
                        if report["stages"]["decoder"]["counters"]["decoded_events"] == 7:
                            break
                    time.sleep(0.01)
                else:
                    self.fail("producer did not decode the complete fixture")
                process.send_signal(signal.SIGINT)
                release.set()
                _, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 130, stderr)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                process.communicate()
        self.assertEqual(len(received), 7)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["run_status"], "interrupted")
        self.assertEqual(report["stages"]["delivery"]["counters"]["accepted"], 7)
        self.assert_receiver_reaped()

    def test_backend_failure_stops_and_reaps_receiver(self):
        path = self.fake_executable(forever=True)
        report_path = self.directory / "metrics.json"
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))  # Bound but deliberately not listening.
            url = f"http://127.0.0.1:{unavailable.getsockname()[1]}"
            result = subprocess.run(self.live_command(
                path, "--backend-url", url, "--metrics-json", str(report_path)),
                                    cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("RTL-SDR:", result.stderr)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["run_status"], "failed")
        counts = report["stages"]["delivery"]["counters"]
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(counts["rejected"], 0)
        self.assertEqual(counts["unconfirmed"], 1)
        self.assertEqual(sum(counts.values()), report["stages"]["decoder"]["counters"]["decoded_events"])
        self.assert_receiver_reaped()

    def test_ctrl_c_stops_reader_threads_and_receiver_process(self):
        path = self.fake_executable(forever=True)
        process = subprocess.Popen(self.live_command(path), cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first["type"], "aircraft_update")
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130)
            self.assertNotIn("Traceback", stderr)
            self.assertIn("stopped", stderr)
            self.assert_receiver_reaped()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_live_device_startup_failure_is_actionable(self):
        path = self.fake_executable(fail=True)
        result = subprocess.run(self.live_command(path), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("usb_claim_interface", result.stderr)
        self.assertIn("TROUBLESHOOTING", result.stderr)
        self.assert_receiver_reaped()

    def test_missing_rtl_executable_and_wrong_rate_fail_cleanly(self):
        missing = self.directory / "not-installed"
        result = subprocess.run(self.live_command(missing), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Install", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        path = self.fake_executable()
        result = subprocess.run(self.live_command(path, "--sample-rate-hz", "2400000"),
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.pid_out.exists())

    def test_replay_sidecar_rate_is_honored_and_explicit_override_works(self):
        path = self.directory / "capture.cu8"
        path.write_bytes(FIXTURE.read_bytes())
        path.with_suffix(".json").write_text(json.dumps({"sample_rate_hz": 2_400_000}))
        command = [sys.executable, "-m", "netradar.pipeline", "--iq", str(path)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("2,000,000", result.stderr)
        result = subprocess.run(command + ["--sample-rate-hz", "2000000"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(len(result.stdout.splitlines()), 7)

    def test_early_eof_does_not_report_success_for_requested_sample_count(self):
        path = self.fake_executable()
        result = subprocess.run(self.live_command(path, "--max-samples", str(TOTAL_SAMPLES + 1)),
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("expected", result.stderr)
        self.assert_receiver_reaped()


if __name__ == "__main__":
    unittest.main()
