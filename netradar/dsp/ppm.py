"""112-bit Mode S PPM extraction at exactly 2 MS/s; CRC stays in the decoder."""
import numpy as np


def demodulate_frame(
    power: np.ndarray,
    start: int
) -> bytes | None:

    if start < 0 or power.ndim != 1 or start + 240 > len(power):
        return None
    # Preamble: 8 us = 16 samples
    data_start = start + 16

    # 112 bits * 2 samples/bit = 224 samples
    data = power[data_start:data_start + 224]

    if len(data) != 224:
        return None

    # Split each bit into its first and second half
    first_half = data[0::2]
    second_half = data[1::2]
    if not np.isfinite(data).all() or np.any(first_half == second_half):
        return None

    # Early pulse = 1, late pulse = 0
    bits = (first_half > second_half).astype(np.uint8)

    # Convert 112 bits into 14 bytes
    frame = np.packbits(bits, bitorder="big")

    return frame.tobytes()
