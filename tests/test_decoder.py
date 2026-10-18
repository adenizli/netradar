from dataclasses import replace
import json
from pathlib import Path
import unittest

from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder, cpr_nl, crc24, parse_header
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


def make_velocity_frame(
    *, subtype: int = 1, east_code: int = 9, north_code: int = 160,
    west: int = 0, south: int = 0,
    vertical_code: int = 0, descending: int = 0, barometric: int = 0,
) -> ModeSFrame:
    """Build a TC19 input with independently computed parity."""
    payload = (
        f"{19:05b}{subtype:03b}" + "11111"
        + f"{west:01b}{east_code:010b}{south:01b}{north_code:010b}"
        + f"{barometric:01b}{descending:01b}{vertical_code:09b}"
        + "1" * 10  # Reserved/height-difference bits must not enter the rate.
    )
    raw = bytearray.fromhex("8D48502000000000000000000000")
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
        self.assertAlmostEqual(second_event.fields["longitude"], 3.9389125279, places=8)
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


class CPRCoordinatesTest(unittest.TestCase):
    EVEN = "8D40621D58C382D690C8AC2863A7"
    ODD = "8D40621D58C386435CC412692AD6"

    def decode_pair(self, even: ModeSFrame, odd: ModeSFrame) -> DecodedEvent:
        decoder = ADSBDecoder()
        decoder.decode(even)
        return decoder.decode(odd)[0]

    def test_published_example_selects_newer_frame(self):
        # The 1090 MHz Riddle: even newer gives (52.257202, 3.919373).
        cases = (
            (75000, 10000, 52.2572021484375, 3.91937255859375, "even"),
            (10000, 75000, 52.26578017412606, 3.938912527901786, "odd"),
            (10000, 10000, 52.2572021484375, 3.91937255859375, "even"),
        )
        for even_index, odd_index, lat, lon, cpr_format in cases:
            with self.subTest(even_index=even_index, odd_index=odd_index):
                even = ModeSFrame(hex=self.EVEN, sample_index=even_index)
                odd = ModeSFrame(hex=self.ODD, sample_index=odd_index)
                event = self.decode_pair(even, odd)
                self.assertAlmostEqual(event.fields["latitude"], lat, places=8)
                self.assertAlmostEqual(event.fields["longitude"], lon, places=8)
                self.assertEqual(event.fields["cpr_format"], cpr_format)
                self.assertEqual(event.raw_frame, even.hex if cpr_format == "even" else odd.hex)

    def test_second_published_pair_matches_pymodes_reference(self):
        # pyModeS v2.21.1 tests/test_adsb.py, test_adsb_position.
        even = ModeSFrame(hex="8D40058B58C901375147EFD09357", sample_index=0)
        odd = ModeSFrame(hex="8D40058B58C904A87F402D3B8C59", sample_index=10_000_000)
        event = self.decode_pair(even, odd)
        self.assertEqual(event.icao, "40058B")
        self.assertEqual(event.fields["altitude_ft"], 39000)
        self.assertAlmostEqual(event.fields["latitude"], 49.81755, delta=0.00001)
        self.assertAlmostEqual(event.fields["longitude"], 6.08442, delta=0.00001)

    def test_southern_western_dateline_equator_and_polar_positions(self):
        # Synthetic CPR fields independently encoded from stationary positions.
        cases = (
            (0, 0, (0, 0), (0, 0)),
            (-33.9, -70.7, (45875, 49407), (58218, 75148)),
            (0, 179.9, (0, 63388), (0, 128960)),
            (0, -179.9, (0, 67684), (0, 2112)),
            (88, 45, (87381, 16384), (55342, 16384)),
            (-88, -45, (43691, 114688), (75730, 114688)),
            (90, 45, (0, 16384), (98304, 16384)),
            (-90, -45, (0, 114688), (32768, 114688)),
        )
        for lat, lon, even_cpr, odd_cpr in cases:
            for newer_odd in (False, True):
                with self.subTest(latitude=lat, longitude=lon, newer_odd=newer_odd):
                    even = replace(make_position_frame(
                        0xC38, cpr_lat=even_cpr[0], cpr_lon=even_cpr[1],
                    ), sample_index=0 if newer_odd else 1)
                    odd = replace(make_position_frame(
                        0xC38, odd=1, cpr_lat=odd_cpr[0], cpr_lon=odd_cpr[1],
                    ), sample_index=1 if newer_odd else 0)
                    event = self.decode_pair(even, odd)
                    self.assertAlmostEqual(event.fields["latitude"], lat, delta=0.0001)
                    self.assertAlmostEqual(event.fields["longitude"], lon, delta=0.0001)

    def test_different_nl_zones_keep_coordinates_unknown(self):
        # Even is just below 10.4704713 degrees; odd is just above it.
        even = make_position_frame(0xC38, cpr_lat=97657)
        odd = replace(make_position_frame(0xC38, odd=1, cpr_lat=93850), sample_index=1)
        event = self.decode_pair(even, odd)
        self.assertIsNone(event.fields["latitude"])
        self.assertIsNone(event.fields["longitude"])
        self.assertEqual(event.fields["altitude_ft"], 38000)

    def test_impossible_latitudes_keep_coordinates_unknown(self):
        # This pair implies a latitude of 180 degrees, outside the poles.
        even = make_position_frame(0xC38, cpr_lat=0)
        odd = replace(make_position_frame(0xC38, odd=1, cpr_lat=65536), sample_index=1)
        event = self.decode_pair(even, odd)
        self.assertIsNone(event.fields["latitude"])
        self.assertIsNone(event.fields["longitude"])

    def test_expired_pair_does_not_emit_coordinates(self):
        even = ModeSFrame(hex=self.EVEN, sample_index=0)
        odd = ModeSFrame(hex=self.ODD, sample_index=20_000_001)
        event = self.decode_pair(even, odd)
        self.assertIsNone(event.fields["latitude"])
        self.assertIsNone(event.fields["longitude"])

    def test_late_counterpart_uses_newest_altitude_and_raw_message(self):
        decoder = ADSBDecoder()
        newest_odd = replace(make_position_frame(
            0xC90, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=75000)
        decoder.decode(newest_odd)
        older_even = ModeSFrame(hex=self.EVEN, sample_index=10000)
        event = decoder.decode(older_even)[0]
        self.assertEqual(event.raw_frame, newest_odd.hex)
        self.assertEqual(event.fields["altitude_ft"], 39000)
        self.assertEqual(event.fields["cpr_format"], "odd")
        self.assertAlmostEqual(event.fields["latitude"], 52.2657801741, places=8)

    def test_unknown_altitude_does_not_block_coordinates(self):
        even = make_position_frame(0x000, cpr_lat=93000, cpr_lon=51372)
        odd = replace(make_position_frame(
            0x000, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=1)
        event = self.decode_pair(even, odd)
        self.assertIsNone(event.fields["altitude_ft"])
        self.assertAlmostEqual(event.fields["latitude"], 52.2657801741, places=8)

    def test_nl_equator_boundaries_and_poles(self):
        for lat, nl in ((0, 59), (10.47047129, 59), (10.47047131, 58),
                        (41.085, 45), (52.257202, 36), (87, 2), (88, 1), (90, 1)):
            for sign in (-1, 1):
                with self.subTest(latitude=sign * lat):
                    self.assertEqual(cpr_nl(sign * lat), nl)

    def test_nl_rejects_nonphysical_input(self):
        for latitude in (-91, 91, float("nan"), float("inf")):
            with self.subTest(latitude=latitude):
                with self.assertRaises(ValueError):
                    cpr_nl(latitude)


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
                    metadata={
                        "df": 17, "type_code": example["type_code"], "crc_valid": True,
                        "source": "rf", "sample_index": 0, "sample_rate_hz": 2_000_000,
                    },
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

class VelocityComponentsTest(unittest.TestCase):
    def test_published_velocity_components_and_event_contract(self):
        frame = ModeSFrame(hex="8D485020994409940838175B284F", sample_index=220000)
        events = ADSBDecoder().decode(frame)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.icao, "485020")
        self.assertEqual(event.event_type, "airborne_velocity")
        self.assertEqual(event.raw_frame, frame.hex)
        self.assertIsNone(event.observed_at_unix_ms)
        self.assertEqual(event.fields["subtype"], 1)
        self.assertEqual(event.fields["velocity_east_knots"], -8)
        self.assertEqual(event.fields["velocity_north_knots"], -159)
        self.assertAlmostEqual(event.fields["speed_knots"], 159.20113065, places=8)
        self.assertAlmostEqual(event.fields["track_deg"], 182.88037755, places=8)
        self.assertEqual(event.fields["vertical_rate_fpm"], -832)
        self.assertEqual(event.fields["vertical_rate_source"], "gnss")

    def test_both_subtypes_and_all_direction_combinations(self):
        for subtype, scale in ((1, 1), (2, 4)):
            for west in (0, 1):
                for south in (0, 1):
                    with self.subTest(subtype=subtype, west=west, south=south):
                        frame = make_velocity_frame(subtype=subtype, west=west, south=south)
                        self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                        fields = ADSBDecoder().decode(frame)[0].fields
                        self.assertEqual(fields["subtype"], subtype)
                        self.assertEqual(fields["velocity_east_knots"], (-8 if west else 8) * scale)
                        self.assertEqual(fields["velocity_north_knots"], (-159 if south else 159) * scale)

    def test_missing_components_are_distinct_from_zero_speed(self):
        for east_code, north_code, expected_east, expected_north in (
            (0, 160, None, -159), (9, 0, -8, None),
            (0, 0, None, None), (1, 1, 0, 0),
        ):
            with self.subTest(east_code=east_code, north_code=north_code):
                frame = make_velocity_frame(
                    east_code=east_code, north_code=north_code, west=1, south=1,
                )
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["velocity_east_knots"], expected_east)
                self.assertEqual(fields["velocity_north_knots"], expected_north)

    def test_maximum_component_codes(self):
        for subtype, expected in ((1, 1022), (2, 4088)):
            with self.subTest(subtype=subtype):
                frame = make_velocity_frame(subtype=subtype, east_code=1023, north_code=1023)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["velocity_east_knots"], expected)
                self.assertEqual(fields["velocity_north_knots"], expected)

    def test_reserved_subtypes_are_skipped(self):
        for subtype in (0, 5, 6, 7):
            with self.subTest(subtype=subtype):
                frame = make_velocity_frame(subtype=subtype)
                self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                self.assertEqual(ADSBDecoder().decode(frame), [])

    def test_corrupted_velocity_is_rejected(self):
        frame = ModeSFrame(hex="8D485020994409940838175B284E", sample_index=0)
        self.assertEqual(ADSBDecoder().decode(frame), [])


