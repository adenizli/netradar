"""Single-capture loss-funnel counters and sampled latency distributions."""
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock
import time
from uuid import uuid4

SOURCES = frozenset(("rf", "replay", "synthetic"))
STAGE_COUNTERS = {
    "acquisition": (
        "rf_bytes", "complex_samples", "emitted_samples", "emitted_chunks",
        "queue_dropped_chunks", "queue_dropped_samples",
    ),
    "dsp": (
        "samples", "chunk_gaps", "gap_samples", "preamble_candidates", "df17_prefixes",
        "ppm_tied_rejects", "demodulated_frames",
    ),
    "decoder": (
        "frames", "supported_df", "unsupported_df", "crc_clean", "crc_corrected_one_bit",
        "crc_rejected", "supported_tc", "unsupported_tc", "decoded_events",
        "cpr_successes", "cpr_failures",
    ),
    "delivery": ("accepted", "rejected"),
    "display": ("fresh", "stale", "expired"),
}
_ACTIVITY_COUNTERS = {
    "acquisition": ("rf_bytes",),
    "dsp": ("samples",),
    "decoder": ("crc_clean", "crc_corrected_one_bit"),
    "delivery": STAGE_COUNTERS["delivery"],
    "display": STAGE_COUNTERS["display"],
}
_RATE_COUNTERS = {
    "acquisition": {"samples_per_second": ("complex_samples",)},
    "dsp": {"samples_per_second": ("samples",), "messages_per_second": ("demodulated_frames",)},
    "decoder": {"messages_per_second": ("decoded_events",)},
    "delivery": {"messages_per_second": STAGE_COUNTERS["delivery"]},
    "display": {"transitions_per_second": STAGE_COUNTERS["display"]},
}
_WINDOW_SECONDS = (60, 300, 900)
# About 26% spacing, 1 microsecond to ~67 seconds. One overflow bucket.
_LATENCY_BOUNDS_NS = tuple(round(1000 * 2 ** (step / 3)) for step in range(79))
_LATENCY_SAMPLE_STRIDE = 32


class _LatencyHistogram:
    def __init__(self):
        self.buckets = [0] * (len(_LATENCY_BOUNDS_NS) + 1)
        self.count = self.sum_ns = self.max_ns = 0

    def record(self, duration_ns: int) -> None:
        self.buckets[bisect_left(_LATENCY_BOUNDS_NS, duration_ns)] += 1
        self.count += 1
        self.sum_ns += duration_ns
        self.max_ns = max(self.max_ns, duration_ns)

    def snapshot(self) -> dict:
        cumulative = 0
        buckets = []
        for index, count in enumerate(self.buckets):
            cumulative += count
            upper = _LATENCY_BOUNDS_NS[index] / 1e6 if index < len(_LATENCY_BOUNDS_NS) else None
            buckets.append({"upper_bound_ms": upper, "count": cumulative})

        def percentile(percent: int) -> float | None:
            if not self.count:
                return None
            rank = (percent * self.count + 99) // 100
            for bucket in buckets:
                if bucket["count"] >= rank:
                    bound = bucket["upper_bound_ms"]
                    maximum = self.max_ns / 1e6
                    return maximum if bound is None else min(maximum, bound)

        return {
            "status": "measured" if self.count else "unavailable",
            "unit": "milliseconds",
            "percentile_method": "histogram_upper_bound",
            "sample_count": self.count,
            "sum_ms": self.sum_ns / 1e6,
            "p50_ms": percentile(50),
            "p95_ms": percentile(95),
            "p99_ms": percentile(99),
            "buckets": buckets,
        }


