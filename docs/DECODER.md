# Implemented decoder coverage

`netradar.decoder.decode.ADSBDecoder` accepts `ModeSFrame` and returns a list of
`DecodedEvent` objects. It requires Python 3.10+ and only the standard library.
CRC-invalid frames, unsupported downlink formats, and unsupported type codes
return an empty list. Event names remain unchanged. Contracts v1.1 adds optional
`metadata` and lossless event JSON serialization; the fields below describe this
decoder's payloads.

| DF17 messages | Supported information |
| --- | --- |
| TC 1-4 | Callsign |
| TC 9-18 | Q=1 and Q=0 barometric altitude, raw CPR and global position |
| TC 20-22 | Q=1 and Q=0 GNSS geometric height, raw CPR and global position |
| TC 19 subtype 1/2 | Ground velocity components, ground speed, true ground track, vertical rate |
| TC 19 subtype 3/4 | IAS/TAS airspeed, magnetic heading, vertical rate |

## Position events

- `altitude_ft`: barometric altitude in feet. It is `None` in GNSS messages.
- `gnss_height_ft`: GNSS geometric height in feet. It is `None` in barometric messages.
- `altitude_source`: `barometric` or `gnss`, identifying the message's height source.
- `cpr_format`, `cpr_lat`, `cpr_lon`: format and raw 17-bit CPR values.
- `latitude`, `longitude`: decimal degrees, or `None` without a usable pair.

The 12-bit AC field is decoded in feet for both message families. This follows
FlightAware dump1090's `decodeAC12Field` and TC20-22 geometric-height handling,
cross-checked against the VIAVI IFR6000 test-set manual. The raw AC field is not
an unsigned metre value. Q=1 uses 25-foot steps with a -1000-foot offset. Q=0 uses
Gillham coding in 100-foot steps; Q=0 does not itself mean high altitude.
Zero AC and invalid Gillham codes produce an unknown height while leaving CPR
available for position processing.

CPR messages are retained per ICAO and height source. This intentionally avoids
mixing a barometric even message with a geometric odd message. Both must be within
10 seconds, and their recovered longitude-zone counts must agree. The newer
message supplies the reported coordinates, height and `raw_frame`; an equal
sample index selects even. Impossible latitudes are rejected.

## Velocity events

- `speed_knots`: **ground speed**, available in subtype 1/2.
- `track_deg`: true ground-track bearing, available in subtype 1/2.
- `velocity_east_knots`, `velocity_north_knots`: signed ground-velocity components.
- `airspeed_knots`: **airspeed**, available in subtype 3/4.
- `airspeed_type`: `ias` or `tas` for subtype 3/4, otherwise `None`.
- `heading_deg`: magnetic heading, available in subtype 3/4 when the status bit is set.
- `heading_reference`: `magnetic` for subtype 3/4, otherwise `None`.
- `vertical_rate_fpm`: signed feet/minute; positive is climb, negative is descent.
- `vertical_rate_source`: `gnss` or `barometric`. The source flag is retained even
  when the rate itself is unavailable.
- `subtype`: the TC19 subtype number, 1-4.

Airspeed is never placed in `speed_knots`, and heading is never placed in
`track_deg`. A missing measurement is `None`; it is different from a valid zero.
Zero ground speed has an undefined track. Heading validity is independent of
airspeed validity. Subtype 2/4 uses four-knot horizontal speed steps; the vertical
rate always uses 64 feet/minute steps.

Backend integration should retain these measurement categories separately.
An unavailable field in the current event does not imply that an earlier
measurement of a different category was zero or should be replaced by this one.
The frontend's legacy `heading_deg` field should not silently mix magnetic
heading and true ground track.

## Complete event delivery

`DecodedEvent.to_dict()` / `to_json()` include the entire extensible `fields`
object, raw hex, measured time (if available), and metadata containing DF, Type
Code, CRC validity, source label, sample index and sample rate. The corresponding
`from_dict()` / `from_json()` methods preserve every field on receipt and accept
legacy events without metadata. Wire events declare `schema_version: "1.1"`.

`netradar.pipeline.DecoderPipeline` forwards each event to `AircraftStore` and
optionally a publisher callback. The update contains the full event in `event`
and aggregated UI measurements in `data`. Snapshots retain full latest events in
`latest_events`, separated by measurement category and height source. This keeps
raw CPR, subtype and extension fields available even if they are not part of the
flat UI summary. Every event is published; the store retains latest values rather
than an unbounded history. See [the delivery schema](CONTRACTS.md).

## Capture lifetime and verification

Use one decoder per capture with its actual `sample_rate_hz` (default 2 MS/s).
Call `reset()` before replaying another capture. CPR age is measured using sample
indices, not processing speed. `get_cpr_pair(icao)` selects barometric messages;
use `get_cpr_pair(icao, altitude_source="gnss")` for geometric messages.
Raw sample indices do not provide absolute time, so `observed_at_unix_ms` remains
`None` unless a future acquisition interface supplies a measured timestamp.

Run `python3 -m unittest discover -s tests -v` from the repository root. Tests use
published examples and explicitly synthetic frames. During development, every
possible AC12 input (4096 values) was also compared against an isolated build of
FlightAware's C decoder, with zero differences. That external decoder is only a
verification oracle, not an implementation dependency.

This coverage increases the information extracted from received DF17 messages.
It does not change the radio frequency, antenna coverage, station count, or RF
sample quality. Actual gains in aircraft count or data completeness require
measurements on real captures. Surface position, other downlink formats, and
other ADS-B message families are outside the implemented scope.

## Sources

- [FlightAware AC12 and geometric-height decoding](https://github.com/flightaware/dump1090/blob/master/mode_s.c)
- [FlightAware Mode C/Gillham reference](https://github.com/flightaware/dump1090/blob/master/mode_ac.c)
- [VIAVI IFR6000 remote commands, BDS 0,5 GNSS altitude, pp. 175-176](https://www.viavisolutions.com/en-us/literature/ifr6000-remote-commands-manual-discontinued-manuals-user-guides-en.pdf)
- [The 1090 MHz Riddle: velocity message formats](https://mode-s.org/1090mhz/content/ads-b/5-airborne-velocity.html)
