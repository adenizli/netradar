"""Dependency-free cross-team contracts with lossless v1.1 JSON event delivery."""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
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
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.fields, dict) or not isinstance(self.metadata, dict):
            raise ValueError("Event fields and metadata must be objects")
        if len(self.icao) != 6 or self.icao != self.icao.upper():
            raise ValueError("ICAO should be 6 uppercase hex characters")
        try:
            int(self.icao, 16)
        except ValueError as exc:
            raise ValueError("ICAO must be hex") from exc
        if self.event_type not in {"identification", "airborne_position", "airborne_velocity"}:
            raise ValueError("Unexpected event type")

    def to_dict(self) -> dict[str, Any]:
        """Return all fields, including future payload extensions, as a copy."""
        return {"schema_version": "1.1", **asdict(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DecodedEvent":
        if not isinstance(payload, dict):
            raise ValueError("Decoded event must be a JSON object")
        data = deepcopy(payload)
        version = data.pop("schema_version", "1.0")
        if version not in ("1.0", "1.1"):
            raise ValueError(f"Unsupported decoded event schema: {version}")
        # Reject values that cannot cross the JSON boundary without invalid numbers.
        json.dumps(data, allow_nan=False)
        return cls(**data)

    @classmethod
    def from_json(cls, payload: str) -> "DecodedEvent":
        return cls.from_dict(json.loads(payload))
