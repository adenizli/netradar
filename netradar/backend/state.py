"""Aircraft state with ordered measurements and independent position age."""
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import time

from netradar.contracts import DecodedEvent


def _capture_clock(metadata: dict) -> tuple[str, str, int, int] | None:
    """Sample indices are comparable only within one explicitly named capture."""
    capture = metadata.get('capture_id')
    source = metadata.get('source')
    rate = metadata.get('sample_rate_hz')
    index = metadata.get('sample_index')
    if (isinstance(capture, str) and capture and isinstance(source, str)
            and type(rate) is int and rate > 0 and type(index) is int and index >= 0):
        return capture, source, rate, index
    return None


def _order(payload: dict) -> tuple[int | None, tuple | None]:
    return payload.get('observed_at_unix_ms'), _capture_clock(payload.get('metadata', {}))


def _compare(incoming: tuple, previous: tuple) -> int | None:
    """Return older/equal/newer, or None when chronology cannot be established."""
    observed, samples = incoming
    old_observed, old_samples = previous
    if observed is not None and old_observed is not None and observed != old_observed:
        return 1 if observed > old_observed else -1
    if samples and old_samples and samples[:3] == old_samples[:3]:
        return (samples[3] > old_samples[3]) - (samples[3] < old_samples[3])
    if observed is not None and old_observed is not None:
        return 0
    # Do not replace dated data with an undated, incomparable message.
    if observed is None and old_observed is not None:
        return -1
    if observed is not None and old_observed is None:
        return 1
    return None


class AircraftStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self._aircraft: dict[str, dict] = {}
        self._measurement_order: dict[tuple[str, str], tuple] = {}
        self._position_timing: dict[str, tuple[float, float]] = {}
        self._capture_samples: dict[tuple, int] = {}
        self._clock = clock
        self._wall_clock = wall_clock

    @staticmethod
    def _event_slot(event: DecodedEvent) -> str:
        if event.event_type == 'airborne_position':
            return f"airborne_position:{event.fields.get('altitude_source', 'barometric')}"
        if event.event_type == 'airborne_velocity':
            kind = 'air' if event.fields.get('subtype') in (3, 4) else 'ground'
            return f'airborne_velocity:{kind}'
        return event.event_type

    def _accept(self, icao: str, key: str, order: tuple, *, strict=False) -> bool:
        previous = self._measurement_order.get((icao, key))
        comparison = _compare(order, previous) if previous is not None else 1
        # Coordinates require positive evidence of a newer measurement. Repeated
        # fixes must not reset their age. Legacy unclocked scalar events retain
        # arrival-order behavior without being allowed to replace dated values.
        accepted = comparison == 1 if strict else comparison is None or comparison >= 0
        if accepted:
            self._measurement_order[icao, key] = order
        return accepted

    def _snapshot_aircraft(self, aircraft: dict) -> dict:
        data = deepcopy(aircraft)
        timing = self._position_timing.get(aircraft['icao'])
        if timing is not None:
            accepted_at, initial_age = timing
            age = initial_age + max(0, self._clock() - accepted_at) * 1000
            samples = _capture_clock(aircraft['position_event'].get('metadata', {}))
            if samples:
                head = self._capture_samples.get(samples[:3], samples[3])
                age = max(age, (head - samples[3]) * 1000 / samples[2])
            data['position_age_ms'] = max(0, int(age))
        return data

    def apply(self, event: DecodedEvent) -> dict:
        """Deliver every event, while only newer fixes update current position."""
        payload = event.to_dict()
        event.to_json()  # Validate lossless JSON compatibility before mutation.
        observed = event.observed_at_unix_ms
        observed_text = None if observed is None else datetime.fromtimestamp(
            observed / 1000, tz=timezone.utc,
        ).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        order = _order(payload)
        samples = order[1]
        if samples:
            key = samples[:3]
            self._capture_samples[key] = max(self._capture_samples.get(key, 0), samples[3])
        scalar_fields = (
            'callsign', 'altitude_ft', 'gnss_height_ft',
            'velocity_east_knots', 'velocity_north_knots',
        )
        measurement_groups = (
            ('speed_knots', 'track_deg'),
            ('airspeed_knots', 'airspeed_type'),
            ('heading_deg', 'heading_reference'),
            ('vertical_rate_fpm', 'vertical_rate_source'),
        )
        aircraft = self._aircraft.setdefault(event.icao, {
            'icao': event.icao,
            **{key: None for key in scalar_fields},
            **{key: None for group in measurement_groups for key in group},
            'latitude': None, 'longitude': None, 'position_source': None,
            'last_seen': None, 'last_seen_unix_ms': None,
            'position_revision': 0, 'position_event': None,
            'position_observed_at_unix_ms': None,
            'position_received_at_unix_ms': None, 'position_age_ms': None,
            'latest_events': {},
        })
        # This is latest-received raw history, including rejected old fixes.
        # position_event separately identifies the accepted coordinate pair.
        aircraft['latest_events'][self._event_slot(event)] = deepcopy(payload)
        for key in scalar_fields:
            if event.fields.get(key) is not None and self._accept(event.icao, key, order):
                aircraft[key] = deepcopy(event.fields[key])
        for measurement, companion in measurement_groups:
            if (event.fields.get(measurement) is not None
                    and self._accept(event.icao, measurement, order)):
                aircraft[measurement] = deepcopy(event.fields[measurement])
                aircraft[companion] = deepcopy(event.fields.get(companion))
        if (event.event_type == 'airborne_position'
                and event.fields.get('latitude') is not None
                and event.fields.get('longitude') is not None
                and self._accept(event.icao, 'position', order, strict=True)):
            aircraft['latitude'] = event.fields['latitude']
            aircraft['longitude'] = event.fields['longitude']
            aircraft['position_source'] = event.fields.get('altitude_source')
            aircraft['position_revision'] += 1
            aircraft['position_event'] = deepcopy(payload)
            aircraft['position_observed_at_unix_ms'] = observed
            wall_ms = int(self._wall_clock() * 1000)
            aircraft['position_received_at_unix_ms'] = wall_ms
            initial_age = 0 if observed is None else max(0, wall_ms - observed)
            self._position_timing[event.icao] = (self._clock(), initial_age)

        if observed is not None and (aircraft['last_seen_unix_ms'] is None or observed >= aircraft['last_seen_unix_ms']):
            aircraft['last_seen'] = observed_text
            aircraft['last_seen_unix_ms'] = observed
        return {
            'schema_version': '1.1', 'type': 'aircraft_update',
            'data': self._snapshot_aircraft(aircraft), 'event': payload,
        }

    def apply_json(self, payload: str) -> dict:
        return self.apply(DecodedEvent.from_json(payload))

    def snapshot(self) -> list[dict]:
        """Position age is recalculated even when no new messages arrive."""
        return [self._snapshot_aircraft(self._aircraft[icao]) for icao in sorted(self._aircraft)]
