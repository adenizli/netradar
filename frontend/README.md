# Frontend -- Bensu

1. Scaffold React + Vite + Leaflet (not included: make deliberate setup choices).
2. Start with `../fixtures/aircraft_updates.mock.jsonl`: these are **fictional** demo flights.
3. Radar center: approximate Bogazici University, Istanbul `(41.085, 29.051)`.
4. Render rings + rotating plane markers, callsign, altitude, track, last seen.
5. A future HTTP/WebSocket adapter will expose `GET /api/aircraft` and
   `WS /ws/aircraft`. The current `DecoderPipeline` already emits their full
   `aircraft_update` envelopes through Python callbacks or JSONL; it does not start
   an HTTP/WebSocket server.
6. Treat `null` position as a known aircraft that cannot be mapped yet.
7. Label UI as **ADS-B receiver** rather than an active transmitter radar.
8. Use `data.track_deg` for true ground-track orientation. Display
   `data.heading_deg` separately as magnetic heading; do not silently substitute
   one for the other. Keep ground speed (`speed_knots`) separate from IAS/TAS
   (`airspeed_knots`, `airspeed_type`) and barometric altitude (`altitude_ft`)
   separate from GNSS geometric height (`gnss_height_ft`).
9. Every real update includes the complete decoder payload in `event`, including
   raw hex, source/sample metadata, CPR fields and future extensions. Render
   optional details from `event.fields`; do not discard unknown fields while
   forwarding updates. Initial snapshots retain full payloads in
   `data.latest_events`. Demo fixture updates have `event: null`.
10. `last_seen` can be null for timestamp-free replay. Show it as unknown rather
    than inventing an observation time. Schema details: `../docs/CONTRACTS.md`.
