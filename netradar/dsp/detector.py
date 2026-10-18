"""Arda: implement preamble detection + 112-bit PPM extraction."""
from collections.abc import Iterable, Iterator
from netradar.contracts import IqChunk, ModeSFrame


def detect_frames(chunks: Iterable[IqChunk]) -> Iterator[ModeSFrame]:
    """TODO: detect and yield ModeSFrame; preserve overlap across chunk boundaries."""
    raise NotImplementedError("Arda: implement detect_frames")
    yield  # type: ignore[misc]  # explicitly marks this as a generator API
