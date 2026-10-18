# Contracts v1 -- FROZEN FOR SPRINT

## General

- Runtime: Python 3.10+ for the first sprint; external serialization is JSON.
- All JSON keys use `snake_case` except fixed `type` discriminator.
- ICAO and raw frame hex are **uppercase without `0x`**.
- Missing derived values must be JSON `null`, **not zero**.
- Capture sample indices are zero-based and monotonic within one capture; units = complex IQ pairs.
- Absolute wall-clock timestamps are only included if measured; a raw CU8 file does not contain timestamps.
- One 112-bit DF17 frame is 28 hexadecimal characters, including its 24-bit parity.

## A. Acquisition -> DSP

`netradar.contracts.IqChunk`

```python
IqChunk(
    data=b'\x7f\x7f...',      # bytes: [I0,Q0,I1,Q1,...] unsigned 8 bit
    start_sample_index=0,     # complex sample index from beginning of stream
    sample_rate_hz=2_000_000,
    center_frequency_hz=1_090_000_000,
)
```

A 2 MS/s input uses 0.5 us per sample. For unsigned RTL-SDR CU8, I and Q are each
0..255 with zero nominally near 127.5. Remove offset when calculating magnitude:
`mag2 = (I-127.5)**2 + (Q-127.5)**2`.

Chunking: the preamble takes 16 complex samples and a long payload 224 samples,
**240 complex samples total**. Arda must handle a message crossing a chunk boundary
(e.g., preserve the last 239 samples, and prevent duplicate detections).

## B. DSP -> Decoder

`netradar.contracts.ModeSFrame`

```python
ModeSFrame(
    hex="8D40621D58C382D690C8AC2863A7",
    sample_index=10000,    # preamble begins here
    source="rf"            # or "replay", "synthetic"
)
```

DSP does NOT decode ICAO, position, callsign, etc. It estimates frame start and bytes.
CRC verification belongs to decoder, although DSP may expose confidence metadata later.
Initial scope: **112-bit DF17**. Other frames are out of MVP scope.

## C. Decoder -> Backend

`netradar.contracts.DecodedEvent`

```json
{
  "icao": "40621D",
  "event_type": "airborne_position",
  "fields": {
    "altitude_ft": 38000,
    "cpr_format": "even",
    "cpr_lat": 93000,
    "cpr_lon": 51372,
    "latitude": null,
    "longitude": null
  },
  "raw_frame": "8D40621D58C382D690C8AC2863A7",
  "observed_at_unix_ms": null
}
```

This is an **illustration of shape**; numbers in this example are NOT an asserted
validated decoding of that frame. The decoder should emit actual values from parsing.

`event_type`: `identification`, `airborne_position`, or `airborne_velocity` in MVP.
The decoder keeps the short-lived even/odd CPR pair context per ICAO. Backend keeps
**application aircraft state** across events and removes stale aircraft.

## D. Backend -> Frontend

On connection, frontend requests `GET /api/aircraft` for a snapshot (JSON array).
Then it opens `WS /ws/aircraft` for incremental updates:

```json
{
  "type": "aircraft_update",
  "data": {
    "icao": "4CA123",
    "callsign": "DEMO123",
    "latitude": 41.1,
    "longitude": 29.06,
    "altitude_ft": 35000,
    "speed_knots": 440,
    "heading_deg": 210,
    "last_seen": "2026-10-18T12:00:00Z"
  }
}
```

Demo data is NOT live ADS-B. On real feeds, unknown values are null.
Ensure reconnect/resubscribe uses a fresh snapshot, so missed updates are recovered.

## E. One Pi vs. multiple computers

MVP: acquisition, DSP, decoder and backend in one Python process or local integration
runner. Frontend is accessed over LAN. Do not transfer continuous raw IQ to browsers:
2 MS/s x 2 bytes/sample = 4 MB/s. Use decoded events for ordinary UI;
optional short, on-demand IQ windows are for debugging only.
