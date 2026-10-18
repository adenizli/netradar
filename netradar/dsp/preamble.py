import numpy as np

SAMPLE_RATE = 2_000_000

# ADS-B preamble: 0, 1, 3.5, 4.5 microseconds
PULSE_POSITIONS = np.array([0, 2, 7, 9])

# Positions expected to have low signal energy
QUIET_POSITIONS = np.array([
    3, 4, 5, 6, 11, 12, 13, 14, 15
])

# 8 us preamble + 112 us data
FRAME_SAMPLES = 240


def detect_preambles(power: np.ndarray) -> list[int]:

    detections = []

    # We need enough samples for a complete frame.
    max_start = len(power) - FRAME_SAMPLES + 1

    if max_start <= 0:
        return detections

    # Process in batches to limit memory consumption.
    BATCH_SIZE = 100_000

    for base in range(0, max_start, BATCH_SIZE):

        end = min(base + BATCH_SIZE, max_start)

        positions = np.arange(base, end)

        # Expected pulse positions
        pulse_values = np.stack([
            power[positions + offset]
            for offset in PULSE_POSITIONS
        ])

        # Expected quiet positions
        quiet_values = np.stack([
            power[positions + offset]
            for offset in QUIET_POSITIONS
        ])

        # Average energy in pulse and quiet regions
        pulse_mean = pulse_values.mean(axis=0)
        quiet_mean = quiet_values.mean(axis=0)

        # Weakest expected pulse
        pulse_min = pulse_values.min(axis=0)

        # Local energy contrast
        score = pulse_mean / (quiet_mean + 1e-6)

        # Experimental detection thresholds
        valid = (
            (score > 3.0)
            & (pulse_min > 1.5 * quiet_mean)
        )

        candidates = positions[valid]

        # Do not let an earlier false candidate suppress a nearby real frame.
        # Streaming overlap is handled by the 239-sample tail in detect_frames;
        # only the decoder decides whether a candidate has a valid CRC.
        detections.extend(candidates.tolist())

    return detections
