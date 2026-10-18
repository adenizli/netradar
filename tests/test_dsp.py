from dataclasses import replace
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from netradar.acquisition.receiver import replay_cu8
from netradar.contracts import IqChunk
from netradar.decoder.decode import ADSBDecoder, crc24


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/synthetic_adsb_2msps.cu8"
HAS_DSP = find_spec("numpy") is not None
if HAS_DSP:
    import numpy as np
    from netradar.dsp.detector import demodulate_frame, detect_frames
    from netradar.dsp.preamble import detect_preambles


HEX = "8D4840D6202CC371C32CE0576098"


def encode_frame(hex_value=HEX):
    """Independent ideal PPM encoder for boundary tests, not metadata detection."""
    raw = bytearray(b"\x7f\x7f" * 240)
    for index in (0, 2, 7, 9):
        raw[index * 2:index * 2 + 2] = b"\xde\xb5"
    for bit, value in enumerate(f"{int(hex_value, 16):0112b}"):
        index = 16 + 2 * bit + (value == "0")
        raw[index * 2:index * 2 + 2] = b"\xde\xb5"
    return bytes(raw)


class ReplayTest(unittest.TestCase):
    def test_chunks_preserve_all_bytes_and_complex_sample_offsets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.cu8"
            raw = bytes(range(22))
            path.write_bytes(raw)
            chunks = list(replay_cu8(path, chunk_samples=4))
        self.assertEqual([c.start_sample_index for c in chunks], [0, 4, 8])
        self.assertEqual([c.sample_count for c in chunks], [4, 4, 3])
        self.assertEqual(b"".join(c.data for c in chunks), raw)
        self.assertTrue(all(c.sample_rate_hz == 2_000_000 for c in chunks))

    def test_empty_file_and_invalid_sizes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.cu8"
            path.write_bytes(b"")
            self.assertEqual(list(replay_cu8(path)), [])
            for size in (0, -1, True, 1.5):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    list(replay_cu8(path, chunk_samples=size))
            with self.assertRaises(ValueError):
                list(replay_cu8(path, sample_rate_hz=0))

    def test_truncated_byte_pair_is_rejected_before_emitting_partial_capture(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.cu8"
            path.write_bytes(b"\x7f" * 9)
            with self.assertRaisesRegex(ValueError, "unpaired"):
                next(replay_cu8(path, chunk_samples=2, strict=True))


@unittest.skipUnless(HAS_DSP, "Install requirements-dsp.txt for IQ/DSP tests")
class DSPTest(unittest.TestCase):
    def test_real_iq_bytes_recover_fixture_hex_and_exact_offsets(self):
        # Expected metadata is the assertion oracle, never passed to the detector.
        metadata = json.loads((FIXTURE.with_suffix(".json")).read_text())
        candidates = list(detect_frames(replay_cu8(FIXTURE), source="synthetic"))
        good = [frame for frame in candidates if crc24(bytes.fromhex(frame.hex)) == 0]
        self.assertEqual([{"hex": f.hex, "sample_index": f.sample_index} for f in good],
                         metadata["expected_frames"])
        self.assertTrue(all(f.source == "synthetic" and f.observed_at_unix_ms is None for f in good))

    def test_every_possible_frame_split_recovers_once(self):
        raw = encode_frame()
        for split in range(1, 240):
            with self.subTest(split=split):
                chunks = [IqChunk(raw[:split * 2], 5000), IqChunk(raw[split * 2:], 5000 + split)]
                frames = list(detect_frames(chunks, source="synthetic"))
                self.assertEqual([(f.hex, f.sample_index) for f in frames], [(HEX, 5000)])

    def test_false_preamble_does_not_suppress_nearby_real_frame(self):
        raw = bytearray(b"\x7f\x7f" + encode_frame())
        for index in (0, 2, 7, 9):
            raw[index * 2:index * 2 + 2] = b"\xde\xb5"
        frames = list(detect_frames([IqChunk(bytes(raw), 0)]))
        valid = [f for f in frames if crc24(bytes.fromhex(f.hex)) == 0]
        self.assertEqual([(f.hex, f.sample_index) for f in valid], [(HEX, 1)])

    def test_one_sample_chunks_and_empty_chunks_do_not_duplicate_frames(self):
        raw = encode_frame()
        chunks = [IqChunk(raw[n:n + 2], n // 2) for n in range(0, len(raw), 2)]
        chunks.insert(100, IqChunk(b"", 100))
        frames = list(detect_frames(chunks))
        self.assertEqual([(f.hex, f.sample_index) for f in frames], [(HEX, 0)])

    def test_adjacent_frames_and_last_complete_frame_are_not_lost(self):
        raw = b"\x7f\x7f" * 5 + encode_frame() + encode_frame()
        for size in (239, 240, 241, 8192):
            with self.subTest(size=size):
                chunks = (IqChunk(raw[n * 2:(n + size) * 2], n) for n in range(0, len(raw) // 2, size))
                frames = list(detect_frames(chunks))
                self.assertEqual([(f.hex, f.sample_index) for f in frames], [(HEX, 5), (HEX, 245)])

    def test_truncated_frame_noise_and_empty_stream_emit_nothing(self):
        self.assertEqual(list(detect_frames([])), [])
        self.assertEqual(list(detect_frames([IqChunk(encode_frame()[:-2], 0)])), [])
        self.assertEqual(list(detect_frames([IqChunk(b"\x7f\x7f" * 1000, 0)])), [])

    def test_gap_never_splices_two_halves_into_a_fake_frame(self):
        raw = encode_frame()
        chunks = [IqChunk(raw[:200], 0), IqChunk(raw[200:], 101), IqChunk(raw, 1000)]
        frames = list(detect_frames(chunks))
        self.assertEqual([(f.hex, f.sample_index) for f in frames], [(HEX, 1000)])

    def test_overlapping_or_rewound_chunks_are_rejected(self):
        for offset in (0, 99):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "overlap|rewind"):
                list(detect_frames([IqChunk(b"\x7f\x7f" * 100, 0), IqChunk(encode_frame(), offset)]))

    def test_wrong_sample_rate_or_frequency_is_explicit_error(self):
        frame = IqChunk(encode_frame(), 0)
        for bad in (replace(frame, sample_rate_hz=2_400_000), replace(frame, center_frequency_hz=978_000_000)):
            with self.subTest(chunk=bad), self.assertRaises(ValueError):
                list(detect_frames([bad]))

    def test_observation_time_comes_only_from_explicit_capture_anchor(self):
        chunks = [IqChunk(encode_frame(), 2_001_000)]
        self.assertIsNone(list(detect_frames(chunks))[0].observed_at_unix_ms)
        frame = list(detect_frames(chunks, capture_start_unix_ms=1_000_000))[0]
        self.assertEqual(frame.observed_at_unix_ms, 1_001_000)
        with self.assertRaises(ValueError):
            list(detect_frames(chunks, capture_start_unix_ms=-1))

    def test_demodulation_rejects_ambiguous_half_bits_and_short_data(self):
        iq = np.frombuffer(encode_frame(), dtype=np.uint8).reshape(-1, 2).astype(np.float32) - 127.5
        power = np.square(iq).sum(axis=1)
        self.assertEqual(demodulate_frame(power, 0), bytes.fromhex(HEX))
        power[16] = power[17]
        self.assertIsNone(demodulate_frame(power, 0))
        self.assertIsNone(demodulate_frame(power[:-1], 0))
        self.assertIsNone(demodulate_frame(power, -1))

    def test_dsp_passes_bad_crc_candidate_but_decoder_drops_it(self):
        bad = HEX[:-1] + "9"
        frames = list(detect_frames([IqChunk(encode_frame(bad), 0)]))
        self.assertEqual([f.hex for f in frames], [bad])
        self.assertEqual(ADSBDecoder().decode(frames[0]), [])

    def test_generator_yields_before_consuming_the_next_chunk(self):
        def source():
            yield IqChunk(encode_frame(), 0)
            raise AssertionError("Detector read ahead instead of yielding")
        frames = detect_frames(source())
        self.assertEqual(next(frames).hex, HEX)
        frames.close()

    def test_closing_detector_also_closes_its_source(self):
        closed = []
        def source():
            try:
                yield IqChunk(encode_frame(), 0)
                yield IqChunk(encode_frame(), 240)
            finally:
                closed.append(True)
        frames = detect_frames(source())
        next(frames)
        frames.close()
        self.assertEqual(closed, [True])

    def test_preamble_at_internal_batch_boundary_is_not_lost(self):
        power = np.ones(100240, dtype=np.float32)
        for offset in (0, 2, 7, 9):
            power[100000 + offset] = 20
        self.assertIn(100000, detect_preambles(power))

    def test_processing_is_independent_of_working_directory(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run([
                sys.executable, "-m", "netradar.pipeline", "--iq", str(FIXTURE), "--source", "synthetic",
            ], cwd=directory, env={**os.environ, "PYTHONPATH": str(ROOT)},
                text=True, capture_output=True, check=True)
        self.assertEqual(len(result.stdout.splitlines()), 7)
        self.assertEqual(result.stderr, "")

    def test_iq_cli_is_not_metadata_fixture_shortcut(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bytes-only.cu8"
            path.write_bytes(FIXTURE.read_bytes())  # No metadata sidecar exists here.
            result = subprocess.run([
                sys.executable, "-m", "netradar.pipeline", "--iq", str(path),
                "--source", "synthetic", "--chunk-samples", "241",
            ], cwd=ROOT, text=True, capture_output=True, check=True)
        updates = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(updates), 7)
        self.assertAlmostEqual(updates[1]["data"]["latitude"], 52.2657801741, places=8)
        self.assertEqual(updates[2]["data"]["callsign"], "KLM1023")

    def test_iq_cli_rejects_unsupported_sample_rate(self):
        result = subprocess.run([
            sys.executable, "-m", "netradar.pipeline", "--iq", str(FIXTURE),
            "--sample-rate-hz", "2400000",
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("2,000,000", result.stderr)


if __name__ == "__main__":
    unittest.main()
