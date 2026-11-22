"""Producer -> real FastAPI backend diagnostics, end to end in separate processes (issue #6).

Starts the real backend with uvicorn on a free loopback port, runs the production pipeline
on the synthetic CU8 fixture with --diagnostics, and reads /api/diagnostics and /metrics.
"""
from importlib.util import find_spec
import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "synthetic_adsb_2msps.cu8"
HAS_STACK = all(find_spec(name) is not None for name in ("numpy", "fastapi", "uvicorn"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(HAS_STACK, "Install requirements-dev.txt and requirements-dsp.txt")
class DiagnosticsEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "netradar.backend.server:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--workers", "1", "--log-level", "warning"],
            cwd=ROOT, env={"NETRADAR_EXPECT_RECEIVER": "1", "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_backend)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urlopen(self.url + "/healthz", timeout=1):
                    return
            except OSError:
                if self.backend.poll() is not None:
                    self.fail("backend exited: " + self.backend.stderr.read().decode())
                time.sleep(0.1)
        self.fail("backend did not become healthy")

    def stop_backend(self):
        self.backend.terminate()
        try:
            self.backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.backend.kill()
            self.backend.wait()
        self.backend.stderr.close()

    def get(self, path):
        with urlopen(self.url + path, timeout=5) as response:
            return response.read().decode()

    def test_pipeline_diagnostics_reach_the_backend(self):
        before = json.loads(self.get("/api/diagnostics"))
        self.assertIsNone(before["capture"])
        subprocess.run(
            [sys.executable, "-m", "netradar.pipeline", "--iq", str(FIXTURE), "--source", "synthetic",
             "--backend-url", self.url, "--diagnostics"],
            cwd=ROOT, check=True, capture_output=True, timeout=60,
        )
        report = json.loads(self.get("/api/diagnostics"))
        self.assertEqual(report["receiver_state"], "completed")
        capture = report["capture"]
        self.assertEqual(capture["source"], "synthetic")
        self.assertEqual(capture["run_status"], "completed")
        self.assertEqual(capture["stages"]["acquisition"]["counters"]["complex_samples"], 560_000)
        self.assertEqual(capture["stages"]["decoder"]["counters"]["crc_clean"], 7)
        self.assertEqual(capture["stages"]["decoder"]["counters"]["cpr_successes"], 2)
        self.assertEqual(capture["stages"]["delivery"]["counters"]["accepted"], 7)
        self.assertEqual(capture["distinct_icao_windows"]["windows_seconds"]["300"]["count"], 5)
        self.assertEqual(report["backend"]["accepted"], 7)
        self.assertIsNotNone(report["last_valid_crc_at_unix_ms"])
        aircraft = json.loads(self.get("/api/aircraft"))
        self.assertEqual(len(aircraft), 5)
        metrics = self.get("/metrics")
        self.assertIn("netradar_", metrics)
        self.assertNotIn("4840D6", metrics)  # no per-aircraft labels


if __name__ == "__main__":
    unittest.main()
