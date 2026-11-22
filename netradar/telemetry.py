"""Best-effort, coalescing producer diagnostics outside the processing loop."""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_throttling_cache = (float("-inf"), None)


def throttling_mask():
    """Probe firmware only on the producer worker, at most once per 30 seconds."""
    global _throttling_cache
    now = time.monotonic()
    if now - _throttling_cache[0] < 30:
        return _throttling_cache[1]
    value = None
    executable = shutil.which("vcgencmd")
    if executable:
        try:
            result = subprocess.run([executable, "get_throttled"], capture_output=True,
                                    text=True, timeout=0.25, check=True)
            prefix, raw = result.stdout.strip().split("=", 1)
            if prefix == "throttled" and raw.startswith("0x"):
                parsed = int(raw, 16)
                if 0 <= parsed <= 0xFFFFFFFF:
                    value = parsed
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    _throttling_cache = now, value
    return value


def process_resources(*, include_throttling=False) -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    try:
        rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        rss = None
    try:
        temperature = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        temperature = None
    return {
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": peak_rss,
        "rss_bytes": rss,
        "temperature_celsius": temperature,
        "throttling_mask": throttling_mask() if include_throttling else None,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelemetrySender:
    """Use one background worker, with at most one HTTP snapshot in flight."""

    def __init__(self, base_url, metrics, *, interval=1.0, timeout=0.4):
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1")
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("Diagnostics backend must be a loopback HTTP URL without credentials")
        if interval < 1 or timeout <= 0:
            raise ValueError("Invalid diagnostics schedule")
        self._url = base_url.rstrip("/") + "/api/diagnostics"
        self._metrics = metrics
        self._interval = interval
        self._timeout = timeout
        self._stop = Event()
        self._worker = Thread(target=self._run, name="netradar-telemetry", daemon=True)

    def start(self):
        self._worker.start()

    def close(self):
        self._stop.set()
        self._worker.join(timeout=self._timeout * 2 + 0.1)

    def _run(self):
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        sequence = 0
        while True:
            began = time.monotonic()
            final = self._stop.is_set()
            sequence += 1
            try:
                payload = json.dumps({
                    "sequence": sequence,
                    "capture": self._metrics.snapshot(),
                    "resources": process_resources(include_throttling=True),
                }, allow_nan=False, separators=(",", ":")).encode()
                request = Request(self._url, payload, {"Content-Type": "application/json"}, method="POST")
                with opener.open(request, timeout=self._timeout):
                    pass
            except (OSError, ValueError):
                # The next heartbeat obtains fresh totals, including missed updates.
                pass
            if final:
                return
            self._stop.wait(max(0, self._interval - (time.monotonic() - began)))
