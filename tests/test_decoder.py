from dataclasses import replace
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


def make_position_frame(
    alt_code: int, *, type_code: int = 11, odd: int = 0,
    cpr_lat: int = 0, cpr_lon: int = 0,
) -> ModeSFrame:
    """Build a position test input; include nonzero status and time flags."""
    payload = (
        f"{type_code:05b}" + "111" + f"{alt_code:012b}"
        + "1" + str(odd) + f"{cpr_lat:017b}" + f"{cpr_lon:017b}"
    )
    raw = bytearray.fromhex("8D40621D00000000000000000000")
    raw[4:11] = int(payload, 2).to_bytes(7, "big")
    raw[11:14] = reference_crc24(raw[:11]).to_bytes(3, "big")
    return ModeSFrame(hex=raw.hex().upper(), sample_index=0, source="synthetic")


class CPRPairTest(unittest.TestCase):
    EVEN = "8D40621D58C382D690C8AC2863A7"
    ODD = "8D40621D58C386435CC412692AD6"

    def test_single_frame_waits_then_pair_is_returned_even_first(self):
        decoder = ADSBDecoder()
        even = ModeSFrame(hex=self.EVEN, sample_index=10000)
        odd = ModeSFrame(hex=self.ODD, sample_index=75000)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        first_event = decoder.decode(even)[0]
        self.assertIsNone(first_event.fields["latitude"])
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        second_event = decoder.decode(odd)[0]
        self.assertIsNone(second_event.fields["longitude"])
        self.assertEqual(decoder.get_cpr_pair("40621D"), (even, odd))

    def test_odd_can_arrive_before_even(self):
        decoder = ADSBDecoder()
        odd = ModeSFrame(hex=self.ODD, sample_index=10000)
        even = ModeSFrame(hex=self.EVEN, sample_index=75000)
        decoder.decode(odd)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        decoder.decode(even)
        self.assertEqual(decoder.get_cpr_pair("40621D"), (even, odd))

    def test_exact_ten_second_limit_and_one_sample_over(self):
        for separation, is_ready in ((20_000_000, True), (20_000_001, False)):
            with self.subTest(separation=separation):
                decoder = ADSBDecoder()
                even = ModeSFrame(hex=self.EVEN, sample_index=0)
                odd = ModeSFrame(hex=self.ODD, sample_index=separation)
                decoder.decode(even)
                decoder.decode(odd)
                if is_ready:
                    self.assertEqual(decoder.get_cpr_pair("40621D"), (even, odd))
                else:
                    self.assertIsNone(decoder.get_cpr_pair("40621D"))

    def test_pair_window_uses_configured_sample_rate(self):
        for separation, is_ready in ((10_000_000, True), (10_000_001, False)):
            with self.subTest(separation=separation):
                decoder = ADSBDecoder(sample_rate_hz=1_000_000)
                decoder.decode(ModeSFrame(hex=self.EVEN, sample_index=0))
                decoder.decode(ModeSFrame(hex=self.ODD, sample_index=separation))
                self.assertEqual(decoder.get_cpr_pair("40621D") is not None, is_ready)

    def test_aircraft_are_kept_separate(self):
        decoder = ADSBDecoder()
        even = ModeSFrame(hex=self.EVEN, sample_index=10000)
        other_odd = ModeSFrame(hex="8D40058B58C904A87F402D3B8C59", sample_index=20000)
        decoder.decode(even)
        decoder.decode(other_odd)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        self.assertIsNone(decoder.get_cpr_pair("40058B"))
        odd = ModeSFrame(hex=self.ODD, sample_index=30000)
        decoder.decode(odd)
        self.assertEqual(decoder.get_cpr_pair("40621D"), (even, odd))
        self.assertIsNone(decoder.get_cpr_pair("40058B"))

    def test_newer_same_format_replaces_old_but_delayed_frame_does_not(self):
        decoder = ADSBDecoder()
        old_even = ModeSFrame(hex=self.EVEN, sample_index=10000)
        odd = ModeSFrame(hex=self.ODD, sample_index=20000)
        new_even = ModeSFrame(hex=self.EVEN, sample_index=30000)
        for frame in (old_even, odd, new_even, old_even):
            decoder.decode(frame)
        self.assertEqual(decoder.get_cpr_pair("40621D"), (new_even, odd))

    def test_corrupted_frames_do_not_replace_or_expire_cached_messages(self):
        decoder = ADSBDecoder()
        even = ModeSFrame(hex=self.EVEN, sample_index=10000)
        odd = ModeSFrame(hex=self.ODD, sample_index=20000)
        decoder.decode(even)
        decoder.decode(odd)
        bad_hex = f"{int(self.ODD, 16) ^ 1:028X}"
        for sample_index in (30000, 100_000_000):
            with self.subTest(sample_index=sample_index):
                bad = ModeSFrame(hex=bad_hex, sample_index=sample_index)
                self.assertEqual(decoder.decode(bad), [])
                self.assertEqual(decoder.get_cpr_pair("40621D"), (even, odd))

    def test_old_pair_expires_as_capture_advances_and_can_form_again(self):
        decoder = ADSBDecoder()
        decoder.decode(ModeSFrame(hex=self.EVEN, sample_index=0))
        odd = ModeSFrame(hex=self.ODD, sample_index=1)
        decoder.decode(odd)
        self.assertIsNotNone(decoder.get_cpr_pair("40621D"))
        # A later identity message advances capture time for all aircraft.
        identity = ModeSFrame(hex="8D4840D6202CC371C32CE0576098", sample_index=20_000_002)
        decoder.decode(identity)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        # An expired delayed message must not restore the old pair.
        decoder.decode(odd)
        even = ModeSFrame(hex=self.EVEN, sample_index=20_000_003)
        decoder.decode(even)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        new_odd = replace(odd, sample_index=20_000_004)
        decoder.decode(new_odd)
        self.assertEqual(decoder.get_cpr_pair("40621D"), (even, new_odd))

    def test_reset_separates_captures_with_reused_sample_indices(self):
        decoder = ADSBDecoder()
        even = ModeSFrame(hex=self.EVEN, sample_index=100_000_000)
        odd = ModeSFrame(hex=self.ODD, sample_index=100_000_001)
        decoder.decode(even)
        decoder.decode(odd)
        decoder.reset()
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        new_odd = replace(odd, sample_index=0)
        decoder.decode(new_odd)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        new_even = replace(even, sample_index=1)
        decoder.decode(new_even)
        self.assertEqual(decoder.get_cpr_pair("40621D"), (new_even, new_odd))

    def test_nonpositive_sample_rate_is_rejected(self):
        for sample_rate in (0, -1):
            with self.subTest(sample_rate=sample_rate):
                with self.assertRaises(ValueError):
                    ADSBDecoder(sample_rate_hz=sample_rate)


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

    def test_velocity_message_is_skipped_for_now(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        for example in examples["frames"]:
            if example["type_code"] != 19:
                continue
            with self.subTest(type_code=example["type_code"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                self.assertEqual(ADSBDecoder().decode(frame), [])


class AirbornePositionTest(unittest.TestCase):
    def test_published_position_fields_and_event_contract(self):
        expected = (
            ("8D40621D58C382D690C8AC2863A7", "40621D", 38000, "even", 93000, 51372),
            ("8D40621D58C386435CC412692AD6", "40621D", 38000, "odd", 74158, 50194),
            ("8D40058B58C901375147EFD09357", "40058B", 39000, "even", 39848, 83951),
            ("8D40058B58C904A87F402D3B8C59", "40058B", 39000, "odd", 21567, 81965),
        )
        decoder = ADSBDecoder()
        for raw, icao, altitude, cpr_format, lat, lon in expected:
            with self.subTest(frame=raw):
                frame = ModeSFrame(hex=raw, sample_index=0)
                self.assertEqual(decoder.decode(frame), [DecodedEvent(
                    icao=icao,
                    event_type="airborne_position",
                    fields={
                        "altitude_ft": altitude,
                        "cpr_format": cpr_format,
                        "cpr_lat": lat,
                        "cpr_lon": lon,
                        "latitude": None,
                        "longitude": None,
                    },
                    raw_frame=raw,
                    observed_at_unix_ms=None,
                )])

    def test_barometric_type_range_and_cpr_boundaries(self):
        for type_code in range(9, 19):
            for odd in (0, 1):
                with self.subTest(type_code=type_code, odd=odd):
                    frame = make_position_frame(
                        0xC38, type_code=type_code, odd=odd,
                        cpr_lat=0, cpr_lon=131071,
                    )
                    self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                    fields = ADSBDecoder().decode(frame)[0].fields
                    self.assertEqual(fields["altitude_ft"], 38000)
                    self.assertEqual(fields["cpr_format"], "odd" if odd else "even")
                    self.assertEqual(fields["cpr_lat"], 0)
                    self.assertEqual(fields["cpr_lon"], 131071)
                    self.assertIsNone(fields["latitude"])
                    self.assertIsNone(fields["longitude"])

    def test_q_one_altitude_boundaries(self):
        for alt_code, altitude in ((0x010, -1000), (0x058, 0), (0xFFF, 50175)):
            with self.subTest(alt_code=alt_code):
                frame = make_position_frame(alt_code)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["altitude_ft"], altitude)

    def test_q_zero_keeps_cpr_but_reports_unknown_altitude(self):
        for alt_code in (0x000, 0xC28):
            with self.subTest(alt_code=alt_code):
                frame = make_position_frame(alt_code, cpr_lat=93000, cpr_lon=51372)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertIsNone(fields["altitude_ft"])
                self.assertEqual(fields["cpr_lat"], 93000)
                self.assertEqual(fields["cpr_lon"], 51372)

    def test_gnss_height_types_are_not_decoded_as_barometric(self):
        for type_code in (20, 21, 22):
            with self.subTest(type_code=type_code):
                frame = make_position_frame(0xC38, type_code=type_code)
                self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                self.assertEqual(ADSBDecoder().decode(frame), [])


if __name__ == "__main__":
    unittest.main()
