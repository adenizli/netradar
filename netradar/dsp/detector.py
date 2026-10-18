"""Streaming 2 MS/s CU8 -> 112-bit DF17 candidates; decoder owns CRC validation."""
from collections.abc import Iterable, Iterator

import numpy as np

from netradar.contracts import IqChunk, ModeSFrame
from netradar.dsp.preamble import FRAME_SAMPLES, SAMPLE_RATE, detect_preambles
from netradar.dsp.ppm import demodulate_df17_frames, demodulate_frame  # noqa: F401 (re-export)

# (I-127.5)**2 + (Q-127.5)**2 in float32 for every little-endian (I, Q) byte pair.
_CODES = np.arange(65536)
_POWER_LUT = (
    np.square((_CODES & 0xFF).astype(np.float32) - 127.5)
    + np.square((_CODES >> 8).astype(np.float32) - 127.5)
)
del _CODES


def detect_frames(chunks: Iterable[IqChunk], *, source: str = "rf",
                  capture_start_unix_ms: int | None = None) -> Iterator[ModeSFrame]:
    """Consume chunks lazily, retaining 239 samples across contiguous boundaries.

    capture_start_unix_ms, when actually known, is the time of sample index zero.
    A raw file has no such timestamp. Gaps discard the tail; overlap/rewind is an
    input error, since indices identify one forward-moving capture.
    """
    if not isinstance(source, str) or not source:
        raise ValueError("Frame source must be a nonempty string")
    if capture_start_unix_ms is not None and (
        type(capture_start_unix_ms) is not int or capture_start_unix_ms < 0
    ):
        raise ValueError("Capture time must be nonnegative integer milliseconds")
    tail = np.empty(0, dtype=np.float32)
    expected_start = None
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if chunk.sample_rate_hz != SAMPLE_RATE:
                raise ValueError("DSP currently supports exactly 2,000,000 samples/second")
            if chunk.center_frequency_hz != 1_090_000_000:
                raise ValueError("DSP expects a 1090 MHz ADS-B capture")
            if chunk.sample_count == 0:
                continue
            if expected_start is not None:
                if chunk.start_sample_index < expected_start:
                    raise ValueError("IQ chunks overlap or rewind within one capture")
                if chunk.start_sample_index != expected_start:
                    tail = np.empty(0, dtype=np.float32)
            current_power = _POWER_LUT[np.frombuffer(chunk.data, dtype="<u2")]
            power = np.concatenate((tail, current_power)) if tail.size else current_power
            buffer_start = chunk.start_sample_index - len(tail)
            expected_start = chunk.start_sample_index + chunk.sample_count
            for start, raw in demodulate_df17_frames(power, detect_preambles(power)):
                sample_index = buffer_start + start
                observed = None if capture_start_unix_ms is None else (
                    capture_start_unix_ms + sample_index * 1000 // SAMPLE_RATE
                )
                yield ModeSFrame(hex=raw.hex().upper(), sample_index=sample_index,
                                 source=source, observed_at_unix_ms=observed)
            # Complete starts were scanned once; only incomplete possible starts remain.
            tail = power[-(FRAME_SAMPLES - 1):].copy()
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
