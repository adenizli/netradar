import numpy as np
from preamble import detect_preambles

# 500 samples of background noise
power = np.ones(500, dtype=np.float32)

# Insert an ideal preamble at sample 100
start = 100

pulse_positions = [0, 2, 7, 9]

for offset in pulse_positions:
    power[start + offset] = 20.0

# Run our detector
detections = detect_preambles(power)

print("Detected preambles:", detections)