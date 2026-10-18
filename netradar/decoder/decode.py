"""Ahmet + Ahmet Eren: implement Mode S / ADS-B protocol processing."""
from dataclasses import dataclass
import math

from netradar.contracts import ModeSFrame, DecodedEvent


def crc24(data: bytes) -> int:
    """Return the Mode S CRC remainder, including the received parity bytes.

    A complete DF17 message passes this check when the remainder is zero.
    This is polynomial long division: XOR replaces subtraction.
    """
    generator = 0x1FFF409  # Degree 24, including the leading x**24 term.
    remainder = int.from_bytes(data, "big")

    for bit_index in range(len(data) * 8 - 1, 23, -1):
        if remainder & (1 << bit_index):
            remainder ^= generator << (bit_index - 24)

    return remainder


@dataclass(frozen=True)
class MessageHeader:
    df: int
    icao: str | None
    type_code: int | None


def parse_header(frame: ModeSFrame) -> MessageHeader:
    """Read DF and the DF17 header fields; this does not verify the CRC."""
    # Keep all 112 bits, including any leading zeros.
    binary = f"{int(frame.hex, 16):0112b}"
    df = int(binary[0:5], 2)

    # Other formats can assign different meanings to these bit positions.
    if df != 17:
        return MessageHeader(df=df, icao=None, type_code=None)

    return MessageHeader(
        df=df,
        icao=frame.hex[2:8],
        type_code=int(binary[32:37], 2),
    )


def decode_callsign(frame: ModeSFrame) -> str | None:
    """Read callsign from an identification payload; caller checks DF/TC/CRC."""
    binary = f"{int(frame.hex, 16):0112b}"
    characters = []

    # Message bits 41-88 contain eight characters of six bits each.
    for start in range(40, 88, 6):
        code = int(binary[start:start + 6], 2)
        if 1 <= code <= 26:
            characters.append(chr(ord("A") + code - 1))
        elif 48 <= code <= 57:
            characters.append(chr(ord("0") + code - 48))
        elif code == 32:
            characters.append(" ")
        else:
            # Reserved codes must not become invented callsign characters.
            return None

    return "".join(characters).rstrip() or None


def decode_altitude(alt_code: int) -> int | None:
    """Decode the 12-bit barometric Q=1 code in feet (TC 9-18 only).

    Q=0 uses Gillham coding, which is not implemented yet.
    """
    if not (alt_code & 0x10):
        return None

    # Remove Q (the fifth bit from the right) to get the 11-bit integer N.
    n = ((alt_code & 0xFE0) >> 1) | (alt_code & 0x0F)
    return 25 * n - 1000


def decode_airborne_position(frame: ModeSFrame) -> dict[str, int | float | str | None]:
    """Read TC 9-18 altitude and raw CPR; caller checks DF/TC/CRC."""
    binary = f"{int(frame.hex, 16):0112b}"
    alt_code = int(binary[40:52], 2)
    is_odd = binary[53] == "1"

    return {
        "altitude_ft": decode_altitude(alt_code),
        "cpr_format": "odd" if is_odd else "even",
        "cpr_lat": int(binary[54:71], 2),
        "cpr_lon": int(binary[71:88], 2),
        "latitude": None,
        "longitude": None,
    }


def cpr_nl(latitude: float) -> int:
    """Return the number of CPR longitude zones at a latitude in degrees."""
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be finite and between -90 and 90 degrees")
    if abs(latitude) > 87:
        return 1
    if abs(latitude) == 87:
        return 2

    a = 1 - math.cos(math.pi / 30)  # NZ = 15 latitude zones per quadrant.
    cosine = 1 - a / math.cos(math.radians(latitude)) ** 2
    cosine = max(-1.0, min(1.0, cosine))
    return min(59, math.floor(2 * math.pi / math.acos(cosine)))


