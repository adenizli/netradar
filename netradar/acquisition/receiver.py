"""Yasir: SDR source and file replay implementation goes here."""
from collections.abc import Iterator
from netradar.contracts import IqChunk


def replay_cu8(path: str, *, chunk_samples: int = 8192) -> Iterator[IqChunk]:
    """Read CU8 from a saved file. TODO: implement chunking and monotonically increasing offsets."""
    raise NotImplementedError("Yasir: implement replay_cu8")


def stream_rtlsdr(*, sample_rate_hz: int = 2_000_000) -> Iterator[IqChunk]:
    """TODO: connect to RTL-SDR and yield IqChunk objects."""
    raise NotImplementedError("Yasir: implement stream_rtlsdr")