class GroundSpeedTrackTest(unittest.TestCase):
    def test_cardinal_directions(self):
        for east_code, north_code, west, south, track in (
            (1, 11, 0, 0, 0), (11, 1, 0, 0, 90),
            (1, 11, 0, 1, 180), (11, 1, 1, 0, 270),
        ):
            with self.subTest(track=track):
                frame = make_velocity_frame(
                    east_code=east_code, north_code=north_code, west=west, south=south,
                )
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["speed_knots"], 10)
                self.assertEqual(fields["track_deg"], track)

    def test_diagonal_directions_cover_all_quadrants(self):
        for west, south, track in ((0, 0, 45), (0, 1, 135), (1, 1, 225), (1, 0, 315)):
            with self.subTest(track=track):
                frame = make_velocity_frame(east_code=11, north_code=11, west=west, south=south)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertAlmostEqual(fields["speed_knots"], 14.1421356237, places=8)
                self.assertAlmostEqual(fields["track_deg"], track, places=8)

    def test_subtype_two_scales_speed_but_keeps_track(self):
        # A 3-4-5 triangle becomes 12-16-20 in subtype 2.
        for subtype, speed in ((1, 5), (2, 20)):
            with self.subTest(subtype=subtype):
                frame = make_velocity_frame(subtype=subtype, east_code=4, north_code=5)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["speed_knots"], speed)
                self.assertAlmostEqual(fields["track_deg"], 36.86989765, places=8)

    def test_missing_component_does_not_invent_speed_or_track(self):
        for east_code, north_code in ((0, 160), (9, 0), (0, 0)):
            with self.subTest(east_code=east_code, north_code=north_code):
                frame = make_velocity_frame(east_code=east_code, north_code=north_code)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertIsNone(fields["speed_knots"])
                self.assertIsNone(fields["track_deg"])

    def test_stationary_aircraft_has_zero_speed_and_unknown_track(self):
        for subtype in (1, 2):
            with self.subTest(subtype=subtype):
                frame = make_velocity_frame(subtype=subtype, east_code=1, north_code=1)
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertEqual(fields["speed_knots"], 0)
                self.assertIsNone(fields["track_deg"])

    def test_west_of_north_bearing_wraps_into_positive_range(self):
        frame = make_velocity_frame(east_code=2, north_code=1023, west=1)
        fields = ADSBDecoder().decode(frame)[0].fields
        self.assertGreater(fields["track_deg"], 359)
        self.assertLess(fields["track_deg"], 360)