def decode_cpr_pair(even: ModeSFrame, odd: ModeSFrame) -> tuple[float, float] | None:
    """Resolve a fresh, CRC-validated same-aircraft pair supplied by the cache.

    Coordinates refer to the newer message; equal sample indices prefer even.
    """
    even_bits = f"{int(even.hex, 16):0112b}"
    odd_bits = f"{int(odd.hex, 16):0112b}"
    if even_bits[53] != "0" or odd_bits[53] != "1":
        return None
    if even.hex[2:8] != odd.hex[2:8]:
        return None

    ye = int(even_bits[54:71], 2) / 131072
    yo = int(odd_bits[54:71], 2) / 131072
    xe = int(even_bits[71:88], 2) / 131072
    xo = int(odd_bits[71:88], 2) / 131072

    j = math.floor(59 * ye - 60 * yo + 0.5)
    lat_even = (360 / 60) * ((j % 60) + ye)
    lat_odd = (360 / 59) * ((j % 59) + yo)
    if lat_even >= 270:
        lat_even -= 360
    if lat_odd >= 270:
        lat_odd -= 360
    if not (-90 <= lat_even <= 90 and -90 <= lat_odd <= 90):
        return None

    nl = cpr_nl(lat_even)
    if nl != cpr_nl(lat_odd):
        return None

    use_odd = odd.sample_index > even.sample_index
    latitude = lat_odd if use_odd else lat_even
    m = math.floor(xe * (nl - 1) - xo * nl + 0.5)
    n = max(nl - int(use_odd), 1)
    x = xo if use_odd else xe
    longitude = (360 / n) * ((m % n) + x)
    longitude = (longitude + 180) % 360 - 180
    return latitude, longitude


class ADSBDecoder:
    """Decode one capture at a fixed sample rate; reset before a new capture."""

    def __init__(self, *, sample_rate_hz: int = 2_000_000):
        if sample_rate_hz <= 0:
            raise ValueError("Sample rate must be positive")

        self._max_pair_samples = 10 * sample_rate_hz
        self._latest_sample_index = 0
        self._cpr_frames: dict[str, dict[str, ModeSFrame]] = {}

    def reset(self) -> None:
        """Start a new capture without carrying over old CPR messages."""
        self._cpr_frames.clear()
        self._latest_sample_index = 0

    def get_cpr_pair(self, icao: str) -> tuple[ModeSFrame, ModeSFrame] | None:
        """Return (even, odd) if both are fresh, or None if no pair is ready."""
        messages = self._cpr_frames.get(icao, {})
        even = messages.get("even")
        odd = messages.get("odd")
        if even is None or odd is None:
            return None
        if abs(even.sample_index - odd.sample_index) > self._max_pair_samples:
            return None
        return even, odd

    def _expire_cpr_frames(self) -> None:
        # The clock is capture time, measured by the latest valid DF17 frame.
        for icao, messages in list(self._cpr_frames.items()):
            for cpr_format, frame in list(messages.items()):
                if self._latest_sample_index - frame.sample_index > self._max_pair_samples:
                    del messages[cpr_format]
            if not messages:
                del self._cpr_frames[icao]

    def _remember_position(self, icao: str, frame: ModeSFrame, cpr_format: str) -> None:
        if self._latest_sample_index - frame.sample_index > self._max_pair_samples:
            return

        messages = self._cpr_frames.setdefault(icao, {})
        previous = messages.get(cpr_format)
        if previous is None or frame.sample_index >= previous.sample_index:
            messages[cpr_format] = frame

    def decode(self, frame: ModeSFrame) -> list[DecodedEvent]:
        """Decode DF17 identification and globally resolved airborne position."""
        header = parse_header(frame)
        if header.df != 17:
            return []

        if crc24(bytes.fromhex(frame.hex)) != 0:
            return []

        self._latest_sample_index = max(self._latest_sample_index, frame.sample_index)
        self._expire_cpr_frames()

        if header.type_code in (1, 2, 3, 4):
            callsign = decode_callsign(frame)
            if callsign is None:
                return []

            return [DecodedEvent(
                icao=header.icao,
                event_type="identification",
                fields={"callsign": callsign},
                raw_frame=frame.hex,
                observed_at_unix_ms=None,
            )]

        if header.type_code in range(9, 19):
            fields = decode_airborne_position(frame)
            cpr_format = "odd" if fields["cpr_format"] == "odd" else "even"
            self._remember_position(header.icao, frame, cpr_format)
            position_frame = frame
            pair = self.get_cpr_pair(header.icao)
            if pair is not None:
                coordinates = decode_cpr_pair(*pair)
                if coordinates is not None:
                    # Keep altitude, raw CPR and raw_frame tied to the same time
                    # as the coordinates, even if an older message arrives late.
                    position_frame = max(pair, key=lambda item: item.sample_index)
                    fields = decode_airborne_position(position_frame)
                    fields["latitude"], fields["longitude"] = coordinates
            return [DecodedEvent(
                icao=header.icao,
                event_type="airborne_position",
                fields=fields,
                raw_frame=position_frame.hex,
                observed_at_unix_ms=None,
            )]

        # TODO: airborne velocity and GNSS height (TC 20-22).
        return []
