"""Checks for one-command launcher (no SDR or SSH needed)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherTest(unittest.TestCase):
    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", "start.sh"], cwd=ROOT, check=True)

    def test_help(self):
        output = subprocess.check_output(
            ["bash", "start.sh", "--help"], cwd=ROOT, text=True
        )
        self.assertIn("./start.sh", output)
        self.assertNotIn("--demo", output)

    def test_demo_flag_is_rejected(self):
        result = subprocess.run(
            ["bash", "start.sh", "--demo"], cwd=ROOT, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Bilinmeyen argüman", result.stderr)

    def test_missing_session_stop_is_harmless(self):
        subprocess.run(
            ["bash", "start.sh", "--stop-pi", "00000000000000000000000000000000"],
            cwd=ROOT, check=True,
        )
