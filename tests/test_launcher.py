"""Checks for one-command launcher (no SDR or SSH needed)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

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

    def test_pi_launcher_keeps_backend_alive_until_delivery_drains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / "start.sh", root)
            (root / "bin").mkdir()
            (root / ".venv/bin").mkdir(parents=True)

            def executable(path, contents):
                full_path = root / path
                full_path.write_text(contents)
                full_path.chmod(0o700)

            executable("bin/uname", "#!/bin/sh\necho Linux\n")
            executable("bin/rtl_sdr", "#!/bin/sh\nexit 0\n")
            executable("bin/curl", '#!/bin/sh\ntest -f "$NETRADAR_TEST_DIR/api-active"\n')
            executable("bin/setsid", f"#!{sys.executable}\nimport os, sys\nos.setsid()\nos.execvp(sys.argv[1], sys.argv[1:])\n")
            executable(".venv/bin/python", f"#!{sys.executable}\n" + r'''
from pathlib import Path
import os
import signal
import sys
import time

root = Path(os.environ["NETRADAR_TEST_DIR"])
api = root / "api-active"
if sys.argv[1] == "-c":
    sys.exit(0)

if sys.argv[2] == "uvicorn":
    def terminate(*_args):
        api.unlink()
        sys.exit(0)
    signal.signal(signal.SIGTERM, terminate)
    api.touch()
else:
    def drain(*_args):
        time.sleep(1.3)
        (root / "delivery-result").write_text("drained" if api.exists() else "backend stopped")
        sys.exit(130)
    signal.signal(signal.SIGINT, drain)
    (root / "producer-active").touch()

while True:
    time.sleep(.1)
''')
            session = "0123456789abcdef" * 2
            env = {**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                   "NETRADAR_TEST_DIR": directory, "NETRADAR_SESSION_ID": session}
            launcher = subprocess.Popen(["bash", "start.sh", "--pi"], cwd=root, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while (not (root / "producer-active").exists() and launcher.poll() is None
                       and time.monotonic() < deadline):
                    time.sleep(.02)
                self.assertTrue((root / "producer-active").exists())
                subprocess.run(["bash", "start.sh", "--stop-pi", session], cwd=root, env=env,
                               check=True, timeout=8)
                launcher.communicate(timeout=3)
                self.assertEqual((root / "delivery-result").read_text(), "drained")
                self.assertFalse((root / "api-active").exists())
            finally:
                if launcher.poll() is None:
                    launcher.terminate()
                launcher.communicate(timeout=24)
