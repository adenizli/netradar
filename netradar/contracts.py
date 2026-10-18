"""Small, dependency-free cross-team data contracts. Version 1 is frozen for sprint."""
from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass(frozen=True)
class IqChunk:
    data: bytes
    start_sample_index: int
    sample_rate_hz: int = 2_000_000
    center_frequency_hz: int = 1_090_000_000

    def __post_init__(self):
        if len(self.data) % 2:
            raise ValueError("CU8 IQ data must contain pairs of I/Q bytes")
        if self.start_sample_index < 0 or self.sample_rate_hz <= 0:
            raise ValueError("Invalid sample index or sampling rate")

    @property
    def sample_count(self) -> int:
        return len(self.data) // 2


@dataclass(frozen=True)
class ModeSFrame:
    hex: str
    sample_index: int
    source: str = "rf"

    def __post_init__(self):
        if len(self.hex) != 28:
            raise ValueError("MVP expects 112-bit / 28-character hex messages")
        try:
            bytes.fromhex(self.hex)
        except ValueError as exc:
            raise ValueError("Invalid hex") from exc
        if self.hex != self.hex.upper() or self.sample_index < 0:
            raise ValueError("Hex must be uppercase; sample index nonnegative")


@dataclass(frozen=True)
class DecodedEvent:
    icao: str
    event_type: str
    fields: dict[str, Any] = field(default_factory=dict)
    raw_frame: Optional[str] = None
    observed_at_unix_ms: Optional[int] = None

    def __post_init__(self):
        if len(self.icao) != 6 or self.icao != self.icao.upper():
            raise ValueError("ICAO should be 6 uppercase hex characters")
        try:
            int(self.icao, 16)
        except ValueError as exc:
            raise ValueError("ICAO must be hex") from exc
        if self.event_type not in {"identification", "airborne_position", "airborne_velocity"}:
            raise ValueError("Unexpected event type")
