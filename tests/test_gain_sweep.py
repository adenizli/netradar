"""Gain/PPM sweep (issue #11): schedule, decision rule and an end-to-end run with a fake rtl_sdr."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import gain_sweep

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "synthetic_adsb_2msps.cu8"

RTL_TEST_OUTPUT = """Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6
[R82XX] PLL not locked!
No E4000 tuner found, aborting.
"""

# Streams the fixture at about real time (16 KiB every 4 ms) so the IQ queue never
# overflows; writes the fixture to the output file for snippet captures.
FAKE_RTL_SDR = f"""#!{sys.executable}
import sys, time
data = open({str(FIXTURE)!r}, "rb").read()
out = sys.argv[-1]
if out != "-":
    open(out, "wb").write(data)
    sys.exit(0)
for start in range(0, len(data), 16384):
    sys.stdout.buffer.write(data[start:start + 16384])
    sys.stdout.buffer.flush()
    time.sleep(0.004)
"""


def window(setting, round_index, valid_per_minute, *, dropped=0, clip=0.1):
    return {"setting": setting, "round": round_index, "score": {
        "valid_per_minute": valid_per_minute, "distinct_icao": 10, "positioned_icao": 5,
        "dropped_samples": dropped, "clip_pct": clip,
    }}


def decide(windows, baseline="gain=33.8 ppm=0"):
    return gain_sweep.summarize(windows, baseline, max_clip_pct=1.0, min_improvement_pct=10.0)


class SupportedGainsTest(unittest.TestCase):
    def test_reads_rtl_test_gain_list(self):
        gains = gain_sweep.parse_supported_gains(RTL_TEST_OUTPUT)
        self.assertEqual(len(gains), 29)
        self.assertEqual(gains[18], 33.8)

    def test_unknown_output(self):
        self.assertIsNone(gain_sweep.parse_supported_gains("usb_open error -3"))

    def test_targets_snap_to_supported_gains(self):
        gains = gain_sweep.parse_supported_gains(RTL_TEST_OUTPUT)
        self.assertEqual(gain_sweep.snap(gain_sweep.DEFAULT_TARGETS, gains),
                         [22.9, 29.7, 33.8, 40.2, 44.5, 49.6])


class ScheduleTest(unittest.TestCase):
    def test_every_round_measures_every_setting_once(self):
        settings = [("auto", 0), (28.0, 0), (33.8, 0), (40.2, 0)]
        order = gain_sweep.schedule(settings, rounds=3, seed=1)
        self.assertEqual(len(order), 12)
        for round_index in range(3):
            self.assertCountEqual([s for r, s in order if r == round_index], settings)
        for (_, a), (_, b) in zip(order, order[1:]):
            self.assertNotEqual(a, b)
        self.assertEqual(order, gain_sweep.schedule(settings, rounds=3, seed=1))


class DecisionTest(unittest.TestCase):
    def test_clear_winner_in_every_round_is_recommended(self):
        result = decide([
            window("gain=33.8 ppm=0", 0, 100), window("gain=40.2 ppm=0", 0, 130),
            window("gain=33.8 ppm=0", 1, 80), window("gain=40.2 ppm=0", 1, 100),
        ])["decision"]
        self.assertTrue(result["changed"])
        self.assertEqual(result["recommended"], "gain=40.2 ppm=0")

    def test_higher_median_that_loses_one_round_keeps_the_baseline(self):
        result = decide([
            window("gain=33.8 ppm=0", 0, 100), window("gain=40.2 ppm=0", 0, 200),
            window("gain=33.8 ppm=0", 1, 100), window("gain=40.2 ppm=0", 1, 95),
        ])["decision"]
        self.assertFalse(result["changed"])
        self.assertEqual(result["best_median"], "gain=40.2 ppm=0")

    def test_one_round_is_never_enough_to_change(self):
        result = decide([window("gain=33.8 ppm=0", 0, 100), window("gain=40.2 ppm=0", 0, 300)])
        self.assertFalse(result["decision"]["changed"])

    def test_dropped_samples_or_clipping_make_a_setting_ineligible(self):
        result = decide([
            window("gain=33.8 ppm=0", 0, 100), window("gain=33.8 ppm=0", 1, 100),
            window("gain=49.6 ppm=0", 0, 300, clip=4.0), window("gain=49.6 ppm=0", 1, 300, clip=3.0),
            window("gain=44.5 ppm=0", 0, 300, dropped=8192), window("gain=44.5 ppm=0", 1, 300),
        ])
        settings = result["settings"]
        self.assertFalse(settings["gain=49.6 ppm=0"]["eligible"])
        self.assertIn("clipping above 1%", settings["gain=49.6 ppm=0"]["ineligible_reasons"])
        self.assertFalse(settings["gain=44.5 ppm=0"]["eligible"])
        self.assertEqual(result["decision"]["recommended"], "gain=33.8 ppm=0")

    def test_ineligible_baseline_falls_back_to_best_eligible(self):
        result = decide([
            window("gain=33.8 ppm=0", 0, 100, dropped=1), window("gain=33.8 ppm=0", 1, 100),
            window("gain=28 ppm=0", 0, 90), window("gain=28 ppm=0", 1, 90),
        ])["decision"]
        self.assertEqual(result["recommended"], "gain=28 ppm=0")
        self.assertTrue(result["changed"])

    def test_failed_windows_are_ignored(self):
        windows = [window("gain=33.8 ppm=0", 0, 100), window("gain=33.8 ppm=0", 1, 100),
                   {"setting": "gain=40.2 ppm=0", "round": 0, "score": None}]
        self.assertNotIn("gain=40.2 ppm=0", decide(windows)["settings"])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="netradar-sweep-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "rtl_sdr"
        self.fake.write_text(FAKE_RTL_SDR)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def run_sweep(self, *extra):
        env = {**os.environ, "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(
            [sys.executable, "-m", "tools.gain_sweep", "--gains", "auto", "33.8",
             "--rounds", "2", "--seed", "3", "--window-seconds", "0.28", "--snippet-seconds", "0.1",
             "--source", "synthetic", "--rtl-sdr-command", str(self.fake),
             "--output-dir", str(self.tmp / "out"), *extra],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
        )

    def test_dry_run_prints_the_schedule_without_touching_the_dongle(self):
        result = self.run_sweep("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 4)
        self.assertFalse((self.tmp / "out").exists())

    def test_sweep_writes_report_with_valid_frames_and_keeps_tied_baseline(self):
        result = self.run_sweep()
        self.assertEqual(result.returncode, 0, result.stderr)
        [run_dir] = list((self.tmp / "out").iterdir())
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(len(report["windows"]), 4)
        for item in report["windows"]:
            score = item["score"]
            self.assertEqual(score["valid_frames"], 7)
            self.assertEqual(score["distinct_icao"], 5)
            self.assertEqual(score["positioned_icao"], 2)
            self.assertEqual(score["dropped_samples"], 0)
            self.assertEqual(score["clip_pct"], 0.0)
        decision = report["summary"]["decision"]
        self.assertEqual(decision["recommended"], "gain=33.8 ppm=0")
        self.assertFalse(decision["changed"])
        self.assertIn("baseline kept", (run_dir / "report.md").read_text())
        self.assertEqual(list(run_dir.glob("*/snippet.cu8")), [])  # snippets removed by default


if __name__ == "__main__":
    unittest.main()
