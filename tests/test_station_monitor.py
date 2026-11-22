"""Station monitor: power-cut/shutdown classification, outages, clock handling and report.

Uses a fake probe and fake clocks; no Raspberry Pi, systemd or network needed.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import station_monitor as sm

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_795_000_000.0  # a fixed "true" wall-clock time


class FakeProbe:
    def __init__(self, test, *, boot_id="boot-1", uptime_at_start=40.0, synced=True):
        self.test = test
        self.boot = boot_id
        self.uptime_at_start = uptime_at_start
        self.mono_at_start = test.mono
        self.synced = synced
        self.net = "ok"
        self.flags = {"under_voltage": False, "freq_capped": False, "throttled": False,
                      "soft_temp_limit": False}
        self.rx = "ok"
        self.stopping = False
        self.target = "reboot"
        self.initiator = "ahmet kullanıcısı (/usr/sbin/reboot, pts/0)"

    def boot_id(self): return self.boot
    def uptime(self): return self.uptime_at_start + (self.test.mono - self.mono_at_start)
    def clock_synced(self): return self.synced
    def network(self): return self.net
    def throttled(self): return dict(self.flags)
    def receiver(self): return self.rx
    def system_stopping(self): return self.stopping
    def shutdown_target(self): return self.target
    def shutdown_initiator(self): return self.initiator


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netradar-monitor-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mono = 1000.0
        self.wall_error = 0.0  # how wrong the Pi clock is (no RTC before NTP sync)

    def wall(self):
        return T0 + self.mono + self.wall_error

    def monitor(self, probe):
        return sm.StationMonitor(self.dir, probe, clock=self.wall, monotonic=lambda: self.mono)

    def advance(self, monitor, seconds, ticks=1):
        for _ in range(ticks):
            self.mono += seconds
            monitor.tick()

    def events(self):
        return sm.load_events(self.dir)

    def kinds(self):
        return [e["kind"] for e in self.events()]

    def log(self):
        return (self.dir / "olaylar.log").read_text(encoding="utf-8")

    def write_previous(self, **state):
        (self.dir / "state.json").write_text(json.dumps(state))

    # ---- lifecycle and boot classification
    def test_first_install(self):
        m = self.monitor(FakeProbe(self))
        m.start()
        self.assertEqual(self.kinds(), ["monitor_installed"])
        state = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(state["boot_id"], "boot-1")
        self.assertTrue((self.dir / "rapor.md").exists())

    def test_power_cut_with_modem_up_points_at_the_pi_supply(self):
        last = T0 + self.mono - 600  # last heartbeat ten minutes ago
        self.write_previous(boot_id="old", last_heartbeat=last, heartbeat_synced=True, clean_stop=None)
        probe = FakeProbe(self, boot_id="new", uptime_at_start=40)
        m = self.monitor(probe)
        m.start()
        self.assertEqual(self.kinds(), [])  # waits for the first network check
        self.advance(m, 30)
        [event] = self.events()
        self.assertEqual(event["kind"], "power_loss")
        self.assertAlmostEqual(event["down_seconds"], 560, places=3)  # boot was 40 s before start
        self.assertIn("ELEKTRİK KESİNTİSİ", event["text"])
        self.assertIn("9 dk 20 sn", event["text"])
        self.assertIn("sadece Pi'nin elektriği", event["text"])

    def test_power_cut_with_modem_rebooting_and_wrong_clock_until_ntp(self):
        last = T0 + self.mono - 3600
        self.write_previous(boot_id="old", last_heartbeat=last, heartbeat_synced=True, clean_stop=None)
        self.wall_error = -3000  # fake-hwclock restored an old time
        probe = FakeProbe(self, boot_id="new", uptime_at_start=30, synced=False)
        probe.net = "lan_down"
        m = self.monitor(probe)
        m.start()
        self.advance(m, 30, ticks=7)  # modem still booting, clock still wrong
        self.assertFalse((self.dir / "events.jsonl").exists())  # held until the clock is right
        probe.net = "ok"
        self.wall_error = 0
        probe.synced = True
        self.advance(m, 30)
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["network_down", "network_up", "power_loss"])
        down, up, power = events
        # The held event gets the corrected time of when it was observed (first check), even
        # though the clock was 3000 s wrong at that moment.
        self.assertAlmostEqual(down["at"], T0 + 1000 + 30, places=3)
        self.assertTrue(down["clock_synced"])
        self.assertIn("Modeme ulaşılamıyor", down["text"])
        self.assertIn("izleme başladığında", down["text"])
        self.assertIn("kesinti süresi", up["text"])
        self.assertAlmostEqual(power["down_seconds"], 3600 - 30, places=3)
        self.assertIn("binanın elektriği kesildi", power["text"])

    def test_clean_reboot_records_who_and_how_long(self):
        probe = FakeProbe(self, boot_id="boot-1")
        m = self.monitor(probe)
        m.start()
        probe.stopping = True
        m.stop()
        stop_event = self.events()[-1]
        self.assertEqual(stop_event["kind"], "shutdown")
        self.assertIn("yeniden başlatma", stop_event["text"])
        self.assertIn("ahmet kullanıcısı", stop_event["text"])
        # Pi comes back 2 minutes later (uptime 25 s when the monitor starts).
        self.mono += 120
        probe2 = FakeProbe(self, boot_id="boot-2", uptime_at_start=25)
        m2 = self.monitor(probe2)
        m2.start()
        self.advance(m2, 30)
        boot = self.events()[-1]
        self.assertEqual(boot["kind"], "boot_after_clean_shutdown")
        self.assertAlmostEqual(boot["down_seconds"], 95, places=3)
        self.assertIn("düzgündü", boot["text"])

    def test_service_restart_and_crash_within_one_boot(self):
        probe = FakeProbe(self)
        m = self.monitor(probe)
        m.start()
        m.stop()  # systemctl stop, Pi stays on
        self.assertEqual(self.kinds()[-1], "monitor_stopped")
        self.monitor(probe).start()
        self.assertEqual(self.kinds()[-1], "monitor_restarted")
        self.monitor(probe).start()  # previous run never recorded a stop: it crashed
        self.assertEqual(self.kinds()[-1], "monitor_crashed")

    def test_pending_boot_event_is_written_if_stopped_early(self):
        self.write_previous(boot_id="old", last_heartbeat=T0, heartbeat_synced=True, clean_stop=None)
        probe = FakeProbe(self, boot_id="new")
        m = self.monitor(probe)
        m.start()
        probe.stopping = True
        m.stop()
        self.assertEqual(self.kinds(), ["power_loss", "shutdown"])

    def test_code_update_restart_is_not_reported_as_a_crash(self):
        probe = FakeProbe(self)
        m = self.monitor(probe)
        m.start()
        m.stop(code_updated=True)
        self.monitor(probe).start()
        self.assertEqual(self.kinds()[-2:], ["monitor_updated", "monitor_restarted"])

    # ---- outages
    def test_internet_outage_needs_two_bad_checks_and_reports_duration(self):
        probe = FakeProbe(self)
        m = self.monitor(probe)
        m.start()
        self.advance(m, 30)
        probe.net = "internet_down"
        self.advance(m, 30)  # one bad check: could be a blip
        self.assertNotIn("network_down", self.kinds())
        probe.net = "ok"
        self.advance(m, 30)
        probe.net = "internet_down"
        self.advance(m, 30, ticks=2)
        self.assertEqual(self.kinds()[-1], "network_down")
        self.assertAlmostEqual(self.events()[-1]["at"], self.wall() - 30, places=3)  # first bad check
        self.assertIn("Modem açık ama internet yok", self.log())
        self.advance(m, 30, ticks=3)
        probe.net = "ok"
        self.advance(m, 30)
        up = self.events()[-1]
        self.assertEqual(up["kind"], "network_up")
        self.assertEqual(up["down_seconds"], 150)  # first bad check to first good check
        self.assertIn("2 dk 30 sn", up["text"])

    def test_under_voltage_throttling_and_receiver_events(self):
        probe = FakeProbe(self)
        m = self.monitor(probe)
        m.start()
        self.advance(m, 30)
        probe.flags["under_voltage"] = True
        probe.flags["throttled"] = True
        probe.rx = "no_iq"
        self.advance(m, 30, ticks=2)
        kinds = self.kinds()
        self.assertIn("voltage_down", kinds)
        self.assertIn("throttle_down", kinds)
        self.assertIn("receiver_down", kinds)
        self.assertIn("DÜŞÜK VOLTAJ", self.log())
        self.assertIn("IQ gelmiyor", self.log())
        probe.flags["under_voltage"] = False
        probe.flags["throttled"] = False
        probe.rx = "ok"
        self.advance(m, 30)
        self.assertIn("voltage_up", self.kinds())
        self.assertIn("receiver_up", self.kinds())

    def test_bad_state_at_monitor_start_is_logged_once(self):
        probe = FakeProbe(self)
        probe.rx = "service_down"
        m = self.monitor(probe)
        m.start()
        self.advance(m, 30, ticks=3)
        self.assertEqual(self.kinds().count("receiver_down"), 1)
        self.assertIn("izleme başladığında", self.log())

    def test_clock_never_syncs_events_are_written_with_a_warning(self):
        probe = FakeProbe(self, synced=False)
        m = self.monitor(probe)
        m.start()
        self.assertFalse((self.dir / "events.jsonl").exists())
        self.advance(m, 60, ticks=sm.CLOCK_WAIT_SECONDS // 60)
        [event] = self.events()
        self.assertFalse(event["clock_synced"])
        self.assertIn("saat doğrulanamadı", event["text"])

    def test_log_rotation(self):
        log = self.dir / "olaylar.log"
        log.write_text("x" * (sm.MAX_LOG_BYTES + 1))
        self.monitor(FakeProbe(self)).start()
        self.assertTrue((self.dir / "olaylar.log.1").exists())
        self.assertLess(log.stat().st_size, 1000)


class ParserTest(unittest.TestCase):
    def test_default_gateway(self):
        self.assertEqual(sm.parse_default_gateway("default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"),
                         "192.168.1.1")
        self.assertIsNone(sm.parse_default_gateway(""))

    def test_throttled_bits(self):
        flags = sm.parse_throttled("throttled=0x50005")
        self.assertTrue(flags["under_voltage"])
        self.assertTrue(flags["throttled"])
        self.assertTrue(flags["under_voltage_since_boot"])
        self.assertFalse(flags["soft_temp_limit"])
        self.assertIsNone(sm.parse_throttled("VCHI initialization failed"))

    def test_shutdown_target(self):
        jobs = "142 reboot.target start waiting\n150 systemd-reboot.service start waiting\n"
        self.assertEqual(sm.parse_shutdown_target(jobs), "reboot")
        self.assertEqual(sm.parse_shutdown_target("9 poweroff.target start waiting"), "poweroff")
        self.assertEqual(sm.parse_shutdown_target(""), "unknown")

    def test_shutdown_initiator_from_journal(self):
        sudo = ("Nov 22 20:01:02 netradar sudo[4242]:  ahmet : TTY=pts/0 ; PWD=/home/ahmet ; "
                "USER=root ; COMMAND=/usr/sbin/reboot")
        self.assertEqual(sm.parse_shutdown_initiator(sudo), "ahmet kullanıcısı (/usr/sbin/reboot, pts/0)")
        systemctl = ("Nov 22 20:01:02 netradar sudo[1]: netradar : TTY=pts/1 ; PWD=/ ; USER=root ; "
                     "COMMAND=/usr/bin/systemctl poweroff")
        self.assertIn("systemctl poweroff", sm.parse_shutdown_initiator(systemctl))
        button = "Nov 22 20:01:02 netradar systemd-logind[512]: Power key pressed short."
        self.assertEqual(sm.parse_shutdown_initiator(sudo + "\n" + button), "güç düğmesi")
        self.assertIsNone(sm.parse_shutdown_initiator("sudo[1]: a : TTY=pts/0 ; COMMAND=/usr/bin/apt update"))

    def test_human_duration(self):
        self.assertEqual(sm.human_duration(560), "9 dk 20 sn")
        self.assertEqual(sm.human_duration(3 * 3600 + 120), "3 sa 2 dk")
        self.assertEqual(sm.human_duration(2 * 86400 + 3600), "2 gün 1 sa")
        self.assertEqual(sm.human_duration(None), "bilinmiyor")


class ReportTest(unittest.TestCase):
    def test_report_counts_and_totals(self):
        now = T0
        events = [
            {"kind": "power_loss", "at": now - 3600, "at_local": "a", "text": "⚡ kesinti", "down_seconds": 600},
            {"kind": "power_loss", "at": now - 1800, "at_local": "b", "text": "⚡ kesinti 2", "down_seconds": None},
            {"kind": "boot_after_clean_shutdown", "at": now - 900, "at_local": "c", "text": "🟢", "down_seconds": 60},
            {"kind": "network_up", "at": now - 600, "at_local": "d", "text": "🟢 net", "down_seconds": 150},
            {"kind": "voltage_down", "at": now - 300, "at_local": "e", "text": "🪫"},
            {"kind": "power_loss", "at": now - 40 * 86400, "at_local": "old", "text": "eski", "down_seconds": 9},
        ]
        report = sm.build_report(events, now=now)
        self.assertIn("| ⚡ Elektrik kesintisi / zorla kapanma | 2 | 10 dk (+1 süresi bilinmeyen) |", report)
        self.assertIn("| 🌐 İnternet kesintisi (bitmiş) | 1 | 2 dk 30 sn |", report)
        self.assertIn("| 🪫 Düşük voltaj uyarısı | 1 |", report)
        self.assertNotIn("eski", report)
        self.assertLess(report.index("🪫"), report.index("⚡ kesinti 2"))  # newest first in the list

    def test_cli_report_with_no_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, "tools/station_monitor.py", "--log-dir", tmp, "--report"],
                                 cwd=ROOT, capture_output=True, text=True, check=True).stdout
            self.assertIn("Kayıtlı olay yok", out)


class DeployTest(unittest.TestCase):
    def test_monitor_unit_and_dropins(self):
        unit = (ROOT / "deploy/systemd/netradar-monitor.service").read_text()
        self.assertIn('ExecStart=/usr/bin/python3 "@ROOT@/tools/station_monitor.py" --log-dir "@LOGDIR@"', unit)
        self.assertIn("User=@USER@", unit)
        self.assertIn("@GROUPS_LINE@", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("Storage=persistent", (ROOT / "deploy/systemd/journald-netradar.conf").read_text())
        self.assertIn("RuntimeWatchdogSec=14s", (ROOT / "deploy/systemd/watchdog-netradar.conf").read_text())

    def test_scripts_parse_and_help_mentions_events(self):
        subprocess.run(["bash", "-n", "scripts/pi_install_service.sh"], cwd=ROOT, check=True)
        out = subprocess.run(["bash", "start.sh", "--help"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
        self.assertIn("--events", out)

    def test_monitor_runs_with_standard_library_only(self):
        source = (ROOT / "tools/station_monitor.py").read_text()
        for module in ("numpy", "fastapi", "netradar"):
            self.assertNotIn(f"import {module}", source)


if __name__ == "__main__":
    unittest.main()
