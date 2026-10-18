"""Ahmet + Ahmet Eren: implement Mode S / ADS-B protocol processing."""
from dataclasses import dataclass, replace
import math
from uuid import uuid4

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


# CRC-24 is linear: flipping bit b of a 112-bit message always leaves the
# same remainder, so a remainder identifies a single-bit error position.
SINGLE_BIT_SYNDROMES = {crc24((1 << (111 - bit)).to_bytes(14, "big")): bit for bit in range(112)}

# A single-bit fix is only trusted for an aircraft heard with a clean CRC this
# recently (capture time). Otherwise random noise could invent an aircraft.
KNOWN_ICAO_SECONDS = 60


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


def _gray_to_int(gray: int) -> int:
    value = 0
    while gray:
        value ^= gray
        gray >>= 1
    return value


def decode_altitude(alt_code: int) -> int | None:
    """Decode a 12-bit AC field in feet, including Q=0 Gillham encoding."""
    if alt_code == 0:
        return None
    if alt_code & 0x10:
        # Remove Q (the fifth bit from the right) to get the 11-bit integer N.
        n = ((alt_code & 0xFE0) >> 1) | (alt_code & 0x0F)
        return 25 * n - 1000

    # AC12 order: C1 A1 C2 A2 C4 A4 B1 Q B2 D2 B4 D4.
    bits = f"{alt_code:012b}"
    gray500 = int("".join(bits[i] for i in (9, 11, 1, 3, 5, 6, 8, 10)), 2)
    gray100 = int("".join(bits[i] for i in (0, 2, 4)), 2)
    n500 = _gray_to_int(gray500)
    n100 = _gray_to_int(gray100)
    if n100 in (0, 5, 6):
        return None
    if n100 == 7:
        n100 = 5
    if n500 % 2:
        n100 = 6 - n100
    return 500 * n500 + 100 * n100 - 1300


def decode_airborne_position(frame: ModeSFrame) -> dict[str, int | float | str | None]:
    """Read TC 9-18/20-22 height and raw CPR; caller checks DF/TC/CRC."""
    binary = f"{int(frame.hex, 16):0112b}"
    is_gnss = int(binary[32:37], 2) in (20, 21, 22)
    alt_code = int(binary[40:52], 2)
    height_ft = decode_altitude(alt_code)
    is_odd = binary[53] == "1"

    return {
        # Keep geometric height separate from the existing barometric field.
        "altitude_ft": None if is_gnss else height_ft,
        "gnss_height_ft": height_ft if is_gnss else None,
        "altitude_source": "gnss" if is_gnss else "barometric",
        "cpr_format": "odd" if is_odd else "even",
        "cpr_lat": int(binary[54:71], 2),
        "cpr_lon": int(binary[71:88], 2),
        "latitude": None,
        "longitude": None,
    }


