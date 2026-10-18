"""Arda: implement preamble detection + 112-bit PPM extraction."""
from collections.abc import Iterable, Iterator

import matplotlib.pyplot as plt
import numpy as np

from netradar.contracts import IqChunk, ModeSFrame
from netradar.dsp.preamble import detect_preambles


def detect_frames(chunks: Iterable[IqChunk]) -> Iterator[ModeSFrame]:

    SAMPLE_RATE = 2_000_000

    # Read the first 0.5 seconds of the recording.
    raw = np.fromfile(
        "adsb.iq",
        dtype=np.uint8,
        count=SAMPLE_RATE
    )

    assert len(raw) % 2 == 0

    # Convert interleaved bytes into I/Q pairs.
    iq = raw.reshape(-1, 2).astype(np.float32)

    I = iq[:, 0] - 127.5
    Q = iq[:, 1] - 127.5

    power = I**2 + Q**2

    PULSE_POSITIONS = [0, 2, 7, 9]
    QUIET_POSITIONS = [3, 4, 5, 6, 11, 12, 13, 14, 15]

    # We need 240 samples for a complete long ADS-B frame.
    n = min(len(power) - 239, 1_000_000)

    if n <= 0:
        raise ValueError("Not enough samples")

    positions = np.arange(n)

    pulse_values = np.stack([
        power[positions + offset]
        for offset in PULSE_POSITIONS
    ])

    quiet_values = np.stack([
        power[positions + offset]
        for offset in QUIET_POSITIONS
    ])

    pulse_mean = pulse_values.mean(axis=0)
    quiet_mean = quiet_values.mean(axis=0)
    pulse_min = pulse_values.min(axis=0)

    score = pulse_mean / (quiet_mean + 1e-6)

    print("Power percentiles:",
        np.percentile(power, [50, 90, 99, 99.9, 100]))

    print("Maximum score:", score.max())

    print("Score > 3:",
        np.sum(score > 3.0))

    print("Score > 2:",
        np.sum(score > 2.0))

    print("Score > 1.5:",
        np.sum(score > 1.5))

    print("Minimum pulse condition:",
        np.sum(pulse_min > 1.5 * quiet_mean))

    print("Both conditions:",
        np.sum(
            (score > 3.0)
            & (pulse_min > 1.5 * quiet_mean)
        ))

    # Detect preambles
    detections = detect_preambles(power)

    print("Number of candidates:", len(detections))
    print("First detections:", detections[:10])

    # Plot the first detected candidate
    if detections:

        start = detections[0]

        window = power[start:start + 240]

        time_us = np.arange(len(window)) / SAMPLE_RATE * 1e6

        plt.figure(figsize=(12, 4))

        plt.plot(time_us, window)

        plt.axvspan(
            0, 8,
            alpha=0.15,
            label="Preamble"
        )

        plt.xlabel("Time (µs)")
        plt.ylabel("Squared magnitude")

        plt.title(
            f"Candidate ADS-B Frame at Sample {start}"
        )

        plt.legend()
        plt.grid(True)
        plt.tight_layout()

        plt.savefig("detected_frame.png")

    else:
        print("No candidates detected.")

def demodulate_frame(power: np.ndarray, start: int):
    # Skip 8 us preamble (16 samples)
    data_start = start + 16

    # Read 112 bits * 2 samples
    data = power[data_start:data_start + 224]

    if len(data) != 224:
        return None

    # Each bit consists of two samples
    first_half = data[0::2]
    second_half = data[1::2]

    # PPM decoding
    bits = (first_half > second_half).astype(np.uint8)

    # Convert 112 bits into 14 bytes
    frame = np.packbits(bits, bitorder="big")

    return frame.tobytes()

if __name__ == "__main__":
    detect_frames([])