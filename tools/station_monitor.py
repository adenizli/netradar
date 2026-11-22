#!/usr/bin/env python3
"""NetRadar station monitor: log power cuts, shutdowns, internet outages and receiver state.

Runs on the Raspberry Pi as the system service netradar-monitor.service (standard library
only, system Python, so a broken venv or a half-applied update cannot stop the logging).

What it records, in Turkish, to <log dir>/olaylar.log (plus events.jsonl and rapor.md):
- how the previous run of the Pi ended: clean poweroff/reboot (with who ran it, or the power
  button) or an unclean stop: power cut, unplugged supply or a hang (software cannot tell
  these apart), with how long the Pi was down, and whether the modem was rebooting too;
- internet outages: no network/modem unreachable vs. modem up but no internet vs. DNS only;
- under-voltage and throttling reported by the Pi firmware;
- the NetRadar receiver service going down or losing IQ, and coming back.

The Pi has no real-time clock, so after a power cut its clock is wrong until NTP syncs.
Events are held with monotonic timestamps until the clock is synchronized and then written
with corrected times (or written with a warning after CLOCK_WAIT_SECONDS).

Usage:
    python3 tools/station_monitor.py --log-dir ~/netradar-logs          # run (systemd)
    python3 tools/station_monitor.py --log-dir ~/netradar-logs --report # print the report
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

INTERVAL_SECONDS = 30
DOWN_CONFIRMATIONS = 2          # consecutive failed checks before an outage is declared
CLOCK_WAIT_SECONDS = 15 * 60    # hold events this long for NTP before writing them anyway
ROUTER_UP_FAST_SECONDS = 90     # network back this soon after boot: modem stayed powered
ROUTER_UP_SLOW_SECONDS = 180    # network back this late after boot: modem was booting too
MAX_LOG_BYTES = 2 * 1024 * 1024
REPORT_DAYS = 30
INTERNET_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))
DNS_NAME = "github.com"
RECEIVER_URL = "http://127.0.0.1:8000/api/diagnostics"

NETWORK_TEXT = {
    "ok": "İnternet bağlantısı var",
    "no_route": "Ağ bağlantısı yok (Wi-Fi/kablo bağlı değil)",
    "lan_down": "Modeme ulaşılamıyor (modem kapalı veya Wi-Fi koptu)",
    "internet_down": "Modem açık ama internet yok",
    "dns_down": "İnternet var ama DNS çalışmıyor",
}
RECEIVER_TEXT = {
    "ok": "Alıcı çalışıyor",
    "no_iq": "Alıcı servisi açık ama IQ gelmiyor (dongle takılı mı?)",
    "down": "Alıcı yazılımı yanıt vermiyor veya çöktü",
    "service_down": "Alıcı servisi çalışmıyor",
}
SHUTDOWN_TEXT = {"poweroff": "kapatma", "reboot": "yeniden başlatma", "halt": "durdurma",
                 "kexec": "kexec yeniden başlatma", "unknown": "kapanış"}


# ---------------------------------------------------------------- parsing helpers

def parse_default_gateway(ip_route_output: str) -> str | None:
    match = re.search(r"^default via (\S+)", ip_route_output, re.MULTILINE)
    return match.group(1) if match else None


def parse_throttled(text: str) -> dict | None:
    """vcgencmd get_throttled -> current flags (bits 0-3)."""
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", text)
    if not match:
        return None
    value = int(match.group(1), 16)
    return {"under_voltage": bool(value & 0x1), "freq_capped": bool(value & 0x2),
            "throttled": bool(value & 0x4), "soft_temp_limit": bool(value & 0x8),
            "under_voltage_since_boot": bool(value & 0x10000), "raw": match.group(1)}


def parse_shutdown_target(list_jobs_output: str) -> str:
    for kind in ("reboot", "poweroff", "halt", "kexec"):
        if f"{kind}.target" in list_jobs_output:
            return kind
    return "unknown"


_SUDO = re.compile(r"sudo\[\d+\]:\s+(\S+)\s+:.*?(?:TTY=(\S+)\s*;.*?)?COMMAND=(\S*(?:shutdown|reboot|poweroff|halt)\b.*"
                   r"|\S*systemctl\s+(?:reboot|poweroff|halt|kexec).*)")


def parse_shutdown_initiator(journal_output: str) -> str | None:
    """Who asked for the shutdown, from the current boot's journal (latest match wins)."""
    found = None
    for line in journal_output.splitlines():
        sudo = _SUDO.search(line)
        if sudo:
            user, tty, command = sudo.groups()
            where = f", {tty}" if tty and tty != "unknown" else ""
            found = f"{user} kullanıcısı ({command.strip()}{where})"
        elif "Power key pressed" in line:
            found = "güç düğmesi"
        elif "unattended-upgrade" in line and "reboot" in line.lower():
            found = "otomatik sistem güncellemesi (unattended-upgrades)"
    return found


