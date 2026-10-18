# Frontend -- Bensu

1. Scaffold React + Vite + Leaflet (not included: make deliberate setup choices).
2. Start with `../fixtures/aircraft_updates.mock.jsonl`: these are **fictional** demo flights.
3. Radar center: approximate Bogazici University, Istanbul `(41.085, 29.051)`.
4. Render rings + rotating plane markers, callsign, altitude, heading, last seen.
5. Switch to `GET /api/aircraft` + `WS /ws/aircraft` when Nisa's backend is ready.
6. Treat `null` position as a known aircraft that cannot be mapped yet.
7. Label UI as **ADS-B receiver** rather than an active transmitter radar.
