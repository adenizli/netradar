# Real-air field baseline

Run on the station with an RTL-SDR dongle, the DSP dependencies and a reachable
NetRadar FastAPI backend. Give the baseline exclusive use of the dongle for the
measurement window. Keep the antenna, cable, gain and PPM fixed during the run.
Use the same duration and record one change at a time for field comparisons.

```bash
.venv/bin/python -m tools.field_baseline --minutes 10 \
  --backend-url http://127.0.0.1:8000 --gain-db 35 --ppm 0 \
  --site /path/to/private-site.json
```

The tool accepts 10–30 minutes at 1090 MHz and 2 MS/s. It relays the same stream
to the production decoder and saves up to two contiguous seconds of raw CU8
every capture minute. Each snippet uses 8 MB, so a 30-minute run uses up to
240 MB of IQ. The remaining files are much smaller. The pipeline's metrics and
backend aircraft snapshots are sampled every five seconds. The tool checks the
backend health before acquiring RF and saves any polling failures.
The relay and pipeline enforce the sample limit in Python, which keeps a
30-minute run within its requested duration even with `rtl_sdr` binaries
that use a 32-bit byte counter for the native sample-limit option.

The default output directory is `.netradar-run/field`, which Git ignores.
Use `--output-dir` for a different private destination. Use `--tuner-serial`
if USB serial auto-detection is ambiguous. The run records the OS-reported Pi
model and CPU temperature where available. Tuner diagnostics record the actual
gain selected by the driver where the driver reports it.
Add `--diagnostics` with a diagnostics-enabled backend to sample receiver state,
queue occupancy, backend ingestion and producer/backend CPU and memory data.

## Site metadata

Supply a JSON object with the fields you can verify. Unknown fields may be
omitted or set to null. Keep exact station coordinates and raw IQ private.

```json
{
  "location_description": null,
  "latitude_deg": null,
  "longitude_deg": null,
  "site_elevation_m": null,
  "antenna_model": null,
  "antenna_height_agl_m": null,
  "antenna_orientation": null,
  "placement": "indoors",
  "cable_type": null,
  "cable_length_m": null,
  "cable_description": null,
  "connectors": null,
  "filter": null,
  "lna": null
}
```

## Outputs and interpretation

- `report.json` and `report.md` contain the run conditions, source, capture ID,
  repository revision, live funnel, CU8 analysis, hourly traffic and range.
- `run.json`, `snapshots.jsonl`, and `metrics-latest.json` record run provenance,
  acquisition and delivery counters, sampled backend state, and temperature.
- `snippets/` contains the sampled CU8 files and sidecars with source, capture
  ID, sample offsets and tuning. The report calculates SHA-256 for each window.
  It uses `tools/iq_health.py` and `tools/rf_funnel.py` to assess the saved
  samples. The continuous live metrics report what the decoder accepted.
- Valid DF17/s divides live CRC-clean plus actually corrected frames by acquired
  sample duration. Backend-visible distinct ICAO counts capture-matched valid
  DF17 events. The receiver-wide distinct count is available when the final
  15-minute metrics window covers the capture. On longer runs, the report
  retains the rolling distinct counts and the backend-visible lower bound.
- Each newly observed accepted coordinate fix contributes once to range
  statistics. Fixes are sampled from the API, so positions between polls can
  be missed. Range is great-circle distance in km. The p50 is the median and
  p95 uses the nearest-rank method. Bearings are clockwise from true north
  in 30° sectors and grouped by the altitude of the observed position fix.
  Unknown altitude has its own bin. Range stays unavailable for aircraft
  without a decoded position or for stations without verified coordinates.
- Hourly rows count observed traffic within the portions of local hours
  covered by the run. The report records HTTP delivery outcomes and distinguishes
  backend visibility from measured browser display transitions.
- Rerun the saved-data analysis with `.venv/bin/python -m tools.field_report
  /path/to/capture-directory`. Check an individual file with
  `.venv/bin/python tools/iq_health.py /path/to/snippet.cu8` or
  `.venv/bin/python tools/rf_funnel.py /path/to/snippet.cu8`.

## Verify live IQ queue behavior

Run the field baseline for 30 minutes with `--diagnostics`. In the final
report check that acquired, emitted and processed complex-sample totals all
equal 3,600,000,000 at 2 MS/s. Inspect application queue drops and DSP index
gaps, and reconcile `decoded_events` with the producer delivery outcomes and
the dedicated backend's ingestion counters. Read the tuner/pipeline logs and
Pi firmware flags separately to distinguish USB or source shortages from
application overflow. The sampled read latency includes waiting on the pipe.
Use IQ queue dwell and consumer chunk latency to inspect downstream pressure.