def human_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "bilinmiyor"
    seconds = int(round(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{days} gün" if days else "", f"{hours} sa" if hours else "",
             f"{minutes} dk" if minutes else "", f"{secs} sn" if secs and not days and not hours else ""]
    return " ".join(p for p in parts if p) or "0 sn"


def fmt_time(unix: float | None) -> str:
    return "?" if unix is None else datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- system access

def _run(command, timeout=5, env=None) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode, result.stdout + result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


class SystemProbe:
    """Reads the real system. Everything the monitor decides on comes through here."""

    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def uptime(self) -> float:
        return float(Path("/proc/uptime").read_text().split()[0])

    def clock_synced(self) -> bool:
        code, out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        return code == 0 and out.strip() == "yes"

    def network(self) -> str:
        for host, port in INTERNET_TARGETS:
            try:
                with socket.create_connection((host, port), timeout=3):
                    break
            except OSError:
                continue
        else:
            _, routes = _run(["ip", "route", "show", "default"])
            gateway = parse_default_gateway(routes)
            if gateway is None:
                return "no_route"
            code, _ = _run(["ping", "-c", "1", "-W", "2", gateway])
            return "internet_down" if code == 0 else "lan_down"
        try:
            socket.getaddrinfo(DNS_NAME, 443)
            return "ok"
        except OSError:
            return "dns_down"

    def throttled(self) -> dict | None:
        code, out = _run(["vcgencmd", "get_throttled"])
        return parse_throttled(out) if code == 0 else None

    def receiver(self) -> str | None:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        code, out = _run(["systemctl", "--user", "is-enabled", "netradar.service"], env=env)
        if code != 0:
            return None  # receiver service not installed for this user: nothing to watch
        code, out = _run(["systemctl", "--user", "is-active", "netradar.service"], env=env)
        if out.strip() != "active":
            return "service_down"
        try:
            from urllib.request import urlopen
            with urlopen(RECEIVER_URL, timeout=3) as response:
                state = json.load(response).get("receiver_state")
        except (OSError, ValueError):
            return "down"
        if state == "receiver_silent":
            return "no_iq"
        if state in ("receiver_down", "telemetry_stale", "unknown", None):
            return "down"
        return "ok"

    def system_stopping(self) -> bool:
        _, out = _run(["systemctl", "is-system-running"])
        return out.strip() == "stopping"

    def shutdown_target(self) -> str:
        return parse_shutdown_target(_run(["systemctl", "list-jobs", "--no-legend"])[1])

    def shutdown_initiator(self) -> str | None:
        return parse_shutdown_initiator(_run(["journalctl", "-b", "0", "-n", "500", "--no-pager", "-o", "short"])[1])


# ---------------------------------------------------------------- the monitor

class StationMonitor:
    def __init__(self, log_dir: Path, probe, *, clock=time.time, monotonic=time.monotonic):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.probe, self.clock, self.monotonic = probe, clock, monotonic
        self.state_path = self.dir / "state.json"
        self.pending: list[dict] = []        # events waiting for a synchronized clock
        self.started_mono = monotonic()
        self.synced = False
        self.boot_event: dict | None = None   # unclean-boot event waiting for network evidence
        self.first_network_ok_uptime: float | None = None
        self.tracks: dict[str, dict] = {}     # name -> {state, since_mono, candidate, count}
        self.state: dict = {}

    # ---- persistence
    def _load_state(self) -> dict | None:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(self.state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    def _append(self, path: Path, line: str) -> None:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---- events
    def event(self, kind: str, text: str, *, at_mono: float | None = None, **data) -> None:
        mono = self.monotonic() if at_mono is None else at_mono
        self.pending.append({"kind": kind, "text": text, "mono": mono, **data})
        self.flush()

    def flush(self, force: bool = False) -> None:
        if not self.synced and not force:
            return
        now, mono = self.clock(), self.monotonic()
        for item in self.pending:
            at = now - (mono - item.pop("mono"))
            record = {"at": round(at, 3), "at_local": fmt_time(at), "clock_synced": self.synced, **item}
            if not self.synced:
                record["text"] += " (saat doğrulanamadı, internet yoktu)"
            self._append(self.dir / "events.jsonl", json.dumps(record, ensure_ascii=False))
            self._append(self.dir / "olaylar.log", f"{record['at_local']}  {record['text']}")
        if self.pending:
            self.pending = []
            write_report(self.dir)

    # ---- lifecycle
    def start(self) -> None:
        previous = self._load_state()
        boot_id, uptime = self.probe.boot_id(), self.probe.uptime()
        self.synced = self.probe.clock_synced()
        self.state = {"boot_id": boot_id, "last_heartbeat": None, "heartbeat_synced": False,
                      "clean_stop": None}
        if previous is None:
            self.event("monitor_installed", "🟢 İzleme başladı (ilk kurulum).")
        elif previous.get("boot_id") == boot_id:
            stop = previous.get("clean_stop") or {}
            if stop.get("kind") == "service_stop":
                self.event("monitor_restarted", "🔁 İzleme servisi yeniden başlatıldı.")
            else:
                self.event("monitor_crashed", "⚠️ İzleme servisi beklenmedik şekilde yeniden başladı (Pi kapanmadı).")
        else:
            self._classify_boot(previous, uptime)
        self._heartbeat()

    def _classify_boot(self, previous: dict, uptime: float) -> None:
        stop = previous.get("clean_stop") or {}
        last = previous.get("last_heartbeat") if previous.get("heartbeat_synced") else None
        data = {"uptime_at_detection": uptime, "previous_last_heartbeat": last}
        if stop.get("kind") in SHUTDOWN_TEXT:
            down_from = stop.get("at") if stop.get("synced") else None
            self.boot_event = {"kind": "boot_after_clean_shutdown", "down_from": down_from,
                               "uptime": uptime, "shutdown": stop, **data}
        else:
            self.boot_event = {"kind": "power_loss", "down_from": last, "uptime": uptime, **data}
        # Written once the clock is synced and the network has had time to come back.

    def _emit_boot_event(self, *, force: bool = False) -> None:
        item, self.boot_event = self.boot_event, None
        boot_at = self.clock() - (self.monotonic() - self.started_mono) - item["uptime"] if self.synced else None
        down = boot_at - item["down_from"] if boot_at is not None and item["down_from"] is not None else None
        boot_text = fmt_time(boot_at) if boot_at else "bilinmiyor"
        if item["kind"] == "power_loss":
            text = (f"⚡ ELEKTRİK KESİNTİSİ veya ZORLA KAPANMA (fiş çekildi / adaptör / donma; yazılım bunları ayırt edemez). "
                    f"Son sinyal: {fmt_time(item['down_from']) if item['down_from'] else 'bilinmiyor'}, "
                    f"açılış: {boot_text}, kapalı kalma ≈ {human_duration(down)}.")
            hint = self._router_hint()
            if hint:
                text += " " + hint
        else:
            stop = item["shutdown"]
            who = stop.get("by") or "bilinmiyor"
            text = (f"🟢 Pi açıldı. Önceki kapanış düzgündü ({SHUTDOWN_TEXT[stop['kind']]}, başlatan: {who}). "
                    f"Kapalı kalma ≈ {human_duration(down)}.")
        self.event(item["kind"], text, boot_at=boot_at, down_seconds=down,
                   **{k: v for k, v in item.items() if k not in ("kind", "uptime")})

    def _router_hint(self) -> str | None:
        up = self.first_network_ok_uptime
        if up is None:
            return "Açılıştan sonra uzun süre internet gelmedi; modem/elektrik durumu belirsiz."
        if up <= ROUTER_UP_FAST_SECONDS:
            return "Açılışta modem zaten çalışıyordu → muhtemelen sadece Pi'nin elektriği kesildi (fiş, adaptör, priz)."
        if up >= ROUTER_UP_SLOW_SECONDS:
            return (f"İnternet açılıştan {human_duration(up)} sonra geldi; modem de yeniden başlıyordu "
                    "→ büyük ihtimalle binanın elektriği kesildi.")
        return None

    def _heartbeat(self) -> None:
        self.state["last_heartbeat"] = self.clock()
        self.state["heartbeat_synced"] = self.synced
        self._save_state()

    def _track(self, name: str, value, texts: dict, *, ok_value="ok", emoji_down="🔴", emoji_up="🟢") -> None:
        """Debounced state tracking: an outage needs DOWN_CONFIRMATIONS bad checks in a row."""
        if value is None:
            return
        now = self.monotonic()
        track = self.tracks.get(name)
        if track is None:
            self.tracks[name] = {"state": value, "since": now, "candidate": None, "count": 0, "first": now}
            if value != ok_value:
                self.event(f"{name}_down", f"{emoji_down} {texts[value]} (izleme başladığında).", state=value)
            return
        if value == track["state"]:
            track["candidate"], track["count"] = None, 0
            return
        if value != track["candidate"]:
            track["candidate"], track["count"], track["first"] = value, 0, now
        track["count"] += 1
        needed = 1 if value == ok_value else DOWN_CONFIRMATIONS
        if track["count"] < needed:
            return
        previous, lasted, changed_at = track["state"], track["first"] - track["since"], track["first"]
        track.update(state=value, since=changed_at, candidate=None, count=0)
        # Timestamp the change at the first check that saw it, not at the confirming one.
        if value == ok_value:
            self.event(f"{name}_up", f"{emoji_up} {texts[value]} — kesinti süresi {human_duration(lasted)} "
                       f"(önceki durum: {texts[previous]}).", at_mono=changed_at,
                       state=value, previous=previous, down_seconds=lasted)
        else:
            self.event(f"{name}_down", f"{emoji_down} {texts[value]}.", at_mono=changed_at,
                       state=value, previous=previous)

    def tick(self) -> None:
        if not self.synced and self.probe.clock_synced():
            self.synced = True
        network = self.probe.network()
        if network == "ok" and self.first_network_ok_uptime is None:
            self.first_network_ok_uptime = self.probe.uptime()
        self._track("network", network, NETWORK_TEXT)
        flags = self.probe.throttled()
        if flags is not None:
            self._track("voltage", "low" if flags["under_voltage"] else "ok",
                        {"ok": "Besleme voltajı normale döndü", "low": "DÜŞÜK VOLTAJ: adaptör/kablo yetersiz"},
                        emoji_down="🪫")
            slowed = flags["throttled"] or flags["freq_capped"] or flags["soft_temp_limit"]
            self._track("throttle", "slow" if slowed else "ok",
                        {"ok": "İşlemci yavaşlatması bitti", "slow": "İşlemci yavaşlatıldı (ısı veya voltaj)"},
                        emoji_down="🌡️")
        self._track("receiver", self.probe.receiver(), RECEIVER_TEXT)
        waited = self.monotonic() - self.started_mono
        if self.boot_event is not None:
            network_settled = (self.first_network_ok_uptime is not None
                               or self.probe.uptime() > ROUTER_UP_SLOW_SECONDS + 60)
            if (self.synced and network_settled) or waited >= CLOCK_WAIT_SECONDS:
                self._emit_boot_event()
        if self.synced:
            self.flush()
        elif waited >= CLOCK_WAIT_SECONDS:
            self.flush(force=True)
        self._heartbeat()

    def stop(self, *, code_updated: bool = False) -> None:
        """Called on SIGTERM: record whether the whole Pi is shutting down, and who asked."""
        if self.boot_event is not None:
            self._emit_boot_event()  # do not lose the boot record if we stop before it was written
        if code_updated:
            self.state["clean_stop"] = {"kind": "service_stop", "at": self.clock(), "synced": self.synced}
            self.event("monitor_updated", "🔄 İzleme servisi yeni sürüme geçiyor.")
        elif self.probe.system_stopping():
            kind = self.probe.shutdown_target()
            by = self.probe.shutdown_initiator()
            self.state["clean_stop"] = {"kind": kind, "by": by, "at": self.clock(), "synced": self.synced}
            self.event("shutdown", f"🔌 Pi düzgün şekilde kapanıyor: {SHUTDOWN_TEXT[kind]}, "
                       f"başlatan: {by or 'bilinmiyor'}.", shutdown=kind, by=by)
        else:
            self.state["clean_stop"] = {"kind": "service_stop", "at": self.clock(), "synced": self.synced}
            self.event("monitor_stopped", "⏹️ İzleme servisi durduruldu (Pi açık kalıyor).")
        self.flush(force=True)
        self.state["last_heartbeat"] = self.clock()
        self.state["heartbeat_synced"] = self.synced
        self._save_state()


# ---------------------------------------------------------------- report

def load_events(log_dir: Path) -> list[dict]:
    events = []
    for name in ("events.jsonl.1", "events.jsonl"):
        path = Path(log_dir) / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return events


def build_report(events: list[dict], *, now: float | None = None, days: int = REPORT_DAYS) -> str:
    now = time.time() if now is None else now
    since = now - days * 86400
    recent = [e for e in events if e.get("at", 0) >= since]

    def total(kind):
        items = [e for e in recent if e["kind"] == kind]
        known = [e["down_seconds"] for e in items if isinstance(e.get("down_seconds"), (int, float))]
        return len(items), sum(known), len(items) - len(known)

    power, power_down, power_unknown = total("power_loss")
    clean, clean_down, _ = total("boot_after_clean_shutdown")
    net, net_down, _ = total("network_up")
    volt = sum(e["kind"] == "voltage_down" for e in recent)
    receiver, receiver_down, _ = total("receiver_up")
    lines = [
        "# NetRadar istasyon raporu", "",
        f"Oluşturulma: {fmt_time(now)} · son {days} gün", "",
        "| Olay | Sayı | Toplam süre |", "|---|---:|---:|",
        f"| ⚡ Elektrik kesintisi / zorla kapanma | {power} | {human_duration(power_down)}"
        + (f" (+{power_unknown} süresi bilinmeyen)" if power_unknown else "") + " |",
        f"| 🔌 Düzgün kapatma / yeniden başlatma | {clean} | {human_duration(clean_down)} |",
        f"| 🌐 İnternet kesintisi (bitmiş) | {net} | {human_duration(net_down)} |",
        f"| 🪫 Düşük voltaj uyarısı | {volt} | |",
        f"| 📡 Alıcı kesintisi (bitmiş) | {receiver} | {human_duration(receiver_down)} |",
        "", "## Son olaylar", "",
    ]
    lines += [f"- {e['at_local']}  {e['text']}" for e in recent[-40:][::-1]] or ["- Kayıtlı olay yok."]
    lines += ["", "Elektrik kesintisi ile fişin çekilmesi veya Pi'nin donması yazılımla ayırt edilemez.",
              "Tüm kayıtlar: olaylar.log (okunur) ve events.jsonl (makine için).", ""]
    return "\n".join(lines)


def write_report(log_dir: Path) -> None:
    path = Path(log_dir) / "rapor.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(build_report(load_events(log_dir)), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, default=Path.home() / "netradar-logs")
    parser.add_argument("--report", action="store_true", help="Print the report and exit")
    parser.add_argument("--days", type=int, default=REPORT_DAYS)
    parser.add_argument("--interval", type=float, default=INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    if args.report:
        print(build_report(load_events(args.log_dir), days=args.days))
        return 0
    monitor = StationMonitor(args.log_dir, SystemProbe())
    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    own_file = Path(__file__).resolve()
    own_mtime = own_file.stat().st_mtime
    code_updated = False
    monitor.start()
    while not stopping:
        deadline = time.monotonic() + args.interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.5)
        if stopping:
            break
        try:
            monitor.tick()
        except Exception as exc:  # keep logging even if one probe misbehaves
            print(f"station_monitor: check failed: {exc!r}", file=sys.stderr, flush=True)
        try:
            # The auto-updater cannot restart this system service; exit so systemd restarts
            # it (Restart=always) with the new code.
            code_updated = own_file.stat().st_mtime != own_mtime
        except OSError:
            code_updated = False
        stopping = stopping or code_updated
    monitor.stop(code_updated=code_updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
