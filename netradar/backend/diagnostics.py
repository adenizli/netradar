"""Bounded, owner-loop diagnostics and Prometheus exposition."""
from collections import OrderedDict
import time

from netradar.metrics import STAGE_COUNTERS, _LATENCY_BOUNDS_NS

MAX_BODY_BYTES = 64 * 1024
STALE_SECONDS = 5
RETAINED_CAPTURES = 8


def validate_capture(capture):
    if capture["stages"].keys() != STAGE_COUNTERS.keys():
        raise ValueError("Invalid stage set")
    if capture["distinct_icao_windows"]["windows_seconds"].keys() != {"60", "300", "900"}:
        raise ValueError("Invalid ICAO windows")
    for name, stage in capture["stages"].items():
        counters = set(stage["counters"])
        if counters != set(STAGE_COUNTERS[name]) and not (
            name == "delivery" and counters == {"accepted", "rejected"}
        ):
            raise ValueError("Invalid counter set")
        activity = stage["last_activity_elapsed_seconds"]
        wall = stage["last_activity_unix_ms"]
        if (activity is None) != (wall is None) or (
            activity is not None and activity > capture["elapsed_seconds"]
        ):
            raise ValueError("Invalid activity time")
        if stage["counters"].get("queue_dropped_samples", 0) > stage["counters"].get("complex_samples", 0):
            raise ValueError("Dropped samples exceed acquired samples")
        if stage["status"] == "unavailable" and (any(stage["counters"].values()) or activity is not None):
            raise ValueError("Unavailable stage has measurements")
        queue = stage.get("queue")
        if queue and queue["capacity"] is not None and (
            queue["depth"] is None or queue["high_water"] is None
            or not 0 <= queue["depth"] <= queue["high_water"] <= queue["capacity"]
        ):
            raise ValueError("Invalid queue gauges")
        latency = stage["latency"]
        previous_count = 0
        bounds = [bound / 1e6 for bound in _LATENCY_BOUNDS_NS] + [None]
        if [bucket["upper_bound_ms"] for bucket in latency["buckets"]] != bounds:
            raise ValueError("Invalid histogram bounds")
        for bucket in latency["buckets"]:
            if bucket["count"] < previous_count:
                raise ValueError("Invalid histogram buckets")
            previous_count = bucket["count"]
        if previous_count != latency["sample_count"]:
            raise ValueError("Invalid histogram count")


def regressed(before, after):
    return before is not None and (after is None or after < before)


def capture_progresses(before, after):
    if (before["run_status"] != "running"
            or before["source"] != after["source"]
            or before["started_at_unix_ms"] != after["started_at_unix_ms"]
            or regressed(before["elapsed_seconds"], after["elapsed_seconds"])
            or regressed(before["capture_head_sample_index"], after["capture_head_sample_index"])
            or before["sample_rate_hz"] is not None and before["sample_rate_hz"] != after["sample_rate_hz"]):
        return False
    for name, stage in before["stages"].items():
        next_stage = after["stages"][name]
        if any(regressed(count, next_stage["counters"].get(key)) for key, count in stage["counters"].items()):
            return False
        if regressed(stage["last_activity_elapsed_seconds"], next_stage["last_activity_elapsed_seconds"]):
            return False
        latency, next_latency = stage["latency"], next_stage["latency"]
        if regressed(latency["sum_ms"], next_latency["sum_ms"]) or any(
            regressed(a["count"], b["count"]) for a, b in zip(latency["buckets"], next_latency["buckets"])
        ):
            return False
    return True


