"""Chunked CU8 replay; live RTL-SDR acquisition remains a hardware adapter."""
from collections.abc import Iterator
from os import fstat
from pathlib import Path
from netradar.contracts import IqChunk


def replay_cu8(path: str | Path, *, chunk_samples: int = 8192,
               sample_rate_hz: int = 2_000_000) -> Iterator[IqChunk]:
    """Stream interleaved unsigned bytes, counting offsets in complex I/Q pairs."""
    if type(chunk_samples) is not int or chunk_samples <= 0:
        raise ValueError("chunk_samples must be a positive integer")
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be a positive integer")
    with Path(path).open("rb") as stream:
        if fstat(stream.fileno()).st_size % 2:
            raise ValueError("CU8 file ends with an unpaired I/Q byte")
        offset = 0
        while data := stream.read(chunk_samples * 2):
            if len(data) % 2:
                raise ValueError("CU8 file ends with an unpaired I/Q byte")
            yield IqChunk(data=data, start_sample_index=offset, sample_rate_hz=sample_rate_hz)
            offset += len(data) // 2


def stream_rtlsdr(*, sample_rate_hz: int = 2_000_000) -> Iterator[IqChunk]:
    """TODO: connect to RTL-SDR and yield IqChunk objects."""
    raise NotImplementedError("Yasir: implement stream_rtlsdr")
