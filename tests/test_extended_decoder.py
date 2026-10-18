"""Additional DF17 encodings; generated frames are synthetic, not RF captures."""
from dataclasses import replace
import unittest

from netradar.contracts import ModeSFrame
from netradar.decoder.decode import ADSBDecoder, crc24, decode_altitude, decode_cpr_pair
from tests.test_decoder import make_position_frame, make_velocity_frame


def make_airspeed_frame(
    *, subtype: int = 3, heading_code: int = 694, heading_valid: int = 1,
    airspeed_code: int = 376, tas: int = 1, vertical_code: int = 37,
    descending: int = 1, barometric: int = 1,
) -> ModeSFrame:
    # TC19 uses these same bit positions with different meanings in subtype 3/4.
    return make_velocity_frame(
        subtype=subtype, east_code=heading_code, west=heading_valid,
        north_code=airspeed_code, south=tas, vertical_code=vertical_code,
        descending=descending, barometric=barometric,
    )


class GillhamAltitudeTest(unittest.TestCase):
    def test_q_zero_reference_vectors(self):
        # Verified against FlightAware dump1090 decodeAC12Field/modeAToModeC.
        for code, expected in (
            (0x080, -1200), (0x200, -1000), (0x20A, 0), (0xA0A, 100),
            (0x208, 500), (0x288, 600), (0x643, 38000), (0x361, 51000),
            (0x084, 126700), (0xC28, 28300),
        ):
            with self.subTest(code=code):
                self.assertEqual(decode_altitude(code), expected)
                event = ADSBDecoder().decode(make_position_frame(code))[0]
                self.assertEqual(event.fields["altitude_ft"], expected)
                self.assertEqual(event.fields["altitude_source"], "barometric")

    def test_invalid_gillham_hundred_counts_remain_unknown(self):
        for code in (0x000, 0x001, 0xA80, 0x880):
            with self.subTest(code=code):
                self.assertIsNone(decode_altitude(code))

    def test_q_zero_pair_resolves_coordinates(self):
        decoder = ADSBDecoder()
        even = make_position_frame(0x643, cpr_lat=93000, cpr_lon=51372)
        odd = replace(make_position_frame(
            0x643, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=1)
        decoder.decode(even)
        fields = decoder.decode(odd)[0].fields
        self.assertEqual(fields["altitude_ft"], 38000)
        self.assertAlmostEqual(fields["latitude"], 52.2657801741, places=8)


class GNSSPositionTest(unittest.TestCase):
    def test_all_gnss_types_and_both_altitude_encodings(self):
        # AC12 encodes feet for geometric height too (FlightAware/VIAVI).
        for tc in (20, 21, 22):
            for code, height in ((0xC38, 38000), (0x643, 38000), (0x361, 51000)):
                with self.subTest(tc=tc, code=code):
                    frame = make_position_frame(code, type_code=tc)
                    self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                    fields = ADSBDecoder().decode(frame)[0].fields
                    self.assertIsNone(fields["altitude_ft"])
                    self.assertEqual(fields["gnss_height_ft"], height)
                    self.assertEqual(fields["altitude_source"], "gnss")

    def test_missing_and_zero_geometric_height_are_distinct(self):
        for code, expected in ((0x000, None), (0x058, 0), (0x010, -1000)):
            with self.subTest(code=code):
                fields = ADSBDecoder().decode(make_position_frame(code, type_code=20))[0].fields
                self.assertEqual(fields["gnss_height_ft"], expected)
                self.assertIsNone(fields["altitude_ft"])

    def test_gnss_pair_resolves_coordinates_with_independent_type_codes(self):
        decoder = ADSBDecoder()
        even = make_position_frame(0xC38, type_code=20, cpr_lat=93000, cpr_lon=51372)
        odd = replace(make_position_frame(
            0xC90, type_code=22, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=1)
        decoder.decode(even)
        event = decoder.decode(odd)[0]
        self.assertEqual(event.event_type, "airborne_position")
        self.assertEqual(event.fields["gnss_height_ft"], 39000)
        self.assertIsNone(event.fields["altitude_ft"])
        self.assertAlmostEqual(event.fields["latitude"], 52.2657801741, places=8)
        self.assertAlmostEqual(event.fields["longitude"], 3.9389125279, places=8)
        self.assertEqual(decoder.get_cpr_pair("40621D", altitude_source="gnss"), (even, odd))
        self.assertIsNone(decoder.get_cpr_pair("40621D"))

    def test_barometric_and_gnss_pairs_are_not_mixed(self):
        decoder = ADSBDecoder()
        baro_even = make_position_frame(0xC38, cpr_lat=93000, cpr_lon=51372)
        gnss_odd = replace(make_position_frame(
            0xC90, type_code=21, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=1)
        decoder.decode(baro_even)
        fields = decoder.decode(gnss_odd)[0].fields
        self.assertIsNone(fields["latitude"])
        self.assertIsNone(decode_cpr_pair(baro_even, gnss_odd))
        gnss_even = replace(make_position_frame(
            0xC90, type_code=20, cpr_lat=93000, cpr_lon=51372,
        ), sample_index=2)
        fields = decoder.decode(gnss_even)[0].fields
        self.assertAlmostEqual(fields["latitude"], 52.2572021484, places=8)
        self.assertEqual(fields["gnss_height_ft"], 39000)
        self.assertIsNone(decoder.get_cpr_pair("40621D"))
        baro_odd = replace(make_position_frame(
            0xC38, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=3)
        fields = decoder.decode(baro_odd)[0].fields
        self.assertEqual(fields["altitude_ft"], 38000)
        self.assertIsNone(fields["gnss_height_ft"])
        self.assertAlmostEqual(fields["latitude"], 52.2657801741, places=8)

    def test_geometric_pairs_expire_and_reset(self):
        decoder = ADSBDecoder()
        even = make_position_frame(0xC38, type_code=20)
        decoder.decode(even)
        odd = replace(make_position_frame(0xC38, type_code=21, odd=1), sample_index=20_000_001)
        self.assertIsNone(decoder.decode(odd)[0].fields["latitude"])
        self.assertIsNone(decoder.get_cpr_pair("40621D", altitude_source="gnss"))
        decoder.decode(replace(even, sample_index=20_000_002))
        self.assertIsNotNone(decoder.get_cpr_pair("40621D", altitude_source="gnss"))
        decoder.reset()
        self.assertIsNone(decoder.get_cpr_pair("40621D", altitude_source="gnss"))

    def test_corrupt_gnss_message_is_rejected(self):
        frame = make_position_frame(0xC38, type_code=20)
        frame = replace(frame, hex=f"{int(frame.hex, 16) ^ 1:028X}")
        decoder = ADSBDecoder()
        self.assertEqual(decoder.decode(frame), [])
        self.assertIsNone(decoder.get_cpr_pair("40621D", altitude_source="gnss"))


class AirspeedHeadingTest(unittest.TestCase):
    def test_published_subtype_three_reference(self):
        # The 1090 MHz Riddle / pyModeS test_adsb_velocity example B.
        frame = ModeSFrame(hex="8DA05F219B06B6AF189400CBC33F", sample_index=0)
        event = ADSBDecoder().decode(frame)[0]
        self.assertEqual(event.event_type, "airborne_velocity")
        self.assertEqual(event.icao, "A05F21")
        self.assertEqual(event.raw_frame, frame.hex)
        self.assertEqual(event.fields["subtype"], 3)
        self.assertEqual(event.fields["airspeed_knots"], 375)
        self.assertEqual(event.fields["airspeed_type"], "tas")
        self.assertAlmostEqual(event.fields["heading_deg"], 243.984375)
        self.assertEqual(event.fields["heading_reference"], "magnetic")
        self.assertEqual(event.fields["vertical_rate_fpm"], -2304)
        self.assertEqual(event.fields["vertical_rate_source"], "barometric")
        self.assertIsNone(event.fields["speed_knots"])
        self.assertIsNone(event.fields["track_deg"])
        self.assertIsNone(event.fields["velocity_east_knots"])
        self.assertIsNone(event.fields["velocity_north_knots"])

    def test_both_subtypes_and_both_airspeed_sources(self):
        for subtype, speed in ((3, 375), (4, 1500)):
            for tas, kind in ((0, "ias"), (1, "tas")):
                with self.subTest(subtype=subtype, kind=kind):
                    frame = make_airspeed_frame(subtype=subtype, tas=tas)
                    self.assertEqual(crc24(bytes.fromhex(frame.hex)), 0)
                    fields = ADSBDecoder().decode(frame)[0].fields
                    self.assertEqual(fields["airspeed_knots"], speed)
                    self.assertEqual(fields["airspeed_type"], kind)
                    self.assertEqual(fields["heading_deg"], 243.984375)
                    self.assertEqual(fields["vertical_rate_fpm"], -2304)

    def test_heading_validity_is_independent_of_airspeed(self):
        frame = make_airspeed_frame(heading_valid=0)
        fields = ADSBDecoder().decode(frame)[0].fields
        self.assertIsNone(fields["heading_deg"])
        self.assertEqual(fields["airspeed_knots"], 375)
        frame = make_airspeed_frame(airspeed_code=0)
        fields = ADSBDecoder().decode(frame)[0].fields
        self.assertIsNone(fields["airspeed_knots"])
        self.assertEqual(fields["heading_deg"], 243.984375)

    def test_heading_cardinal_directions_and_maximum(self):
        for code, heading in ((0, 0), (256, 90), (512, 180), (768, 270), (1023, 359.6484375)):
            with self.subTest(code=code):
                fields = ADSBDecoder().decode(make_airspeed_frame(heading_code=code))[0].fields
                self.assertEqual(fields["heading_deg"], heading)

    def test_airspeed_missing_zero_and_maximum(self):
        for subtype, maximum in ((3, 1022), (4, 4088)):
            for code, expected in ((0, None), (1, 0), (1023, maximum)):
                with self.subTest(subtype=subtype, code=code):
                    fields = ADSBDecoder().decode(make_airspeed_frame(
                        subtype=subtype, airspeed_code=code,
                    ))[0].fields
                    self.assertEqual(fields["airspeed_knots"], expected)
                    self.assertIsNone(fields["speed_knots"])
                    self.assertIsNone(fields["track_deg"])

    def test_vertical_rate_remains_available_without_heading_or_airspeed(self):
        fields = ADSBDecoder().decode(make_airspeed_frame(
            heading_valid=0, airspeed_code=0,
        ))[0].fields
        self.assertEqual(fields["vertical_rate_fpm"], -2304)
        self.assertIsNone(fields["heading_deg"])
        self.assertIsNone(fields["airspeed_knots"])

    def test_ground_velocity_does_not_invent_airspeed_or_heading(self):
        fields = ADSBDecoder().decode(make_velocity_frame())[0].fields
        self.assertIsNotNone(fields["speed_knots"])
        self.assertIsNone(fields["airspeed_knots"])
        self.assertIsNone(fields["airspeed_type"])
        self.assertIsNone(fields["heading_deg"])
        self.assertIsNone(fields["heading_reference"])

    def test_corrupt_airspeed_message_is_rejected(self):
        frame = ModeSFrame(hex="8DA05F219B06B6AF189400CBC33E", sample_index=0)
        self.assertEqual(ADSBDecoder().decode(frame), [])


if __name__ == "__main__":
    unittest.main()