class FunnelMetrics:
    """Thread-safe metrics for one source and capture. Create a new one to reset."""

    def __init__(self, source: str, *, capture_id: str | None = None,
                 sample_rate_hz: int | None = None, icao_capacity: int = 4096,
                 clock_ns=time.monotonic_ns, wall_clock_ns=time.time_ns):
        if not isinstance(source, str) or source not in SOURCES:
            raise ValueError("Metrics source must be rf, replay or synthetic")
        if capture_id is not None and (not isinstance(capture_id, str) or not capture_id):
            raise ValueError("Capture identity must be a nonempty string")
        if sample_rate_hz is not None and (type(sample_rate_hz) is not int or sample_rate_hz <= 0):
            raise ValueError("Sample rate must be a positive integer")
        if type(icao_capacity) is not int or icao_capacity <= 0:
            raise ValueError("ICAO capacity must be a positive integer")
        self.source = source
        self.capture_id = capture_id if capture_id is not None else uuid4().hex
        self.sample_rate_hz = sample_rate_hz
        self._clock_ns = clock_ns
        self._wall_clock_ns = wall_clock_ns
        self._started_ns = clock_ns()
        self._started_unix_ns = wall_clock_ns()
        self._finished_ns: int | None = None
        self._run_status = "running"
        self._lock = Lock()
        self._counters = {stage: dict.fromkeys(keys, 0) for stage, keys in STAGE_COUNTERS.items()}
        self._measured: set[str] = set()
        self._last_activity_ns: dict[str, int] = {}
        self._latency = {stage: _LatencyHistogram() for stage in STAGE_COUNTERS}
        self._latency_calls = dict.fromkeys(STAGE_COUNTERS, 0)
        self._capture_head: int | None = None
        self._icao_capacity = icao_capacity
        self._last_icao_sample: OrderedDict[str, int] = OrderedDict()
        self._last_evicted_sample = -1

    def set_sample_rate(self, sample_rate_hz: int) -> None:
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise ValueError("Sample rate must be a positive integer")
        with self._lock:
            if self.sample_rate_hz is not None and self.sample_rate_hz != sample_rate_hz:
                raise ValueError("Metrics sample rate cannot change within a capture")
            self.sample_rate_hz = sample_rate_hz

    def begin_latency(self, stage: str) -> int | None:
        """Time the first and every 32nd operation per stage using a monotonic clock."""
        with self._lock:
            calls = self._latency_calls[stage]
            self._latency_calls[stage] += 1
        return self._clock_ns() if calls % _LATENCY_SAMPLE_STRIDE == 0 else None

    def elapsed_latency(self, started_ns: int | None) -> int | None:
        return None if started_ns is None else self._clock_ns() - started_ns

    def observe(self, stage: str, *, counters: Mapping[str, int] | None = None,
                duration_ns: int | None = None, head_sample_index: int | None = None,
                valid_icao: str | None = None, icao_sample_index: int | None = None) -> None:
        """Record a stage operation, or mark it measured with a zero-counter call.

        Head is the exclusive sample index. An optional CRC-valid ICAO and its
        sample index are recorded together with the corresponding decoder counters.
        """
        target = self._counters[stage]
        increments = counters.items() if counters is not None else ()
        for key, value in increments:
            if key not in target:
                raise ValueError(f"Unknown {stage} counter: {key}")
            if type(value) is not int or value < 0:
                raise ValueError("Counters require nonnegative integer increments")
        if duration_ns is not None and (type(duration_ns) is not int or duration_ns < 0):
            raise ValueError("Latency requires a nonnegative integer duration in ns")
        if head_sample_index is not None and (type(head_sample_index) is not int or head_sample_index < 0):
            raise ValueError("Capture sample index must be a nonnegative integer")
        if valid_icao is not None and (
            stage != "decoder" or type(icao_sample_index) is not int or icao_sample_index < 0
        ):
            raise ValueError("Valid ICAO observations require a decoder sample index")

        with self._lock:
            if self._finished_ns is not None:
                raise RuntimeError("Cannot record metrics for a finished capture")
            self._measured.add(stage)
            for key, value in increments:
                target[key] += value
            if counters and any(counters.get(key, 0) for key in _ACTIVITY_COUNTERS[stage]):
                self._last_activity_ns[stage] = self._clock_ns()
            if duration_ns is not None:
                self._latency[stage].record(duration_ns)
            if head_sample_index is not None:
                self._capture_head = max(self._capture_head or 0, head_sample_index)
            if valid_icao is not None:
                self._record_icao(valid_icao, icao_sample_index)

    def _record_icao(self, icao: str, sample_index: int) -> None:
        previous = self._last_icao_sample.get(icao)
        if previous is not None:
            if sample_index <= previous:
                return
            self._last_icao_sample.move_to_end(icao)
        elif len(self._last_icao_sample) >= self._icao_capacity:
            _, removed = self._last_icao_sample.popitem(last=False)
            self._last_evicted_sample = max(self._last_evicted_sample, removed)
        self._last_icao_sample[icao] = sample_index

    def finish(self, status: str = "completed") -> None:
        if status not in ("completed", "interrupted", "failed"):
            raise ValueError("Invalid terminal run status")
        with self._lock:
            if self._finished_ns is None:
                self._finished_ns = self._clock_ns()
                self._run_status = status

    def snapshot(self) -> dict:
        """Return an independent, JSON-serializable copy of the current report."""
        with self._lock:
            now_ns = self._clock_ns() if self._finished_ns is None else self._finished_ns
            elapsed = (now_ns - self._started_ns) / 1e9
            stages = {}
            for name, counters in self._counters.items():
                available = name in self._measured
                activity_ns = self._last_activity_ns.get(name)
                rates = dict.fromkeys(("samples_per_second", "messages_per_second", "transitions_per_second"))
                if available and elapsed > 0:
                    for rate, rate_counters in _RATE_COUNTERS[name].items():
                        rates[rate] = sum(counters[key] for key in rate_counters) / elapsed
                stages[name] = {
                    "status": "measured" if available else "unavailable",
                    "counters": counters.copy(),
                    "throughput": {
                        "status": "derived" if available and elapsed > 0 else "unavailable",
                        "timebase": "monotonic_capture_elapsed",
                        **rates,
                    },
                    "latency": self._latency[name].snapshot(),
                    "last_activity_elapsed_seconds": None if activity_ns is None else (
                        (activity_ns - self._started_ns) / 1e9
                    ),
                    "last_activity_unix_ms": None if activity_ns is None else (
                        (self._started_unix_ns + activity_ns - self._started_ns) // 1_000_000
                    ),
                }
            return {
                "schema_version": "1.0",
                "capture_id": self.capture_id,
                "source": self.source,
                "run_status": self._run_status,
                "started_at_unix_ms": self._started_unix_ns // 1_000_000,
                "reported_at_unix_ms": self._wall_clock_ns() // 1_000_000,
                "elapsed_seconds": elapsed,
                "sample_rate_hz": self.sample_rate_hz,
                "capture_head_sample_index": self._capture_head,
                "distinct_icao_windows": self._icao_windows(),
                "stages": stages,
            }

    def _icao_windows(self) -> dict:
        available = "decoder" in self._measured and self.sample_rate_hz is not None
        counts = {}
        for seconds in _WINDOW_SECONDS:
            cutoff = (self._capture_head or 0) - seconds * (self.sample_rate_hz or 0)
            if available:
                counts[str(seconds)] = {
                    "count": sum(index >= cutoff for index in self._last_icao_sample.values()),
                    "lower_bound": self._last_evicted_sample >= 0 and self._last_evicted_sample >= cutoff,
                }
            else:
                counts[str(seconds)] = {"count": None, "lower_bound": False}
        return {
            "status": "measured" if available else "unavailable",
            "timebase": "capture_samples",
            "capacity": self._icao_capacity,
            "windows_seconds": counts,
        }
