# Live diagnostics

`./start.sh` enables diagnostics for its live RTL-SDR producer. A manually
started pipeline can use `--diagnostics --backend-url http://127.0.0.1:8000`.
Keep the backend bound to loopback and use the launcher's SSH tunnel for
access from the Mac. If the SDR producer exits unexpectedly, the launcher
keeps the UI and API running for inspection until Ctrl+C. The telemetry POST
accepts JSON from loopback
connections without browser origin, fetch, or forwarding headers.

`GET /api/diagnostics` returns schema version `1.0` and the latest producer
snapshot. Poll at most once per second. Treat a failed HTTP request or
unreachable API as backend unavailable.

| `receiver_state` | Meaning |
| --- | --- |
| `unknown` | No producer is configured or the producer has no IQ measurement. |
| `starting` | The backend expects its first heartbeat or the capture is initializing. |
| `receiver_down` | A failed or interrupted capture reported its terminal state, or an expected producer never registered. |
| `telemetry_stale` | The last running producer has sent no heartbeat for five seconds. A killed producer can leave this state until the next capture starts. |
| `receiver_silent` | A live producer with IQ instrumentation has received no recent IQ bytes. |
| `no_valid_traffic` | Recent IQ has arrived without a recent CRC-valid frame. |
| `receiving` | IQ and CRC-valid activity were both observed in the last five seconds. |
| `completed` | The capture ended normally. |

`heartbeat_age_seconds` comes from backend monotonic receipt time and is null
before the first heartbeat. Activity recency uses the producer's relative
monotonic activity time plus the age of the heartbeat.
`last_iq_at_unix_ms` and `last_valid_crc_at_unix_ms` are producer wall-clock
timestamps in milliseconds and stay null until the respective activity occurs.
They remain at the last observed value during silence, including when the
producer continues to send heartbeats.

`capture` is the latest [loss-funnel report](METRICS.md) or null. Its throughput
covers the whole capture. Calculate interval rates from successive counter and
elapsed-time differences within the same capture ID. A measured zero represents
an observed stage with no counted activity. Rates and latency percentiles may
be null when unavailable. Acquisition, DSP and decoder counters describe the
capture, with `source` identifying RF, replay or synthetic input.
`backend.accepted` and `backend.rejected` count aircraft ingestion HTTP outcomes
over the backend process lifetime, independently of producer delivery attempts.

`queue` exposes nullable depth, capacity and high watermark in chunks and the
capture-wide fraction of acquired samples discarded by the live queue.
`resources.producer` is null before the first heartbeat. Producer and backend
resource reports include cumulative process CPU seconds, current and peak RSS
bytes, host temperature in Celsius and the Pi throttling bit mask where
available. Derive process CPU utilization by dividing a CPU-seconds delta by
the matching elapsed-time delta. Current RSS requires Linux `/proc`. Temperature
requires a readable Linux thermal zone. Throttling is currently unavailable.
A missing measurement is null.

A producer starts a new session with a new capture ID. The backend holds one
full snapshot plus metadata for the previous eight captures. Starting a newer
capture resets the displayed counters and timestamps. The backend rejects
out-of-order updates, counter regression and writes to a finished capture.
Restarting the backend clears retention. The producer sends its current
snapshot once per second even while aircraft traffic is idle, with a final
best-effort update on graceful shutdown. Network unavailability coalesces
diagnostics to the next snapshot and does not change aircraft event delivery.

`GET /metrics` returns Prometheus text format 0.0.4. Capture metrics use only the
finite `source` label and can reset with a new capture. Histograms use seconds
and cumulative buckets, including `+Inf`. Activity timestamps use Unix seconds.
Unavailable measurements are omitted. Receiver state uses a finite `state`
label. Neither export contains raw IQ, individual ICAO addresses or
Prometheus labels for capture IDs.

Telemetry overhead on Raspberry Pi hardware has not been measured here.
