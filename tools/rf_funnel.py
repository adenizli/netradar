#!/usr/bin/env python3
"""Where do messages get lost? Count each DSP/decoder stage over a CU8 capture.

Usage: python3 tools/rf_funnel.py <file.cu8> [--chunk-samples N]

Stages: preamble candidates -> DF17 prefix -> at most MAX_TIED_BITS tied
half-bits (read as 0) -> CRC ok / single-bit fixable / bad. The decoder repairs
single-bit errors only for aircraft already heard with a clean CRC.
"""
import argparse
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from netradar.acquisition.receiver import replay_cu8  # noqa: E402
from netradar.decoder.decode import SINGLE_BIT_SYNDROMES, crc24  # noqa: E402
from netradar.dsp.detector import _POWER_LUT  # noqa: E402
from netradar.dsp.ppm import MAX_TIED_BITS  # noqa: E402
from netradar.dsp.preamble import FRAME_SAMPLES, detect_preambles  # noqa: E402

FRAME_OFFSETS = 16 + np.arange(224)


def classify(raw: bytes):
    """Return ('clean', raw) / ('fixed', corrected) / ('bad', raw)."""
    syndrome = crc24(raw)
    if syndrome == 0:
        return "clean", raw
    bit = SINGLE_BIT_SYNDROMES.get(syndrome)
    if bit is None:
        return "bad", raw
    value = int.from_bytes(raw, "big") ^ (1 << (111 - bit))
    fixed = value.to_bytes(14, "big")
    return ("fixed", fixed) if fixed[0] >> 3 == 17 else ("bad", raw)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path")
    p.add_argument("--chunk-samples", type=int, default=262_144)
    args = p.parse_args(argv)

    n = Counter()
    clean_icaos, fixed_icaos = set(), set()
    type_codes = Counter()
    last_clean = {}  # hex -> sample index, to spot one message decoded at nearby offsets
    tail = np.empty(0, dtype=np.float32)
    total_samples = 0
    for chunk in replay_cu8(args.path, chunk_samples=args.chunk_samples):
        total_samples += chunk.sample_count
        power = _POWER_LUT[np.frombuffer(chunk.data, dtype="<u2")]
        if tail.size:
            power = np.concatenate((tail, power))
        buffer_start = chunk.start_sample_index - len(tail)
        tail = power[-(FRAME_SAMPLES - 1):].copy()

        starts = np.asarray(detect_preambles(power), dtype=np.intp)
        n["candidates"] += starts.size
        if not starts.size:
            continue
        data = power[starts[:, None] + FRAME_OFFSETS]
        first, second = data[:, 0::2], data[:, 1::2]
        bits = first > second
        df17 = bits[:, 0] & ~bits[:, 1] & ~bits[:, 2] & ~bits[:, 3] & bits[:, 4]
        too_many_ties = (first == second).sum(axis=1) > MAX_TIED_BITS
        n["df17_prefix"] += int(df17.sum())
        n["tied"] += int((df17 & too_many_ties).sum())
        frames = np.packbits(bits, axis=1, bitorder="big")
        for i in np.flatnonzero(df17 & ~too_many_ties):
            kind, raw = classify(frames[i].tobytes())
            icao = raw[1:4].hex().upper()
            n[kind] += 1
            if kind == "clean":
                index = buffer_start + int(starts[i])
                hex_ = raw.hex().upper()
                if hex_ in last_clean and index - last_clean[hex_] <= 3:
                    n["clean_duplicate"] += 1
                last_clean[hex_] = index
                clean_icaos.add(icao)
                type_codes[raw[4] >> 3] += 1
            elif kind == "fixed":
                fixed_icaos.add(icao)

    seconds = total_samples / 2_000_000
    demodulated = n["df17_prefix"] - n["tied"]
    print(f"file:                 {args.path}  ({seconds:.1f} s)")
    print(f"preamble candidates:  {n['candidates']:>9}  ({n['candidates'] / max(seconds, 1e-9):.0f}/s)")
    print(f"  DF17 prefix:        {n['df17_prefix']:>9}")
    print(f"  dropped for ties:   {n['tied']:>9}  (more than {MAX_TIED_BITS} equal half-bit pairs)")
    print(f"  demodulated:        {demodulated:>9}")
    print(f"    CRC ok:           {n['clean']:>9}")
    print(f"      same frame at nearby offset: {n['clean_duplicate']}  (duplicate events)")
    print(f"    1-bit fixable:    {n['fixed']:>9}  (repaired if the aircraft was already heard)")
    print(f"    CRC bad:          {n['bad']:>9}")
    print(f"aircraft (ICAO), CRC ok:        {len(clean_icaos)}")
    print(f"  1-bit fixes, already-seen ICAO: {len(fixed_icaos & clean_icaos)}"
          f" / new ICAO: {len(fixed_icaos - clean_icaos)} (never repaired)")
    names = {**{tc: "identification" for tc in range(1, 5)}, 19: "velocity",
             **{tc: "airborne position" for tc in [*range(9, 19), *range(20, 23)]}}
    kinds = Counter()
    for tc, count in type_codes.items():
        kinds[names.get(tc, f"other TC {tc}")] += count
    print("CRC-ok message types: " + (", ".join(f"{k} {v}" for k, v in kinds.most_common()) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
