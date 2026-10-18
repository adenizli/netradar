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


class ADSBDecoder:
    def decode(self, frame: ModeSFrame) -> list[DecodedEvent]:
        """Reject unsupported/corrupted frames; payload decoding is still TODO."""
        header = parse_header(frame)
        if header.df != 17:
            return []

        if crc24(bytes.fromhex(frame.hex)) != 0:
            return []

        raise NotImplementedError("Implement CRC-valid DF17 payload decoding next")