For a controlled comparison on the Pi, replay identical clips and use the
same CPU affinity, per-process address-space limit and IQ queue byte budget
for each revision. The example pins the benchmark to cores 2 and 3 on a Pi 4
and limits each process to 1 GiB of virtual memory:

```bash
prlimit --as=1073741824 -- taskset -c 2,3 .venv/bin/python \
  tools/iq_queue_benchmark.py /path/to/revision REVISION /path/to/snippets \
  --delays 0 0.3
```

The tool reuses the first six CU8 clips, feeds them at 4 MB/s, and measures
the live pipeline with a loopback backend. It tests 8,192 × 64, 16,384 × 32 and
32,768 × 16 samples per chunk × chunks. Each queue holds up to 1 MiB of CU8.
The report includes IQ and HTTP delivery outcomes, stage p95 latency and child
CPU seconds. The paced input measures the effect of identical HTTP delays on
the queue. Use a real-air run to measure the hardware and source.

### Issue #9 results on Raspberry Pi 4

On 2026-11-22 the same indoor receiver ran at 1090 MHz, 2 MS/s, PPM 0 and
35 dB requested gain (33.8 dB reported by the tuner). Both captures used the
default 8,192-sample chunks and 64-chunk IQ queue. The `43dcf54` baseline
had synchronous POSTs. The 30-minute `243d72f` capture used the bounded live
delivery worker with one POST in flight and up to 256 pending events.

| Live measurement | Synchronous baseline | Ordered delivery worker |
| --- | ---: | ---: |
| RF sample duration | 600 s | 1,800 s |
| Wall-clock duration | 601.57 s | 1,801.38 s |
| IQ samples acquired and processed | 1,200,000,000 | 3,600,000,000 |
| Application-dropped IQ chunks / samples | 0 / 0 | 0 / 0 |
| DSP sample-index gaps | 0 | 0 |
| Maximum IQ queue depth | 16/64 | 16/64 |
| IQ queue dwell p95 | — | 0.203 ms |
| Consumer chunk p95 | — | 4.096 ms |
| DSP preamble and demodulation p95 | 4.096 ms | 4.096 ms |
| Decoder p95 | 0.323 ms | 0.406 ms |
| HTTP latency p95 | 13.004 ms | 13.004 ms |
| Decoded events accepted by backend | 1,194/1,194 | 2,894/2,894 |
| Producer mean CPU, equivalent to one core | 28.3% | 28.7% |
| Producer peak RSS | 41.4 MiB | 41.2 MiB |
| Pi temperature range | 55.0–57.9 °C | 55.0–59.4 °C |

The 30-minute run reported zero rejected, undelivered or unconfirmed events.
It recorded no sample-rate-shortfall warnings, and the producer's Pi firmware
throttling mask was zero throughout. The IQ queue drained to zero at shutdown.
The two live windows had different air traffic. The controlled test uses the
same IQ bytes so HTTP delay is the variable being measured.

The controlled run fed 24,000,000 samples per case from six clips at 4 MB/s.
It compared `43dcf54` with `7374d7a` on the same Pi and used the resource limits
above. All cases at 0 ms added delay processed every sample and delivered
24 events. Child CPU seconds before/after were 4.54/4.48, 3.71/3.89 and
3.67/3.64 in increasing chunk-size order.

| Chunk samples × capacity | Added HTTP delay | Dropped IQ samples, before → after | IQ peak, before → after | Accepted events, before → after |
| --- | ---: | ---: | ---: | ---: |
| 8,192 × 64 | 300 ms | 4,767,744 → 0 | 64/64 → 16/64 | 18 → 24 |
| 16,384 × 32 | 300 ms | 4,538,368 → 0 | 32/32 → 8/32 | 18 → 24 |
| 32,768 × 16 | 300 ms | 4,849,664 → 0 | 16/16 → 4/16 | 18 → 24 |

The default configuration at 300 ms had a consumer chunk p95 of 3.251 ms and
a POST p95 of 308.915 ms with the worker. The new delivery backlog peaked at
3 of 256 pending events in each delayed case. Keep the 8,192 × 64 defaults
validated by the sustained real-air run. The same 1 MiB IQ buffer handled
the controlled delay once POST execution moved off the IQ processing path.
