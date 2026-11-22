"""Pi auto-update script against temporary git repos, with fake systemctl/curl/pip.

No Raspberry Pi, systemd or network needed: a bare repo stands in for GitHub.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "pi_update.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}

FAKE_SYSTEMCTL = """#!/bin/bash
echo "$*" >> "$FAKE/systemctl.log"
[ "$1" = "--user" ] && shift
case "$1" in
  is-active) state="$(cat "$FAKE/active")"; echo "$state"; [ "$state" = active ] ;;
  is-enabled) [ "$(cat "$FAKE/enabled")" = yes ] ;;
  restart)
    if [ -f "$PI_REPO/BROKEN" ]; then echo fail > "$FAKE/health"; else echo ok > "$FAKE/health"; fi
    echo active > "$FAKE/active" ;;
esac
"""
FAKE_CURL = """#!/bin/bash
[ "$(cat "$FAKE/health")" = ok ]
"""
FAKE_PYTHON = """#!/bin/bash
echo "$*" >> "$FAKE/pip.log"
exit "$(cat "$FAKE/pip_rc")"
"""


def _executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class PiUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="netradar-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, **GIT_ENV, "HOME": str(self.tmp)}
        self.origin = self.tmp / "origin.git"
        self.dev = self.tmp / "dev"      # a teammate pushing to "GitHub"
        self.pi = self.tmp / "pi"        # the Raspberry Pi checkout
        self.fake = self.tmp / "fake"
        self.fake.mkdir()

        self.git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        self.git(self.tmp, "clone", "--quiet", str(self.origin), str(self.dev))
        self.git(self.dev, "checkout", "--quiet", "-b", "main")
        (self.dev / "scripts").mkdir()
        shutil.copy2(UPDATER, self.dev / "scripts" / "pi_update.sh")
        self.write(self.dev, ".gitignore", ".venv/\n.netradar-run/\n")
        self.write(self.dev, "netradar/app.py", "VERSION = 1\n")
        self.write(self.dev, "docs/NOTES.md", "notes\n")
        self.write(self.dev, "requirements-dev.txt", "numpy\n")
        self.commit_and_push("initial")
        self.git(self.tmp, "clone", "--quiet", str(self.origin), str(self.pi))

        bin_dir = self.fake / "bin"
        _executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
        _executable(bin_dir / "curl", FAKE_CURL)
        _executable(self.pi / ".venv" / "bin" / "python", FAKE_PYTHON)
        self.set_state(active="active", enabled="yes", health="ok", pip_rc="0")
        self.env.update({
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE": str(self.fake), "PI_REPO": str(self.pi),
            "NETRADAR_HEALTH_TIMEOUT": "1", "NETRADAR_HEALTH_SETTLE": "0",
        })

    # helpers
    def git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def write(self, repo: Path, rel: str, content: str):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit_and_push(self, message: str) -> str:
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "--quiet", "-m", message)
        self.git(self.dev, "push", "--quiet", "origin", "main")
        return self.git(self.dev, "rev-parse", "HEAD")

    def set_state(self, **values):
        for name, value in values.items():
            (self.fake / name).write_text(value + "\n")

    def run_update(self):
        return subprocess.run(
            ["bash", str(self.pi / "scripts" / "pi_update.sh")],
            cwd=self.pi, env=self.env, capture_output=True, text=True, timeout=60,
        )

    def pi_head(self) -> str:
        return self.git(self.pi, "rev-parse", "HEAD")

    def restarts(self) -> int:
        log = self.fake / "systemctl.log"
        return log.read_text().count("restart") if log.exists() else 0

    def pip_calls(self) -> str:
        log = self.fake / "pip.log"
        return log.read_text() if log.exists() else ""

    # tests
    def test_no_new_commit_does_nothing(self):
        before = self.pi_head()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pi_head(), before)
        self.assertEqual(self.restarts(), 0)

    def test_runtime_change_is_pulled_and_service_restarted(self):
        self.write(self.dev, "netradar/app.py", "VERSION = 2\n")
        new = self.commit_and_push("runtime change")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pi_head(), new)
        self.assertEqual(self.restarts(), 1)
        self.assertIn("Güncelleme tamam", result.stdout)

    def test_docs_only_change_does_not_restart_receiver(self):
        self.write(self.dev, "docs/NOTES.md", "more notes\n")
        new = self.commit_and_push("docs")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pi_head(), new)
        self.assertEqual(self.restarts(), 0)

    def test_local_edits_on_pi_are_never_overwritten(self):
        before = self.pi_head()
        self.write(self.pi, "netradar/app.py", "VERSION = 'local experiment'\n")
        self.write(self.dev, "netradar/app.py", "VERSION = 2\n")
        self.commit_and_push("runtime change")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pi_head(), before)
        self.assertIn("local experiment", (self.pi / "netradar/app.py").read_text())
        self.assertIn("commit edilmemiş", result.stdout)
        self.assertEqual(self.restarts(), 0)

    def test_diverged_pi_branch_is_skipped(self):
        self.write(self.pi, "netradar/app.py", "VERSION = 'pi only'\n")
        self.git(self.pi, "commit", "--quiet", "-am", "pi-only commit")
        before = self.pi_head()
        self.write(self.dev, "netradar/app.py", "VERSION = 2\n")
        self.commit_and_push("runtime change")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pi_head(), before)
        self.assertIn("ayrışmış", result.stdout)

    def test_other_branch_is_skipped(self):
        self.git(self.pi, "checkout", "--quiet", "-b", "experiment")
        before = self.pi_head()
        self.write(self.dev, "netradar/app.py", "VERSION = 2\n")
        self.commit_and_push("runtime change")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pi_head(), before)
        self.assertIn("experiment", result.stdout)

    def test_broken_commit_is_rolled_back_and_not_retried(self):
        old = self.pi_head()
        self.write(self.dev, "BROKEN", "this release crashes\n")
        bad = self.commit_and_push("broken release")
        result = self.run_update()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.pi_head(), old)
        self.assertFalse((self.pi / "BROKEN").exists())
        self.assertEqual((self.pi / ".netradar-run/bad-commit").read_text().strip(), bad)
        self.assertEqual(self.restarts(), 2)  # new version, then the rollback
        self.assertEqual((self.fake / "health").read_text().strip(), "ok")

        again = self.run_update()  # same bad commit: do not try it again
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.pi_head(), old)
        self.assertEqual(self.restarts(), 2)

        self.git(self.dev, "rm", "--quiet", "BROKEN")
        fixed = self.commit_and_push("fix")
        result = self.run_update()  # a newer commit is tried as usual
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.pi_head(), fixed)
        self.assertFalse((self.pi / ".netradar-run/bad-commit").exists())

    def test_no_rollback_when_system_was_already_unhealthy(self):
        # e.g. no dongle: failing after the update is not proof the commit is bad
        self.set_state(health="fail")
        self.write(self.dev, "BROKEN", "x\n")
        new = self.commit_and_push("release while receiver is down")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.pi_head(), new)
        self.assertIn("geri alınmadı", result.stdout)

    def test_requirements_change_installs_dependencies(self):
        self.write(self.dev, "requirements-dev.txt", "numpy\nscipy\n")
        self.commit_and_push("new dependency")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-m pip install --quiet -r requirements-dev.txt", self.pip_calls())
        self.assertEqual(self.restarts(), 1)

    def test_failed_dependency_install_rolls_back(self):
        old = self.pi_head()
        self.set_state(pip_rc="1")
        self.write(self.dev, "requirements-dev.txt", "does-not-exist-pkg\n")
        self.commit_and_push("bad dependency")
        result = self.run_update()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.pi_head(), old)

    def test_manually_stopped_service_is_not_started(self):
        self.set_state(active="inactive", enabled="no", health="fail")
        self.write(self.dev, "netradar/app.py", "VERSION = 2\n")
        new = self.commit_and_push("runtime change")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.pi_head(), new)
        self.assertEqual(self.restarts(), 0)


class DeployFilesTest(unittest.TestCase):
    def test_scripts_parse(self):
        for script in ("scripts/pi_update.sh", "scripts/pi_install_service.sh"):
            subprocess.run(["bash", "-n", script], cwd=ROOT, check=True)

    def test_units_point_at_repo_scripts(self):
        units = ROOT / "deploy" / "systemd"
        service = (units / "netradar.service").read_text()
        update = (units / "netradar-update.service").read_text()
        timer = (units / "netradar-update.timer").read_text()
        self.assertIn('ExecStart=/bin/bash "@ROOT@/start.sh" --service', service)
        self.assertIn("Restart=always", service)
        self.assertIn('ExecStart=/bin/bash "@ROOT@/scripts/pi_update.sh"', update)
        self.assertIn("OnUnitActiveSec=", timer)

    @unittest.skipIf(os.uname().sysname == "Linux", "would start the real receiver")
    def test_service_mode_is_a_known_launcher_argument(self):
        result = subprocess.run(
            ["bash", "start.sh", "--service"], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Linux", result.stderr)
        self.assertNotIn("Bilinmeyen argüman", result.stderr)


if __name__ == "__main__":
    unittest.main()
