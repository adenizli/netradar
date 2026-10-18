#!/usr/bin/env python3
"""Independent fixture sanity checks; this is a test oracle, not the team's decoder."""
import json
from pathlib import Path

F = Path(__file__).resolve().parents[1] / 'fixtures'


def crc24(data: bytes) -> int:
    """Mode-S CRC-24 bitwise remainder over full message (DF17 should yield zero)."""
    reg=0
    for byte in data:
        reg ^= byte << 16
        for _ in range(8):
            reg <<= 1
            if reg & 0x1000000:
                reg ^= 0x1FFF409
    return reg & 0xFFFFFF


def known_frames():
    return json.loads((F/'frames.json').read_text())['frames']


def sample_bit(iq, n):
    i=iq[2*n]-127.5
    q=iq[2*n+1]-127.5
    return i*i+q*q


def verify():
    good=known_frames()
    assert len(good)>=6
    for frame in good:
        msg=bytes.fromhex(frame['hex'])
        assert len(msg)==14 and msg[0] >> 3 == 17
        assert crc24(msg)==0, f"Invalid CRC: {frame['hex']}"
        assert frame['icao']==msg[1:4].hex().upper()
        assert frame['type_code']==(msg[4]>>3)

    for frame in json.loads((F/'frames_bad.json').read_text())['frames']:
        assert crc24(bytes.fromhex(frame['hex']))!=0

    meta=json.loads((F/'synthetic_adsb_2msps.json').read_text())
    iq=(F/meta['file']).read_bytes()
    assert len(iq)==2*meta['total_complex_samples']
    for item in meta['expected_frames']:
        st=item['sample_index']
        for j in (0,2,7,9):
            assert sample_bit(iq, st+j)>1000
        actual=''
        for b in range(112):
            a=sample_bit(iq, st+16+2*b)
            c=sample_bit(iq, st+17+2*b)
            actual += '1' if a>c else '0'
        assert f'{int(actual,2):028X}'==item['hex'], item
    print(f"OK: {len(good)} CRC-valid real-example frames, bad-CRC rejection,"
          f" {len(meta['expected_frames'])} embedded synthetic frames at known offsets")


if __name__=='__main__':
    verify()
