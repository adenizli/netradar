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


# Sample offsets from a preamble start: the 5-bit DF field, and all 112 bits.
_DF_OFFSETS = 16 + np.arange(10)
_FRAME_OFFSETS = 16 + np.arange(224)


def demodulate_df17_frames(power: np.ndarray, starts) -> list[tuple[int, bytes]]:
    """Vectorized demodulate_frame over many candidate starts, keeping only DF17.

    Accepts exactly the starts for which demodulate_frame returns a frame whose
    downlink format is 17, in the same order. Noise candidates are first
    rejected on the 5 DF bits alone, so only plausible ones are fully sliced.
    """
    starts = np.asarray(starts, dtype=np.intp)
    starts = starts[(starts >= 0) & (starts + 240 <= len(power))]
    if not starts.size:
        return []
    head = power[starts[:, None] + _DF_OFFSETS]
    df_bits = head[:, 0::2] > head[:, 1::2]
    # DF 17 = 10001; ties fail the full check below anyway.
    starts = starts[df_bits[:, 0] & ~df_bits[:, 1] & ~df_bits[:, 2] & ~df_bits[:, 3] & df_bits[:, 4]]
    if not starts.size:
        return []
    data = power[starts[:, None] + _FRAME_OFFSETS]
    first_half, second_half = data[:, 0::2], data[:, 1::2]
    frames = np.packbits(first_half > second_half, axis=1, bitorder="big")
    ok = (
        np.isfinite(data).all(axis=1)
        & ~(first_half == second_half).any(axis=1)
        & (frames[:, 0] >> 3 == 17)
    )
    return [(int(start), frame.tobytes()) for start, frame in zip(starts[ok], frames[ok])]