class VerticalRateTest(unittest.TestCase):
    def test_both_subtypes_share_vertical_scale_sign_and_source(self):
        for subtype in (1, 2):
            for descending, rate in ((0, 832), (1, -832)):
                for barometric, source in ((0, "gnss"), (1, "barometric")):
                    with self.subTest(subtype=subtype, descending=descending, source=source):
                        frame = make_velocity_frame(
                            subtype=subtype, vertical_code=14,
                            descending=descending, barometric=barometric,
                        )
                        self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                        fields = ADSBDecoder().decode(frame)[0].fields
                        self.assertEqual(fields["vertical_rate_fpm"], rate)
                        self.assertEqual(fields["vertical_rate_source"], source)

    def test_missing_rate_is_distinct_from_level_flight(self):
        for code, expected in ((0, None), (1, 0)):
            for descending in (0, 1):
                with self.subTest(code=code, descending=descending):
                    frame = make_velocity_frame(vertical_code=code, descending=descending)
                    fields = ADSBDecoder().decode(frame)[0].fields
                    self.assertEqual(fields["vertical_rate_fpm"], expected)
                    self.assertIsNotNone(fields["speed_knots"])

    def test_vertical_rate_boundaries(self):
        for code, expected in ((2, 64), (511, 32640)):
            for descending in (0, 1):
                with self.subTest(code=code, descending=descending):
                    frame = make_velocity_frame(vertical_code=code, descending=descending)
                    fields = ADSBDecoder().decode(frame)[0].fields
                    self.assertEqual(fields["vertical_rate_fpm"], -expected if descending else expected)

    def test_known_vertical_rate_survives_missing_horizontal_components(self):
        frame = make_velocity_frame(east_code=0, north_code=0, vertical_code=14, descending=1)
        fields = ADSBDecoder().decode(frame)[0].fields
        self.assertEqual(fields["vertical_rate_fpm"], -832)
        self.assertIsNone(fields["velocity_east_knots"])
        self.assertIsNone(fields["velocity_north_knots"])
        self.assertIsNone(fields["speed_knots"])
        self.assertIsNone(fields["track_deg"])


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
                # This test inspects individual frames, without a cached pair.
                decoder.reset()
                frame = ModeSFrame(hex=raw, sample_index=0)
                self.assertEqual(decoder.decode(frame), [DecodedEvent(
                    icao=icao,
                    event_type="airborne_position",
                    fields={
                        "altitude_ft": altitude,
                        "gnss_height_ft": None,
                        "altitude_source": "barometric",
                        "cpr_format": cpr_format,
                        "cpr_lat": lat,
                        "cpr_lon": lon,
                        "latitude": None,
                        "longitude": None,
                    },
                    raw_frame=raw,
                    observed_at_unix_ms=None,
                    metadata={
                        "df": 17, "type_code": 11, "crc_valid": True,
                        "source": "rf", "sample_index": 0, "sample_rate_hz": 2_000_000,
                    },
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
        for alt_code in (0x000, 0x001):
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
                fields = ADSBDecoder().decode(frame)[0].fields
                self.assertIsNone(fields["altitude_ft"])
                self.assertEqual(fields["gnss_height_ft"], 38000)
                self.assertEqual(fields["altitude_source"], "gnss")


if __name__ == "__main__":
    unittest.main()
