import json
from pathlib import Path
import unittest

from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder, crc24, parse_header
from tools.verify_fixtures import crc24 as reference_crc24


ROOT = Path(__file__).resolve().parents[1]


def make_identification_frame(codes: list[int], type_code: int = 4) -> ModeSFrame:
    """Build an identification test input with independently computed parity."""
    raw = bytearray.fromhex("8D4840D600000000000000000000")
    raw[4] = (type_code << 3) | 5  # Nonzero category must not enter the callsign.
    packed_characters = 0
    for code in codes:
        packed_characters = (packed_characters << 6) | code
    raw[5:11] = packed_characters.to_bytes(6, "big")
    # This register-based oracle generates parity from the 11 data bytes.
    raw[11:14] = reference_crc24(raw[:11]).to_bytes(3, "big")
    return ModeSFrame(hex=raw.hex().upper(), sample_index=10000, source="synthetic")


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


class IdentificationTest(unittest.TestCase):
    def test_published_callsigns_and_event_contract(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        decoder = ADSBDecoder()
        for example in examples["frames"]:
            if "expected_callsign" not in example:
                continue
            with self.subTest(frame=example["hex"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                self.assertEqual(decoder.decode(frame), [DecodedEvent(
                    icao=example["icao"],
                    event_type="identification",
                    fields={"callsign": example["expected_callsign"]},
                    raw_frame=example["hex"],
                    observed_at_unix_ms=None,
                )])

    def test_all_identification_types_and_character_ranges(self):
        for type_code in (1, 2, 3, 4):
            with self.subTest(type_code=type_code):
                frame = make_identification_frame(
                    [1, 26, 48, 57, 32, 1, 2, 32], type_code=type_code,
                )
                self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                events = ADSBDecoder().decode(frame)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].fields["callsign"], "AZ09 AB")

    def test_reserved_character_codes_do_not_emit_identity(self):
        for reserved in (0, 27, 31, 33, 47, 58, 63):
            with self.subTest(reserved=reserved):
                frame = make_identification_frame([1, 2, 3, reserved, 32, 32, 32, 32])
                self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                self.assertEqual(ADSBDecoder().decode(frame), [])

    def test_blank_callsign_does_not_emit_identity(self):
        frame = make_identification_frame([32] * 8)
        self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
        self.assertEqual(ADSBDecoder().decode(frame), [])

    def test_corrupted_identification_does_not_emit_identity(self):
        raw = bytearray.fromhex("8D4840D6202CC371C32CE0576098")
        raw[-1] ^= 1
        frame = ModeSFrame(hex=raw.hex().upper(), sample_index=0)
        self.assertEqual(ADSBDecoder().decode(frame), [])

    def test_other_valid_message_types_are_skipped_for_now(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        for example in examples["frames"]:
            if example["type_code"] in (1, 2, 3, 4):
                continue
            with self.subTest(type_code=example["type_code"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                self.assertEqual(ADSBDecoder().decode(frame), [])


if __name__ == "__main__":
    unittest.main()