def decode_velocity_components(frame: ModeSFrame) -> dict[str, int | float | str | None] | None:
    """Read TC19 subtype 1-4 ground/air/vertical velocity; caller checks DF/TC/CRC."""
    binary = f"{int(frame.hex, 16):0112b}"
    subtype = int(binary[37:40], 2)
    if subtype not in (1, 2, 3, 4):
        return None

    east = None
    north = None
    speed = None
    track = None
    airspeed = None
    airspeed_type = None
    heading = None
    scale = 4 if subtype in (2, 4) else 1
    if subtype in (1, 2):
        east_code = int(binary[46:56], 2)
        north_code = int(binary[57:67], 2)
        # Zero means unavailable; code 1 represents a real zero-knot component.
        east = None if east_code == 0 else (east_code - 1) * scale
        north = None if north_code == 0 else (north_code - 1) * scale
        if east is not None and binary[45] == "1":
            east = -east
        if north is not None and binary[56] == "1":
            north = -north
        if east is not None and north is not None:
            speed = math.hypot(east, north)
            if speed > 0:
                # atan2(east, north) gives clockwise bearing from north.
                track = math.degrees(math.atan2(east, north)) % 360
    else:
        # The same bit positions mean heading and airspeed in subtype 3/4.
        if binary[45] == "1":
            heading = int(binary[46:56], 2) * 360 / 1024
        airspeed_code = int(binary[57:67], 2)
        airspeed = None if airspeed_code == 0 else (airspeed_code - 1) * scale
        airspeed_type = "tas" if binary[56] == "1" else "ias"

    vertical_code = int(binary[69:78], 2)
    vertical_rate = None if vertical_code == 0 else (vertical_code - 1) * 64
    if vertical_rate is not None and binary[68] == "1":
        vertical_rate = -vertical_rate
    vertical_source = "barometric" if binary[67] == "1" else "gnss"

    return {
        "subtype": subtype,
        "velocity_east_knots": east,
        "velocity_north_knots": north,
        "speed_knots": speed,
        "track_deg": track,
        "airspeed_knots": airspeed,
        "airspeed_type": airspeed_type,
        "heading_deg": heading,
        "heading_reference": "magnetic" if subtype in (3, 4) else None,
        "vertical_rate_fpm": vertical_rate,
        "vertical_rate_source": vertical_source,
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
    even_gnss = int(even_bits[32:37], 2) in (20, 21, 22)
    odd_gnss = int(odd_bits[32:37], 2) in (20, 21, 22)
    if even_gnss != odd_gnss:
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

    def __init__(self, *, sample_rate_hz: int = 2_000_000, capture_id: str | None = None):
        if sample_rate_hz <= 0:
            raise ValueError("Sample rate must be positive")

        if capture_id is not None and (not isinstance(capture_id, str) or not capture_id):
            raise ValueError("Capture identity must be a nonempty string")
        self.capture_id = capture_id if capture_id is not None else uuid4().hex
        self._sample_rate_hz = sample_rate_hz
        self._max_pair_samples = 10 * sample_rate_hz
        self._known_icao_samples = KNOWN_ICAO_SECONDS * sample_rate_hz
        self._latest_sample_index = 0
        self._cpr_frames: dict[tuple[str, str], dict[str, ModeSFrame]] = {}
        # ICAO -> sample index of its latest clean-CRC (uncorrected) frame.
        self._clean_icaos: dict[str, int] = {}

    def reset(self) -> None:
        """Start a new capture without carrying over old CPR messages."""
        self.capture_id = uuid4().hex
        self._cpr_frames.clear()
        self._clean_icaos.clear()
        self._latest_sample_index = 0

    def get_cpr_pair(
        self, icao: str, *, altitude_source: str = "barometric",
    ) -> tuple[ModeSFrame, ModeSFrame] | None:
        """Return fresh (even, odd) for one ICAO and height source, or None."""
        messages = self._cpr_frames.get((icao, altitude_source), {})
        even = messages.get("even")
        odd = messages.get("odd")
        if even is None or odd is None:
            return None
        if abs(even.sample_index - odd.sample_index) > self._max_pair_samples:
            return None
        return even, odd

    def _expire_cpr_frames(self) -> None:
        # The clock is capture time, measured by the latest valid DF17 frame.
        for key, messages in list(self._cpr_frames.items()):
            for cpr_format, frame in list(messages.items()):
                if self._latest_sample_index - frame.sample_index > self._max_pair_samples:
                    del messages[cpr_format]
            if not messages:
                del self._cpr_frames[key]
        for icao, sample_index in list(self._clean_icaos.items()):
            if self._latest_sample_index - sample_index > self._known_icao_samples:
                del self._clean_icaos[icao]

    def _correct_single_bit(self, frame: ModeSFrame, syndrome: int) -> ModeSFrame | None:
        """Flip the one bit a single-bit error would explain, for a known aircraft only."""
        bit = SINGLE_BIT_SYNDROMES.get(syndrome)
        if bit is None:
            return None
        value = int(frame.hex, 16) ^ (1 << (111 - bit))
        corrected = f"{value:028X}"
        last_clean = self._clean_icaos.get(corrected[2:8])
        if value >> 107 != 17 or last_clean is None:
            return None
        if abs(frame.sample_index - last_clean) > self._known_icao_samples:
            return None
        return replace(frame, hex=corrected)

    def _remember_position(
        self, icao: str, frame: ModeSFrame, cpr_format: str, altitude_source: str,
    ) -> None:
        if self._latest_sample_index - frame.sample_index > self._max_pair_samples:
            return

        messages = self._cpr_frames.setdefault((icao, altitude_source), {})
        previous = messages.get(cpr_format)
        if previous is None or frame.sample_index >= previous.sample_index:
            messages[cpr_format] = frame

    def _event(self, frame: ModeSFrame, event_type: str, fields: dict,
               extra_metadata: dict | None = None) -> DecodedEvent:
        header = parse_header(frame)
        return DecodedEvent(
            icao=header.icao,
            event_type=event_type,
            fields=fields,
            raw_frame=frame.hex,
            observed_at_unix_ms=frame.observed_at_unix_ms,
            metadata={
                "capture_id": self.capture_id,
                "df": header.df,
                "type_code": header.type_code,
                "crc_valid": True,
                "source": frame.source,
                "sample_index": frame.sample_index,
                "sample_rate_hz": self._sample_rate_hz,
                **(extra_metadata or {}),
            },
        )

    def decode(self, frame: ModeSFrame) -> list[DecodedEvent]:
        """Decode DF17 identification, airborne position and velocity components.

        A frame with a single-bit CRC error is repaired when its corrected ICAO
        sent a clean frame within KNOWN_ICAO_SECONDS; such events carry
        ``crc_corrected_bit`` and the original ``received_frame`` in metadata.
        """
        header = parse_header(frame)
        if header.df != 17:
            return []

        extra_metadata = None
        syndrome = crc24(bytes.fromhex(frame.hex))
        if syndrome != 0:
            corrected = self._correct_single_bit(frame, syndrome)
            if corrected is None:
                return []
            extra_metadata = {
                "crc_corrected_bit": SINGLE_BIT_SYNDROMES[syndrome],
                "received_frame": frame.hex,
            }
            frame = corrected
            header = parse_header(frame)
        else:
            # Only clean frames vouch for an aircraft; fixes cannot bootstrap fixes.
            previous = self._clean_icaos.get(header.icao, frame.sample_index)
            self._clean_icaos[header.icao] = max(previous, frame.sample_index)

        self._latest_sample_index = max(self._latest_sample_index, frame.sample_index)
        self._expire_cpr_frames()

        if header.type_code in (1, 2, 3, 4):
            callsign = decode_callsign(frame)
            if callsign is None:
                return []

            return [self._event(frame, "identification", {"callsign": callsign}, extra_metadata)]

        if header.type_code in range(9, 19) or header.type_code in (20, 21, 22):
            fields = decode_airborne_position(frame)
            cpr_format = "odd" if fields["cpr_format"] == "odd" else "even"
            altitude_source = "gnss" if header.type_code in (20, 21, 22) else "barometric"
            self._remember_position(header.icao, frame, cpr_format, altitude_source)
            position_frame = frame
            pair = self.get_cpr_pair(header.icao, altitude_source=altitude_source)
            if pair is not None:
                coordinates = decode_cpr_pair(*pair)
                if coordinates is not None:
                    # Keep altitude, raw CPR and raw_frame tied to the same time
                    # as the coordinates, even if an older message arrives late.
                    position_frame = max(pair, key=lambda item: item.sample_index)
                    fields = decode_airborne_position(position_frame)
                    fields["latitude"], fields["longitude"] = coordinates
            if position_frame is not frame:
                extra_metadata = None  # the event describes the newer pair member
            return [self._event(position_frame, "airborne_position", fields, extra_metadata)]

        if header.type_code == 19:
            fields = decode_velocity_components(frame)
            if fields is None:
                return []
            return [self._event(frame, "airborne_velocity", fields, extra_metadata)]

        return []