class Diagnostics:
    def __init__(self, *, clock=time.monotonic, expected_producer=False):
        self._clock = clock
        self._expected_producer = expected_producer
        self._created = clock()
        self._active = None
        self._retained = OrderedDict()
        self.accepted = 0
        self.rejected = 0

    def ingest(self, update):
        capture = update["capture"]
        validate_capture(capture)
        now = self._clock()
        if self._active is not None:
            previous, _ = self._active
            before = previous["capture"]
            if capture["capture_id"] == before["capture_id"]:
                if update["sequence"] <= previous["sequence"] or not capture_progresses(before, capture):
                    raise ValueError("Snapshot is out of order or metrics regressed")
            else:
                if (capture["capture_id"] in self._retained
                        or capture["started_at_unix_ms"] <= before["started_at_unix_ms"]):
                    raise ValueError("Capture is out of order")
                self._retained[before["capture_id"]] = self._summary(before)
                if len(self._retained) > RETAINED_CAPTURES:
                    self._retained.popitem(last=False)
        self._active = update, now

    @staticmethod
    def _summary(capture):
        return {key: capture[key] for key in ("capture_id", "source", "run_status", "started_at_unix_ms")}

    def _receiver_state(self, capture, age):
        if capture is None:
            if not self._expected_producer:
                return "unknown"
            return "starting" if self._clock() - self._created < STALE_SECONDS else "receiver_down"
        status = capture["run_status"]
        if status in ("failed", "interrupted"):
            return "receiver_down"
        if status == "completed":
            return "completed"
        if age >= STALE_SECONDS:
            return "telemetry_stale"
        iq = capture["stages"]["acquisition"]
        crc = capture["stages"]["decoder"]
        if iq["status"] == "unavailable":
            return "starting" if capture["elapsed_seconds"] + age < STALE_SECONDS else "unknown"
        if not self._activity_is_recent(iq, capture, age):
            return "receiver_silent"
        return "receiving" if self._activity_is_recent(crc, capture, age) else "no_valid_traffic"

    @staticmethod
    def _activity_is_recent(stage, capture, age):
        last = stage["last_activity_elapsed_seconds"]
        return last is not None and capture["elapsed_seconds"] - last + age < STALE_SECONDS

    def report(self, backend_resources, subscriber_count):
        age = capture = producer = last_iq = last_crc = None
        queue = {"depth": None, "capacity": None, "high_watermark": None, "drop_fraction": None}
        if self._active is not None:
            update, received = self._active
            age = self._clock() - received
            capture, producer = update["capture"], update["resources"]
            iq = capture["stages"]["acquisition"]
            crc = capture["stages"]["decoder"]
            last_iq = iq["last_activity_unix_ms"]
            last_crc = crc["last_activity_unix_ms"]
            if gauges := iq.get("queue"):
                queue.update(depth=gauges["depth"], capacity=gauges["capacity"], high_watermark=gauges["high_water"])
            acquired = iq["counters"]["complex_samples"]
            if iq["status"] == "measured" and acquired and queue["capacity"] is not None:
                queue["drop_fraction"] = iq["counters"]["queue_dropped_samples"] / acquired
        return {
            "schema_version": "1.0",
            "receiver_state": self._receiver_state(capture, age),
            "heartbeat_age_seconds": age,
            "last_iq_at_unix_ms": last_iq,
            "last_valid_crc_at_unix_ms": last_crc,
            "capture": capture,
            "resources": {"producer": producer, "backend": backend_resources},
            "queue": queue,
            "backend": {"accepted": self.accepted, "rejected": self.rejected, "subscriber_count": subscriber_count},
            "retained_captures": list(reversed(self._retained.values())),
        }


def prometheus(report):
    lines = []
    defined = set()

    def metric(name, value, kind="gauge", labels=""):
        if value is not None:
            if name not in defined:
                lines.append(f"# TYPE netradar_{name} {kind}")
                defined.add(name)
            lines.append(f"netradar_{name}{labels} {value}")

    for status in ("accepted", "rejected"):
        metric(f"backend_events_{status}_total", report["backend"][status], "counter")
    metric("backend_subscribers", report["backend"]["subscriber_count"])
    metric("producer_heartbeat_age_seconds", report["heartbeat_age_seconds"])
    for process, resources in report["resources"].items():
        if resources is None:
            continue
        metric(f"{process}_cpu_seconds_total", resources["cpu_seconds"], "counter")
        for name in ("rss_bytes", "peak_rss_bytes", "temperature_celsius", "throttling_mask"):
            metric(f"{process}_{name}", resources.get(name))
    state = report["receiver_state"]
    metric("receiver_state", 1, labels=f'{{state="{state}"}}')
    capture = report["capture"]
    if capture is not None:
        source = capture["source"]
        labels = f'{{source="{source}"}}'
        metric("capture_elapsed_seconds", capture["elapsed_seconds"], labels=labels)
        metric("sample_rate_hertz", capture["sample_rate_hz"], labels=labels)
        windows = capture["distinct_icao_windows"]
        if windows["status"] == "measured":
            for seconds, window in windows["windows_seconds"].items():
                window_labels = f'{{source="{source}",window_seconds="{seconds}"}}'
                metric("distinct_icao", window["count"], labels=window_labels)
                metric("distinct_icao_lower_bound", int(window["lower_bound"]), labels=window_labels)
        for name in ("last_iq_at_unix_ms", "last_valid_crc_at_unix_ms"):
            if (value := report[name]) is not None:
                metric(name.removesuffix("_at_unix_ms") + "_timestamp_seconds", value / 1000, labels=labels)
        for key, value in report["queue"].items():
            metric(f"acquisition_queue_{key}", value, labels=labels)
        for name, stage in capture["stages"].items():
            if stage["status"] != "measured":
                continue
            for key, value in stage["counters"].items():
                metric(f"{name}_{key}_total", value, "counter", labels)
            for key in ("samples_per_second", "messages_per_second", "transitions_per_second"):
                metric(f"{name}_{key}", stage["throughput"][key], labels=labels)
            latency = stage["latency"]
            if latency["status"] == "measured":
                base = f"netradar_{name}_latency_seconds"
                lines.append(f"# TYPE {base} histogram")
                for bucket in latency["buckets"]:
                    bound = bucket["upper_bound_ms"]
                    le = "+Inf" if bound is None else str(bound / 1000)
                    lines.append(f'{base}_bucket{{source="{source}",le="{le}"}} {bucket["count"]}')
                lines.append(f'{base}_sum{labels} {latency["sum_ms"] / 1000}')
                lines.append(f'{base}_count{labels} {latency["sample_count"]}')
    return "\n".join(lines) + "\n"
