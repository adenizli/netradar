"""Aircraft state with complete decoder events retained for downstream consumers."""
from copy import deepcopy
from datetime import datetime, timezone

from netradar.contracts import DecodedEvent


class AircraftStore:
    def __init__(self):
        self._aircraft: dict[str, dict] = {}

    @staticmethod
    def _event_slot(event: DecodedEvent) -> str:
        if event.event_type == "airborne_position":
            return f"airborne_position:{event.fields.get('altitude_source', 'barometric')}"
        if event.event_type == "airborne_velocity":
            kind = "air" if event.fields.get("subtype") in (3, 4) else "ground"
            return f"airborne_velocity:{kind}"
        return event.event_type

    def apply(self, event: DecodedEvent) -> dict:
        """Merge known measurements and return a lossless aircraft_update payload."""
        payload = event.to_dict()
        # Validate wire compatibility before changing state. No field whitelist
        # is used for the full event: arbitrary payload/metadata extensions survive.
        event.to_json()
        observed = event.observed_at_unix_ms
        observed_text = None if observed is None else datetime.fromtimestamp(
            observed / 1000, tz=timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        scalar_fields = (
            "callsign", "altitude_ft", "gnss_height_ft",
            "velocity_east_knots", "velocity_north_knots",
        )
        measurement_groups = (
            ("speed_knots", "track_deg"),
            ("airspeed_knots", "airspeed_type"),
            ("heading_deg", "heading_reference"),
            ("vertical_rate_fpm", "vertical_rate_source"),
        )
        aircraft = self._aircraft.setdefault(event.icao, {
            "icao": event.icao,
            **{key: None for key in scalar_fields},
            **{key: None for group in measurement_groups for key in group},
            "latitude": None, "longitude": None, "position_source": None,
            "last_seen": None, "last_seen_unix_ms": None,
            "latest_events": {},
        })
        aircraft["latest_events"][self._event_slot(event)] = deepcopy(payload)

        for key in scalar_fields:
            if event.fields.get(key) is not None:
                aircraft[key] = deepcopy(event.fields[key])
        for measurement, companion in measurement_groups:
            if event.fields.get(measurement) is not None:
                aircraft[measurement] = deepcopy(event.fields[measurement])
                # A real zero speed intentionally clears the undefined track.
                aircraft[companion] = deepcopy(event.fields.get(companion))
        if event.fields.get("latitude") is not None and event.fields.get("longitude") is not None:
            aircraft["latitude"] = event.fields["latitude"]
            aircraft["longitude"] = event.fields["longitude"]
            aircraft["position_source"] = event.fields.get("altitude_source")

        if observed is not None and (aircraft["last_seen_unix_ms"] is None or observed >= aircraft["last_seen_unix_ms"]):
            aircraft["last_seen"] = observed_text
            aircraft["last_seen_unix_ms"] = observed

        return {
            "schema_version": "1.1",
            "type": "aircraft_update",
            "data": deepcopy(aircraft),
            "event": payload,
        }

    def apply_json(self, payload: str) -> dict:
        """Receive a serialized decoder event from another process."""
        return self.apply(DecodedEvent.from_json(payload))

    def snapshot(self) -> list[dict]:
        """Return independent aircraft objects, including all retained event fields."""
        return [deepcopy(self._aircraft[icao]) for icao in sorted(self._aircraft)]
