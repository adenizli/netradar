#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p fixtures
OUT="${1:-fixtures/real_1090_2msps.cu8}"
SAMPLES=10000000
echo "Recording 5 seconds of LIVE RTL-SDR IQ to $OUT (requires access to dongle)."
echo "Parameters: 1090 MHz, 2 MS/s, 35 dB requested gain, unsigned 8-bit IQ."
rtl_sdr -f 1090000000 -s 2000000 -g 35 -n "$SAMPLES" "$OUT"
python3 - "$OUT" <<'PY2'
import json, pathlib, sys
path=pathlib.Path(sys.argv[1]); total=path.stat().st_size
metadata={"kind":"real_receiver_capture", "file":path.name,
  "center_frequency_hz":1090000000, "sample_rate_hz":2000000,
  "sample_format":"cu8_interleaved_iq", "requested_gain_db":35,
  "file_size_bytes":total, "complex_samples":total//2,
  "duration_seconds":(total//2)/2000000,
  "absolute_capture_time":"not_recorded_by_raw_format"}
path.with_suffix('.json').write_text(json.dumps(metadata, indent=2)+'\n')
print('Wrote',path.with_suffix('.json'))
PY2
