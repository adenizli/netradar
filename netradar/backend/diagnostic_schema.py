"""Telemetry wire schema. Field sets are explicit so arbitrary labels cannot be retained."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


UInt = Annotated[int, Field(ge=0, le=2**63 - 1)]
Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]
Number = Annotated[float, Field(ge=0)]
Status = Literal["measured", "unavailable"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Resources(StrictModel):
    cpu_seconds: Number
    peak_rss_bytes: UInt
    rss_bytes: UInt | None = None
    temperature_celsius: float | None
    throttling_mask: UInt | None


class Throughput(StrictModel):
    status: Literal["derived", "unavailable"]
    timebase: Literal["monotonic_capture_elapsed"]
    samples_per_second: Number | None
    messages_per_second: Number | None
    transitions_per_second: Number | None


class Bucket(StrictModel):
    upper_bound_ms: Number | None
    count: UInt


class Latency(StrictModel):
    status: Status
    unit: Literal["milliseconds"]
    percentile_method: Literal["histogram_upper_bound"]
    sample_count: UInt
    sum_ms: Number
    p50_ms: Number | None
    p95_ms: Number | None
    p99_ms: Number | None
    buckets: Annotated[list[Bucket], Field(min_length=1, max_length=128)]


class Queue(StrictModel):
    depth: UInt | None
    high_water: UInt | None
    capacity: Positive | None


class Stage(StrictModel):
    status: Status
    counters: dict[str, UInt]
    throughput: Throughput
    latency: Latency
    last_activity_elapsed_seconds: Number | None
    last_activity_unix_ms: UInt | None
    queue: Queue | None = None
    queue_dwell_latency: Latency | None = None
    consumer_chunk_latency: Latency | None = None


class Window(StrictModel):
    count: UInt | None
    lower_bound: bool


class ICAOWindows(StrictModel):
    status: Status
    timebase: Literal["capture_samples"]
    capacity: Positive
    windows_seconds: dict[Literal["60", "300", "900"], Window]


class Capture(StrictModel):
    schema_version: Literal["1.0"]
    capture_id: Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")]
    source: Literal["rf", "replay", "synthetic"]
    run_status: Literal["running", "completed", "interrupted", "failed"]
    started_at_unix_ms: UInt
    reported_at_unix_ms: UInt
    elapsed_seconds: Number
    sample_rate_hz: Positive | None
    capture_head_sample_index: UInt | None
    distinct_icao_windows: ICAOWindows
    stages: dict[str, Stage]


class Telemetry(StrictModel):
    sequence: Positive
    capture: Capture
    resources: Resources
