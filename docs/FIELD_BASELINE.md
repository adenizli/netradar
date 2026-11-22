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
