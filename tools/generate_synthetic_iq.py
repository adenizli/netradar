#!/usr/bin/env python3
"""Generate simple PPM baseband IQ fixture. NOT live RF and NOT suitable for transmission."""
from pathlib import Path
import json
import random

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'fixtures'
F.mkdir(parents=True, exist_ok=True)
FRAMES = json.loads((F / 'frames.json').read_text())['frames']
SAMPLE_RATE = 2_000_000
TOTAL_SAMPLES = 560_000
OFFSETS = [10_000, 75_000, 145_000, 220_000, 300_000, 420_000, 530_000]
assert len(FRAMES) == len(OFFSETS)


def make():
    rng = random.Random(1090)
    samples = bytearray(TOTAL_SAMPLES * 2)
    for n in range(TOTAL_SAMPLES):
        samples[2*n] = 127 + rng.randrange(-3, 4)
        samples[2*n+1] = 127 + rng.randrange(-3, 4)
    expected = []

    def pulse(n):
        assert n < TOTAL_SAMPLES
        samples[2*n] = 222 + rng.randrange(-3, 4)
        samples[2*n+1] = 181 + rng.randrange(-3, 4)

    for frame, start in zip(FRAMES, OFFSETS):
        # Mode S preamble: 0, 1.0, 3.5 and 4.5 us pulses.
        # 2 MS/s => sample indices 0, 2, 7, 9; each pulse occupies one sample.
        for off in (0, 2, 7, 9):
            pulse(start + off)
        bitstream = ''.join(f'{byte:08b}' for byte in bytes.fromhex(frame['hex']))
        assert len(bitstream) == 112
        # Data begins 8 us after preamble => 16 samples.
        # PPM: 1 = first 0.5us high; 0 = second 0.5us high.
        for i, bit in enumerate(bitstream):
            pulse(start + 16 + 2*i + (0 if bit == '1' else 1))
        expected.append({"sample_index":start, "hex":frame['hex']})

    out=F/'synthetic_adsb_2msps.cu8'
    out.write_bytes(samples)
    meta={"kind":"synthetic_baseband_iq", "file":out.name,
          "sample_format":"cu8_interleaved_iq", "sample_rate_hz":SAMPLE_RATE,
          "center_frequency_hz":1_090_000_000,
          "total_complex_samples":TOTAL_SAMPLES,
          "duration_seconds":TOTAL_SAMPLES/SAMPLE_RATE,
          "generator_seed":1090, "bits_per_frame":112,
          "preamble_start_sample_indices":OFFSETS,
          "expected_frames":expected,
          "notes":"Ideal sample-aligned, non-overlapping pulses; fake noise, not a hardware capture."}
    (F/'synthetic_adsb_2msps.json').write_text(json.dumps(meta,indent=2)+'\n')
    print('Created',out, f'({len(samples):,} bytes), {len(expected)} frames')


if __name__=='__main__':
    make()
