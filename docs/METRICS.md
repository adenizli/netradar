# Loss-funnel metrics v1.0

Write a complete producer report while running a bounded live capture:

```sh
.venv/bin/python -m netradar.pipeline --rtl-sdr --max-samples 120000000 \
  --backend-url http://127.0.0.1:8000 --metrics-json /tmp/netradar-rf.json \
  --metrics-interval-seconds 1
```

At 2,000,000 samples per second, the bound is 60 nominal seconds of IQ.
The JSON file is replaced atomically at startup and shutdown. The optional
interval also refreshes it during processing or idle periods. The report stays
separate from decoded event JSONL on standard output. Omit the metrics options
and diagnostics for a baseline with the collector disabled. Use
`--capture-id` to supply an ID for both the metrics and the decoded events.
Metrics IDs accept 1–96 ASCII letters, digits, underscores, periods, colons
and hyphens.

For a deterministic seven-frame capture, replace the live input options with
`--iq fixtures/synthetic_adsb_2msps.cu8`. The fixture's sidecar identifies it
as synthetic. Other saved IQ defaults to replay. Frame JSONL with metrics
requires all frames to share one source, which can be supplied by `--source`
when the input omits it. An empty replay completes with measured zeros for
acquisition, DSP and decoder. Without HTTP delivery or display instrumentation,
those stages report unavailable.

A report describes a single capture. `capture_id` matches decoded event metadata.
`source` is `rf` for live radio, `replay` for stored IQ or frames, or `synthetic`
for a simulated input. Each new capture gets a new collector and capture ID.
Compare changes across reports only within the same capture and source.

`run_status` is `running`, `completed`, `interrupted` or `failed`.
`reported_at_unix_ms` is the report's wall-clock time. `elapsed_seconds` uses a
monotonic clock from collector creation to the report or the end of the run.
`capture_head_sample_index` is the exclusive position on the capture sample
clock, including known gaps. It is derived from acquired IQ, or from the
latest input frame plus one sample when no IQ is available. It can be null until
samples or frames arrive. `sample_rate_hz` may be null for downstream collectors
without a capture sample clock.

Each stage carries `status`, `counters`, `throughput`, `latency`,
`last_activity_elapsed_seconds` and `last_activity_unix_ms`. Counters are
cumulative, monotonic integers and all keys are present at zero from the start.
A stage is `measured` when hooked up, including during a healthy empty stream.
An `unavailable` stage has no measurement in this collector. Subtract two
snapshots of the same capture for interval counts.

| Stage | Counter definitions |
| --- | --- |
| `acquisition` | `rf_bytes` read as CU8, `complex_samples` read as complete I/Q pairs, `emitted_samples` and `emitted_chunks` yielded, `queue_dropped_chunks` and `queue_dropped_samples` discarded when the live queue fills. |
| `dsp` | `samples` processed, `chunk_gaps` in input indices and their `gap_samples`, `preamble_candidates`, `df17_prefixes` among those candidates, `ppm_tied_rejects` among the DF17 prefixes, and `demodulated_frames` prepared for CRC validation. |
| `decoder` | `frames` received, `supported_df` and `unsupported_df`; `crc_clean`, `crc_corrected_one_bit` confirmed by the existing repair policy, and `crc_rejected` among supported DF; `supported_tc` and `unsupported_tc` among CRC-accepted frames; `decoded_events` emitted. `cpr_successes` and `cpr_failures` count supported position inputs with a resolved or unresolved pair, including a missing or expired counterpart. |
| `delivery` | Live HTTP counts `accepted` (successful HTTP status), `rejected` (4xx), `undelivered` (never sent), and `unconfirmed` (sent or attempted, with an unknown outcome). On shutdown these counts account for all decoded events. The synchronous replay path retains its accepted and rejected attempt counts. |
| `display` | `fresh`, `stale`, `expired` count entries into those display states, including fresh on first display. These cumulative counts can exceed the current aircraft population. |

