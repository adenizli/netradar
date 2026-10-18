"""Ahmet + Ahmet Eren: implement Mode S / ADS-B protocol processing."""
from dataclasses import dataclass

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


def decode_airborne_position(frame: ModeSFrame) -> dict[str, int | str | None]:
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


class ADSBDecoder:
    def decode(self, frame: ModeSFrame) -> list[DecodedEvent]:
        """Decode DF17 identification and raw barometric airborne position."""
        header = parse_header(frame)
        if header.df != 17:
            return []

        if crc24(bytes.fromhex(frame.hex)) != 0:
            return []

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
            return [DecodedEvent(
                icao=header.icao,
                event_type="airborne_position",
                fields=decode_airborne_position(frame),
                raw_frame=frame.hex,
                observed_at_unix_ms=None,
            )]

        # TODO: CPR pair state, airborne velocity and GNSS height (TC 20-22).
        return []
