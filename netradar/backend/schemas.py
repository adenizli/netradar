"""Validate the external event boundary without dropping payload extensions."""
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, field_validator

from netradar.contracts import DecodedEvent


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "1.1"] = "1.1"
    icao: Annotated[str, Field(pattern=r"^[0-9A-F]{6}$")]
    event_type: Literal["identification", "airborne_position", "airborne_velocity"]
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    raw_frame: Annotated[str, Field(pattern=r"^[0-9A-F]{28}$")] | None = None
    # datetime's supported UTC range; reject invalid times before store mutation.
    observed_at_unix_ms: Annotated[StrictInt, Field(ge=0, le=253402300799999)] | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values):
        numeric = {
            "latitude", "longitude", "altitude_ft", "gnss_height_ft", "speed_knots",
            "track_deg", "airspeed_knots", "heading_deg", "vertical_rate_fpm",
            "velocity_east_knots", "velocity_north_knots",
        }
        strings = {"callsign", "airspeed_type", "heading_reference",
                   "vertical_rate_source", "altitude_source"}
        for key in numeric:
            value = values.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"{key} must be a finite number or null")
        for key in strings:
            if values.get(key) is not None and not isinstance(values[key], str):
                raise ValueError(f"{key} must be a string or null")
        for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
            if values.get(key) is not None and not low <= values[key] <= high:
                raise ValueError(f"{key} is out of range")
        for key in ("track_deg", "heading_deg"):
            if values.get(key) is not None and not 0 <= values[key] < 360:
                raise ValueError(f"{key} must be in [0, 360)")
        if (values.get("latitude") is None) != (values.get("longitude") is None):
            raise ValueError("latitude and longitude must be supplied together")
        return values

    def to_event(self) -> DecodedEvent:
        event = DecodedEvent.from_dict(self.model_dump())
        event.to_json()  # Also rejects non-finite numbers in future extension fields.
        return event
