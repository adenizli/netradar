# Contracts v1.1 -- Complete decoder event delivery

Version 1.1 adds optional event metadata, JSON serialization and lossless backend
updates. Existing v1 decoder events without `schema_version` or `metadata` are
accepted by `DecodedEvent.from_dict()` / `from_json()`. Existing dataclass
constructor arguments remain valid. Consumers must distinguish the measurement
types documented below rather than treating every speed or direction alike.

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
  "schema_version": "1.1",
  "icao": "40621D",
  "event_type": "airborne_position",
  "fields": {
    "altitude_ft": 38000,
    "gnss_height_ft": null,
    "altitude_source": "barometric",
    "cpr_format": "even",
    "cpr_lat": 93000,
    "cpr_lon": 51372,
    "latitude": null,
    "longitude": null
  },
  "raw_frame": "8D40621D58C382D690C8AC2863A7",
  "observed_at_unix_ms": null,
  "metadata": {
    "df": 17,
    "type_code": 11,
    "crc_valid": true,
    "source": "synthetic",
    "sample_index": 10000,
    "sample_rate_hz": 2000000
  }
}
```

This example is the first synthetic fixture message before an even/odd pair is
available. All values come from the corresponding decoder output.

`fields` and `metadata` are extensible JSON objects. Serialization must preserve
every key and nested value, including unknown future fields, zeros, `false` and
`null`. Never rebuild a decoder event from a whitelist of UI fields.

Use `event.to_dict()` or `event.to_json()` for transport and
`DecodedEvent.from_dict()` or `DecodedEvent.from_json()` on receipt. The JSON
boundary rejects non-finite numbers. Unknown schema versions are rejected rather
than silently reinterpreted. `raw_frame` remains the complete 112-bit message.

`metadata` carries DF, Type Code, CRC result, source label, sample index and actual
sample rate. It refers to the same frame as `raw_frame` and its measurements. For
a resolved CPR pair that is the newer message, even if its counterpart arrived
late. Capture-relative sample indices do not create an absolute observation time.

Position events carry both `altitude_ft` (barometric) and `gnss_height_ft`
(geometric), with the inapplicable one set to null. Velocity events distinguish
`speed_knots`/`track_deg` (ground speed/true track) from
`airspeed_knots`/`airspeed_type` and `heading_deg`/`heading_reference` (IAS/TAS and
magnetic heading). They also include signed velocity components, subtype,
`vertical_rate_fpm` and `vertical_rate_source`. Raw CPR values are retained.
See [the complete decoder field definitions](DECODER.md).

`event_type`: `identification`, `airborne_position`, or `airborne_velocity` in MVP.
The decoder keeps the short-lived even/odd CPR pair context per ICAO and height
source. Backend keeps application aircraft state across events. Expiring stale
aircraft is a separate backend lifecycle concern, not implemented by this delivery
adapter.

## D. Backend -> Frontend

`AircraftStore.apply(event)` and `apply_json(event_json)` return this envelope:

```python
{
  "schema_version": "1.1",
  "type": "aircraft_update",
  "data": aircraft_state,
  "event": complete_decoded_event
}
```

The variable names above stand for objects, not string literals:

- `event`: the full versioned decoder event from section C, with every payload and
  metadata field. Every accepted event is delivered, even if the aircraft state
  already contains the same measurement.
- `data`: the complete current aircraft state, with flat measurement fields for UI
  convenience. Known measurements survive unrelated events with null fields.
  Real zero values update the state. A zero speed clears its undefined track.
  Related value/source pairs (airspeed/type, heading/reference, vertical rate/source)
  update together. Latitude and longitude update as a pair.
- `data.latest_events`: full latest-received events, retained separately under
  `identification`, `airborne_position:barometric`, `airborne_position:gnss`,
  `airborne_velocity:ground` and `airborne_velocity:air`. This also preserves
  fields not flattened into the UI state. It is not an unlimited event history.
- `data.position_source`: source of the most recently resolved coordinate pair.
- `data.last_seen` / `last_seen_unix_ms`: measured observation time, ISO UTC and
  Unix milliseconds. Both remain null for timestamp-free replay. Receipt time is
  not substituted for observation time.

`AircraftStore.snapshot()` returns copied aircraft objects with these complete
retained events. A subscriber can persist every update's `event` for a full history.
The `DecoderPipeline` delivers events to this store and an optional publisher
callback. Its CLI emits the entire envelope as one JSON object per line. No HTTP
or WebSocket server is started by this module.

The planned HTTP adapter should expose `snapshot()` as `GET /api/aircraft` and
publish these same envelopes on `WS /ws/aircraft`. Reconnect should fetch a fresh
snapshot. Demo fixture updates are fictional UI data and have `event: null`.

## E. One Pi vs. multiple computers

MVP: acquisition, DSP, decoder and backend in one Python process or local integration
runner. Frontend is accessed over LAN. Do not transfer continuous raw IQ to browsers:
2 MS/s x 2 bytes/sample = 4 MB/s. Use decoded events for ordinary UI;
optional short, on-demand IQ windows are for debugging only.
