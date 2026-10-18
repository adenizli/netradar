"""Nisa: retain aircraft state and merge DecodedEvent updates."""
from netradar.contracts import DecodedEvent


class AircraftStore:
    def apply(self, event: DecodedEvent) -> dict:
        """TODO: merge an event into aircraft state keyed by ICAO and return update."""
        raise NotImplementedError("Nisa: implement apply")

    def snapshot(self) -> list[dict]:
        """TODO: return aircraft objects for initial GET /api/aircraft."""
        raise NotImplementedError("Nisa: implement snapshot")