Live acquisition exposes a `queue` object under its stage with the current
`depth`, `high_water` and `capacity` in chunks. These gauges are null for
replay and before the live queue is created. A DSP gap counts a discontinuity
between received chunks. Queue drop counters include a trailing overflow even
when no later chunk arrives to show a gap. At 2 MS/s, chunk sizes of 8192,
16384 and 32768 span 4.096, 8.192 and 16.384 ms, respectively. With the default
64 chunks the corresponding maximum IQ queue holds 1, 2 and 4 MiB of CU8
and covers approximately 0.26, 0.52 and 1.05 seconds of incoming RF.
Live HTTP exposes the same gauges under `stages.delivery.queue` in pending
events. An event in flight is tracked separately from the pending queue.

The stage activity timestamp changes on acquired bytes, processed DSP samples,
CRC-accepted decoder frames, delivery attempts, or display transitions,
respectively. Calling an empty hook or taking a snapshot does not advance it.
The Unix timestamp projects the elapsed monotonic time onto the collector's
starting wall clock.

Throughput divides the relevant counters by total monotonic elapsed seconds.
Acquisition and DSP report complex samples per second. DSP, decoder and
delivery respectively report demodulated frames, decoded events and confirmed
HTTP results per second. Display uses transitions per second. Inapplicable or
unavailable rates are null.

Latency durations are measured with a monotonic clock and reported in
milliseconds. The default timer samples the first and every 32nd operation.
Percentiles are derived upper bounds from the cumulative histogram, capped at
the longest measured duration. The histogram exposes `sample_count`,
`sum_ms` and `buckets` with inclusive `upper_bound_ms` and cumulative
`count` values. A null upper bound denotes the overflow bucket. A stage with no
timed operations has latency status `unavailable` and null percentiles.
Acquisition measures each sampled CU8 read, including time waiting for the pipe
or file. Live acquisition also reports `queue_dwell_latency` from IQ enqueue
to dequeue, and `consumer_chunk_latency` for the complete downstream work on a
yielded chunk. DSP measures active chunk processing, excluding time suspended
while the decoder and publisher run. Decoder measures the frame-to-event
operation. Live producer delivery measures the background POST through receipt
of response headers. The synchronous replay path also parses the response.
The backend `/metrics` endpoint exposes the IQ queue dwell and consumer chunk
histograms in seconds, and the delivery queue depth, capacity and high water.

`distinct_icao_windows.windows_seconds` uses keys `60`, `300` and `900`.
Each window counts ICAOs from CRC-clean or corrected DF17 frames, including
unsupported type codes, with indices at or after the window's start. The
windows advance by capture samples even during IQ silence. A frame-only stream
advances its clock when another input frame arrives. The table tracks the
most recently updated 4,096 ICAOs by default. After eviction, a window that
could omit an active ICAO sets `lower_bound: true`. No individual ICAO
addresses appear in the report.

## Measure producer CPU and memory on the Pi

With the SDR available for a dedicated benchmark, run alternating metrics-off
and metrics-on captures at the same gain, rate and chunk size:

```sh
.venv/bin/python tools/metrics_overhead.py --rtl-sdr --seconds 60 --pairs 3 \
  --gain-db 35 > /tmp/netradar-overhead.json
```

The metrics-on runs write one JSON snapshot per second. Both modes send the
same event JSONL through an in-process sink. Each run uses a fresh producer
process, starts timing after the pipeline and DSP imports, and reports CPU
seconds, wall time, peak RSS, input samples, queue drops and decoded event
count. The comparison takes the median paired CPU change per million received
samples. The target is less than 5% additional producer CPU. Event traffic
and drops can vary across adjacent live captures.

For a controlled comparison, replay the same IQ in both modes:

```sh
.venv/bin/python tools/metrics_overhead.py --iq CAPTURE.cu8 --pairs 6 \
  > /tmp/netradar-overhead.json
```

The benchmark warms the input file cache once before the first pair.
Six or more pairs also report a distribution-free median interval with at
least 95% coverage under independent paired observations. The pair values
remain in the output for inspection.
