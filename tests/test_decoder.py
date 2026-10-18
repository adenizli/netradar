import json
from pathlib import Path
import unittest

from netradar.contracts import ModeSFrame
from netradar.decoder.decode import ADSBDecoder, crc24, parse_header


ROOT = Path(__file__).resolve().parents[1]


class HeaderTest(unittest.TestCase):
    def test_published_example_headers(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        for example in examples["frames"]:
            with self.subTest(frame=example["hex"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                header = parse_header(frame)
                self.assertEqual(header.df, example["df"])
                self.assertEqual(header.icao, example["icao"])
                self.assertEqual(header.type_code, example["type_code"])

    def test_unsupported_format_preserves_leading_zero_bits(self):
        # A parsing-only input, not a CRC-valid ADS-B example.
        frame = ModeSFrame(hex="0D4840D6202CC371C32CE0576098", sample_index=0)
        header = parse_header(frame)
        self.assertEqual(header.df, 1)
        self.assertIsNone(header.icao)
        self.assertIsNone(header.type_code)


class CRCTest(unittest.TestCase):
    def test_published_examples_pass_crc(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        for example in examples["frames"]:
            with self.subTest(frame=example["hex"]):
                self.assertEqual(crc24(bytes.fromhex(example["hex"])), 0)

    def test_known_parity_calculation(self):
        # Published example: divide the 88 data bits followed by 24 zero bits.
        data_with_zero_parity = bytes.fromhex("8D406B902015A678D4D220000000")
        self.assertEqual(crc24(data_with_zero_parity), 0xAA4BDA)

    def test_every_single_bit_error_is_detected(self):
        original = int("8D40621D58C382D690C8AC2863A7", 16)
        for bit_index in range(112):
            with self.subTest(bit_index=bit_index):
                corrupted = (original ^ (1 << bit_index)).to_bytes(14, "big")
                self.assertNotEqual(crc24(corrupted), 0)


class DecoderValidationTest(unittest.TestCase):
    def test_crc_bad_fixture_is_rejected(self):
        decoder = ADSBDecoder()
        examples = json.loads((ROOT / "fixtures" / "frames_bad.json").read_text())
        for example in examples["frames"]:
            with self.subTest(frame=example["hex"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                self.assertEqual(decoder.decode(frame), [])

    def test_crc_valid_unsupported_format_is_rejected(self):
        # Construct a CRC-valid DF18 input to isolate the format check.
        payload_with_zeros = bytes.fromhex("954840D6202CC371C32CE0000000")
        parity = crc24(payload_with_zeros).to_bytes(3, "big")
        raw = payload_with_zeros[:-3] + parity
        self.assertEqual(crc24(raw), 0)
        frame = ModeSFrame(hex=raw.hex().upper(), sample_index=0)
        self.assertEqual(ADSBDecoder().decode(frame), [])


if __name__ == "__main__":
    unittest.main()
