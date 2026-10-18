"""Ahmet + Ahmet Eren: implement Mode S / ADS-B protocol processing."""
from dataclasses import dataclass

from netradar.contracts import ModeSFrame, DecodedEvent


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
        """TODO: CRC, DF17, ME type code, CPR pair state, velocity, identification."""
        raise NotImplementedError("Ahmet + Ahmet Eren: implement decode")
