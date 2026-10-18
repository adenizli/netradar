"""Regression tests for late fixes and independent position freshness."""
from dataclasses import replace
import unittest

from netradar.backend.state import AircraftStore
from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder


def position(observed, latitude=41.0, sample=100, capture='capture-A', source='barometric'):
    return DecodedEvent(
        icao='ABC123', event_type='airborne_position',
        fields={'latitude': latitude, 'longitude': 29.0, 'altitude_ft': 30000,
                'altitude_source': source}, observed_at_unix_ms=observed,
        metadata={'capture_id': capture, 'sample_index': sample,
                  'sample_rate_hz': 1000, 'source': 'rf'},
    )


class PositionOrderingTest(unittest.TestCase):
    def test_older_fix_cannot_replace_newer_fix_but_event_is_delivered(self):
        store = AircraftStore()
        store.apply(position(2000, latitude=42.0, sample=200))
        old = position(1000, latitude=40.0)
        update = store.apply(old)
        self.assertEqual(update['data']['latitude'], 42.0)
        self.assertEqual(update['data']['position_revision'], 1)
        self.assertEqual(update['data']['position_observed_at_unix_ms'], 2000)
        self.assertEqual(update['event'], old.to_dict())
        self.assertEqual(update['data']['position_event']['fields']['latitude'], 42.0)

    def test_fix_is_compared_with_last_fix_not_newer_callsign(self):
        store = AircraftStore()
        store.apply(position(1000))
        store.apply(DecodedEvent(icao='ABC123', event_type='identification',
                                 fields={'callsign': 'TEST123'}, observed_at_unix_ms=3000))
        data = store.apply(position(2000, latitude=42.0, sample=200))['data']
        self.assertEqual(data['latitude'], 42.0)
        self.assertEqual(data['last_seen_unix_ms'], 3000)
        self.assertEqual(data['position_observed_at_unix_ms'], 2000)

    def test_sample_order_works_without_absolute_time_in_same_capture(self):
        store = AircraftStore()
        store.apply(position(None, latitude=42.0, sample=200))
        data = store.apply(position(None, latitude=40.0, sample=100))['data']
        self.assertEqual(data['latitude'], 42.0)
        self.assertIsNone(data['position_observed_at_unix_ms'])
        self.assertEqual(store.apply(position(None, latitude=43.0, sample=300))['data']['latitude'], 43.0)

    def test_duplicate_and_equal_time_do_not_refresh_position(self):
        store = AircraftStore()
        first = store.apply(position(1000))['data']
        duplicate = store.apply(position(1000))['data']
        self.assertEqual(duplicate['position_revision'], first['position_revision'])
        self.assertEqual(duplicate['position_received_at_unix_ms'], first['position_received_at_unix_ms'])

    def test_different_capture_samples_are_not_comparable(self):
        store = AircraftStore()
        store.apply(position(None, latitude=42.0, sample=200))
        data = store.apply(position(None, latitude=40.0, sample=99999, capture='other'))['data']
        self.assertEqual(data['latitude'], 42.0)

    def test_unknown_time_cannot_replace_a_dated_position(self):
        store = AircraftStore()
        store.apply(position(2000, latitude=42.0))
        event = DecodedEvent(icao='ABC123', event_type='airborne_position',
                             fields={'latitude': 40.0, 'longitude': 28.0})
        self.assertEqual(store.apply(event)['data']['latitude'], 42.0)

    def test_gnss_and_baro_fixes_share_coordinate_ordering(self):
        store = AircraftStore()
        store.apply(position(2000, latitude=42.0, source='gnss'))
        self.assertEqual(store.apply(position(1000, latitude=40.0))['data']['latitude'], 42.0)

    def test_unresolved_cpr_does_not_refresh_last_resolved_fix(self):
        store = AircraftStore()
        store.apply(position(1000))
        unresolved = replace(position(2000, sample=200), fields={'latitude': None, 'longitude': None, 'altitude_ft': 31000})
        data = store.apply(unresolved)['data']
        self.assertEqual(data['position_revision'], 1)
        self.assertEqual(data['position_observed_at_unix_ms'], 1000)
        self.assertEqual(data['altitude_ft'], 31000)


