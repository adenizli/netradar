"""Ahmet + Ahmet Eren: implement Mode S / ADS-B protocol processing."""
from netradar.contracts import ModeSFrame, DecodedEvent


class ADSBDecoder:
    def decode(self, frame: ModeSFrame) -> list[DecodedEvent]:
        """TODO: CRC, DF17, ME type code, CPR pair state, velocity, identification."""
        raise NotImplementedError("Ahmet + Ahmet Eren: implement decode")
