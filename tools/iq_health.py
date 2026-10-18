#!/usr/bin/env python3
"""IQ health check for a CU8 capture: DC offset, clipping, noise floor and peaks.

Usage: python3 tools/iq_health.py <file.cu8> [--chunk-samples N] [--per-chunk]

Every (I, Q) byte pair is counted into a 65536-bin histogram (numpy if it is
installed, otherwise pure Python), and all statistics are computed exactly from
that histogram. Magnitude = sqrt((I-127.5)**2 + (Q-127.5)**2).
"""
import argparse
from collections import Counter
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netradar.acquisition.receiver import replay_cu8  # noqa: E402


def make_histogrammer():
    """Return (name, fn) where fn(data) -> {code: count}, code = I + 256 * Q."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        def hist(data):
            counts = np.bincount(np.frombuffer(data, dtype='<u2'), minlength=65536)
            codes = np.nonzero(counts)[0]
            return dict(zip(codes.tolist(), counts[codes].tolist()))
        return 'numpy', hist

    def hist(data):
        counts = Counter(memoryview(data).cast('H'))
        if sys.byteorder == 'big':
            return {((k & 0xFF) << 8) | (k >> 8): v for k, v in counts.items()}
        return counts
    return 'pure Python', hist


def summarize(hist):
    n = sum(hist.values())
    if not n:
        return None
    sum_i = sum_q = clipped = 0
    by_mag = []
    for code, count in hist.items():
        i, q = code & 0xFF, code >> 8
        sum_i += i * count
        sum_q += q * count
        clipped += count * ((i in (0, 255)) + (q in (0, 255)))
        by_mag.append((math.hypot(i - 127.5, q - 127.5), count))
    by_mag.sort()
    seen = 0
    for median, count in by_mag:
        seen += count
        if 2 * seen >= n:
            break
    peak = by_mag[-1][0]
    return {
        'samples': n,
        'mean_i': sum_i / n,
        'mean_q': sum_q / n,
        'clip_pct': 100 * clipped / (2 * n),
        'median_mag': median,
        'peak_mag': peak,
        'peak_to_noise_db': 20 * math.log10(peak / median) if median else math.inf,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('path')
    p.add_argument('--chunk-samples', type=int, default=262_144)
    p.add_argument('--per-chunk', action='store_true', help='also print one line per chunk')
    args = p.parse_args(argv)

    engine, hist = make_histogrammer()
    total = Counter()
    rate = freq = None
    if args.per_chunk:
        print(f"{'start':>10} {'samples':>8} {'mean_I':>7} {'mean_Q':>7} {'clip%':>7}"
              f" {'median':>7} {'peak':>7} {'pk/n dB':>8}")
    for chunk in replay_cu8(args.path, chunk_samples=args.chunk_samples):
        rate, freq = chunk.sample_rate_hz, chunk.center_frequency_hz
        h = hist(chunk.data)
        total.update(h)
        if args.per_chunk:
            s = summarize(h)
            print(f"{chunk.start_sample_index:>10} {s['samples']:>8} {s['mean_i']:>7.2f} {s['mean_q']:>7.2f}"
                  f" {s['clip_pct']:>7.3f} {s['median_mag']:>7.2f} {s['peak_mag']:>7.1f}"
                  f" {s['peak_to_noise_db']:>8.1f}")

    s = summarize(total)
    print(f"file:        {args.path}  ({engine})")
    if s is None:
        print('samples:     0 (empty capture)')
        return 1
    print(f"samples:     {s['samples']} ({s['samples'] / rate:.3f} s at {rate / 1e6:.3f} MS/s,"
          f" center {freq / 1e6:.3f} MHz)")
    print(f"DC mean:     I={s['mean_i']:.2f} ({s['mean_i'] - 127.5:+.2f})"
          f"  Q={s['mean_q']:.2f} ({s['mean_q'] - 127.5:+.2f})  [ideal 127.5]")
    print(f"clipping:    {s['clip_pct']:.3f} % of bytes at 0 or 255")
    print(f"noise floor: {s['median_mag']:.2f} (median magnitude)")
    print(f"peak:        {s['peak_mag']:.2f} (max magnitude, full scale ~180)")
    print(f"peak/noise:  {s['peak_to_noise_db']:.1f} dB")
    if s['clip_pct'] > 1:
        print('hint:        >1 % clipping -- gain is probably too high')
    if s['peak_to_noise_db'] < 10:
        print('hint:        weak peaks -- check antenna and gain; maybe no aircraft nearby')
    return 0


if __name__ == '__main__':
    sys.exit(main())