class PositionAgeTest(unittest.TestCase):
    def setUp(self):
        self.mono = 10.0
        self.wall = 1000.0
        self.store = AircraftStore(clock=lambda: self.mono, wall_clock=lambda: self.wall)

    def test_identification_does_not_rejuvenate_position_and_snapshot_ages(self):
        first = self.store.apply(position(999000))['data']
        self.assertEqual(first['position_age_ms'], 1000)
        self.mono += 40
        data = self.store.apply(DecodedEvent(icao='ABC123', event_type='identification', fields={'callsign': 'TEST123'}, observed_at_unix_ms=1040000))['data']
        self.assertEqual(data['position_age_ms'], 41000)
        self.assertEqual(data['position_received_at_unix_ms'], first['position_received_at_unix_ms'])
        self.mono += 20
        self.assertEqual(self.store.snapshot()[0]['position_age_ms'], 61000)

    def test_repeated_stationary_but_newer_fix_refreshes_position(self):
        self.store.apply(position(999000))
        self.mono += 40; self.wall += 40
        data = self.store.apply(position(1040000, sample=200))['data']
        self.assertEqual(data['position_revision'], 2)
        self.assertEqual(data['position_age_ms'], 0)

    def test_unknown_absolute_time_uses_receipt_without_inventing_observation(self):
        self.store.apply(position(None))
        self.mono += 31
        data = self.store.snapshot()[0]
        self.assertEqual(data['position_age_ms'], 31000)
        self.assertIsNone(data['last_seen_unix_ms'])
        self.assertIsNone(data['position_observed_at_unix_ms'])

    def test_capture_progress_ages_fix_even_when_replay_is_fast(self):
        self.store.apply(position(None, sample=100))
        identity = DecodedEvent(icao='ABC123', event_type='identification', fields={'callsign': 'TEST123'}, metadata={'capture_id': 'capture-A', 'sample_index': 40100, 'sample_rate_hz': 1000, 'source': 'rf'})
        self.assertEqual(self.store.apply(identity)['data']['position_age_ms'], 40000)


class DecoderClockTest(unittest.TestCase):
    def test_decoder_assigns_capture_identity_and_reset_changes_it(self):
        decoder = ADSBDecoder()
        frame = ModeSFrame(hex='8D4840D6202CC371C32CE0576098', sample_index=100)
        first = decoder.decode(frame)[0]
        second = decoder.decode(replace(frame, sample_index=200))[0]
        self.assertEqual(first.metadata['capture_id'], second.metadata['capture_id'])
        decoder.reset()
        self.assertNotEqual(first.metadata['capture_id'], decoder.decode(frame)[0].metadata['capture_id'])

    def test_frame_observation_time_survives_decoding(self):
        frame = ModeSFrame(hex='8D4840D6202CC371C32CE0576098', sample_index=100, observed_at_unix_ms=1000)
        self.assertEqual(ADSBDecoder().decode(frame)[0].observed_at_unix_ms, 1000)

class ClockEdgeCaseTest(unittest.TestCase):
    def test_equal_millisecond_new_sample_is_newer_but_older_sample_is_not(self):
        store = AircraftStore()
        store.apply(position(1000, sample=100))
        self.assertEqual(store.apply(position(1000, latitude=42.0, sample=200))['data']['latitude'], 42.0)
        self.assertEqual(store.apply(position(1000, latitude=40.0, sample=150))['data']['latitude'], 42.0)

    def test_late_cpr_counterpart_does_not_rejuvenate_same_resolved_fix(self):
        clock = [10.0]
        store = AircraftStore(clock=lambda:clock[0], wall_clock=lambda:1.0)
        decoder = ADSBDecoder()
        even = ModeSFrame(hex='8D40621D58C382D690C8AC2863A7', sample_index=100)
        odd = ModeSFrame(hex='8D40621D58C386435CC412692AD6', sample_index=200)
        store.apply(decoder.decode(even)[0])
        first = store.apply(decoder.decode(odd)[0])['data']
        clock[0] += 40
        repeated = store.apply(decoder.decode(even)[0])['data']
        self.assertEqual(first['position_revision'], repeated['position_revision'])
        self.assertEqual(repeated['position_age_ms'], 40000)

    def test_all_unknown_clocks_cannot_replace_the_first_coordinate_pair(self):
        store = AircraftStore()
        event = DecodedEvent(icao='ABC123', event_type='airborne_position', fields={'latitude':41.0,'longitude':29.0})
        store.apply(event)
        self.assertEqual(store.apply(replace(event,fields={'latitude':40.0,'longitude':28.0}))['data']['latitude'],41.0)

    def test_stale_altitude_and_velocity_do_not_overwrite_dated_measurements(self):
        store = AircraftStore()
        store.apply(position(2000))
        older = replace(position(1000), fields={**position(1000).fields,'altitude_ft':12000})
        self.assertEqual(store.apply(older)['data']['altitude_ft'],30000)
        event = DecodedEvent(icao='ABC123', event_type='airborne_velocity', fields={'speed_knots':400,'track_deg':0},observed_at_unix_ms=2000)
        store.apply(event)
        self.assertEqual(store.apply(replace(event, observed_at_unix_ms=1000,fields={'speed_knots':100,'track_deg':180}))['data']['speed_knots'],400)

    def test_invalid_observation_times_are_rejected(self):
        for value in (-1,True,1.5,'1000',float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                position(value)

    def test_cpr_time_refers_to_newer_frame_even_when_counterpart_arrives_late(self):
        decoder = ADSBDecoder()
        odd=ModeSFrame(hex='8D40621D58C386435CC412692AD6', sample_index=200,observed_at_unix_ms=2000)
        even=ModeSFrame(hex='8D40621D58C382D690C8AC2863A7', sample_index=100,observed_at_unix_ms=1000)
        decoder.decode(odd)
        result=decoder.decode(even)[0]
        self.assertEqual(result.observed_at_unix_ms,2000)
        self.assertEqual(result.raw_frame,odd.hex)
