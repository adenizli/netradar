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

        # power[base + offset:end + offset] is a view, so nothing is copied;
        # the sums run in the same order as the former stacked mean.
        def at(offset):
            return power[base + offset:end + offset]

        # Average energy in pulse and quiet regions (accumulated in place)
        pulse_mean = at(PULSE_POSITIONS[0]).copy()
        pulse_min = pulse_mean.copy()
        for offset in PULSE_POSITIONS[1:]:
            np.add(pulse_mean, at(offset), out=pulse_mean)
            # Weakest expected pulse
            np.minimum(pulse_min, at(offset), out=pulse_min)
        quiet_mean = at(QUIET_POSITIONS[0]).copy()
        for offset in QUIET_POSITIONS[1:]:
            np.add(quiet_mean, at(offset), out=quiet_mean)
        pulse_mean /= len(PULSE_POSITIONS)
        quiet_mean /= len(QUIET_POSITIONS)

        # Local energy contrast
        score = pulse_mean / (quiet_mean + 1e-6)

        # Experimental detection thresholds
        valid = (
            (score > 3.0)
            & (pulse_min > 1.5 * quiet_mean)
        )

        # Do not let an earlier false candidate suppress a nearby real frame.
        # Streaming overlap is handled by the 239-sample tail in detect_frames;
        # only the decoder decides whether a candidate has a valid CRC.
        detections.extend((np.flatnonzero(valid) + base).tolist())

    return detections
