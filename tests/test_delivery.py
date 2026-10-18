from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

from netradar.backend.state import AircraftStore
from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder
from netradar.pipeline import DecoderPipeline
from tests.test_decoder import make_position_frame, make_velocity_frame
from tests.test_extended_decoder import make_airspeed_frame


ROOT = Path(__file__).resolve().parents[1]


def supported_frames() -> list[ModeSFrame]:
    metadata = json.loads((ROOT / "fixtures" / "synthetic_adsb_2msps.json").read_text())
    frames = [
        ModeSFrame(hex=item["hex"], sample_index=item["sample_index"], source="synthetic")
        for item in metadata["expected_frames"]
    ]
    frames.extend((
        make_position_frame(0x361),
        make_position_frame(0xC90, type_code=20, cpr_lat=93000, cpr_lon=51372),
        make_position_frame(0xC90, type_code=22, odd=1, cpr_lat=74158, cpr_lon=50194),
        make_velocity_frame(subtype=2, vertical_code=14),
        make_airspeed_frame(subtype=3), make_airspeed_frame(subtype=4),
    ))
    return [replace(frame, sample_index=i * 10000) for i, frame in enumerate(frames)]


class EventWireTest(unittest.TestCase):
    def test_every_decoder_field_survives_json_roundtrip(self):
        decoder = ADSBDecoder()
        for frame in supported_frames():
            with self.subTest(frame=frame.hex):
                for event in decoder.decode(frame):
                    recovered = DecodedEvent.from_json(event.to_json())
                    self.assertEqual(recovered, event)
                    self.assertEqual(recovered.fields, event.fields)
                    self.assertEqual(event.metadata["sample_index"], frame.sample_index)
                    self.assertEqual(event.metadata["source"], "synthetic")
                    self.assertEqual(event.metadata["sample_rate_hz"], 2_000_000)

    def test_future_payload_and_metadata_fields_are_preserved(self):
        event = DecodedEvent(
            icao="40621D", event_type="airborne_position",
            fields={"future_metric": {"zero": 0, "missing": None, "flag": False, "label": "uçak"}},
            metadata={"future_metadata": [1, "test", None]},
        )
        self.assertEqual(DecodedEvent.from_json(event.to_json()), event)
        payload = event.to_dict()
        payload["fields"]["future_metric"]["zero"] = 99
        payload["metadata"]["future_metadata"].append(4)
        self.assertEqual(event.fields["future_metric"]["zero"], 0)
        self.assertEqual(event.metadata["future_metadata"], [1, "test", None])

    def test_legacy_v1_event_without_metadata_is_accepted(self):
        payload = {
            "icao": "40621D", "event_type": "identification",
            "fields": {"callsign": "TEST123"}, "raw_frame": None,
            "observed_at_unix_ms": None,
        }
        event = DecodedEvent.from_dict(payload)
        self.assertEqual(event.fields["callsign"], "TEST123")
        self.assertEqual(event.metadata, {})
        self.assertEqual(event.to_dict()["schema_version"], "1.1")

    def test_unknown_schema_and_nonfinite_numbers_are_rejected(self):
        event = DecodedEvent(icao="40621D", event_type="identification")
        with self.assertRaises(ValueError):
            DecodedEvent.from_dict({**event.to_dict(), "schema_version": "2.0"})
        with self.assertRaises(ValueError):
            DecodedEvent.from_json('[]')
        bad = replace(event, fields={"speed_knots": float("nan")})
        with self.assertRaises(ValueError):
            bad.to_json()

    def test_coordinates_and_metadata_refer_to_same_newer_message(self):
        decoder = ADSBDecoder(sample_rate_hz=1_000_000)
        odd = replace(make_position_frame(
            0xC90, type_code=22, odd=1, cpr_lat=74158, cpr_lon=50194,
        ), sample_index=20000)
        even = replace(make_position_frame(
            0xC38, type_code=20, cpr_lat=93000, cpr_lon=51372,
        ), sample_index=10000)
        decoder.decode(odd)
        event = decoder.decode(even)[0]
        self.assertEqual(event.raw_frame, odd.hex)
        self.assertEqual(event.metadata["sample_index"], 20000)
        self.assertEqual(event.metadata["type_code"], 22)
        self.assertEqual(event.metadata["sample_rate_hz"], 1_000_000)
        self.assertEqual(event.fields["gnss_height_ft"], 39000)


class BackendDeliveryTest(unittest.TestCase):
    def test_every_event_is_delivered_and_published_without_field_loss(self):
        published = []
        pipeline = DecoderPipeline(publish=published.append)
        expected_decoder = ADSBDecoder()
        expected_count = 0
        for frame in supported_frames():
            expected_events = expected_decoder.decode(frame)
            updates = pipeline.handle_frame(frame)
            self.assertEqual(len(updates), len(expected_events))
            for event, update in zip(expected_events, updates):
                expected_count += 1
                self.assertEqual(update["type"], "aircraft_update")
                self.assertEqual(update["schema_version"], "1.1")
                wire_update = json.loads(json.dumps(update, allow_nan=False))
                self.assertEqual(wire_update["event"], event.to_dict())
                self.assertIn(event.to_dict(), list(wire_update["data"]["latest_events"].values()))
                self.assertIsNone(wire_update["data"]["last_seen"])
        self.assertEqual(len(published), expected_count)
        self.assertEqual(expected_count, len(supported_frames()))

    def test_other_process_can_send_all_fields_as_event_json(self):
        event = DecodedEvent(
            icao="40621D", event_type="identification", fields={
                "callsign": "TEST123", "future_metric": {"nested": [0, None, False]},
            }, metadata={"source": "replay", "future_metadata": "preserved"},
        )
        store = AircraftStore()
        update = store.apply_json(event.to_json())
        self.assertEqual(update["event"], event.to_dict())
        saved = store.snapshot()[0]["latest_events"]["identification"]
        self.assertEqual(saved, event.to_dict())

    def test_ground_and_air_measurements_are_retained_separately(self):
        decoder = ADSBDecoder()
        store = AircraftStore()
        ground = decoder.decode(make_velocity_frame(west=1, south=1))[0]
        air = decoder.decode(make_airspeed_frame())[0]
        store.apply(ground)
        data = store.apply(air)["data"]
        self.assertAlmostEqual(data["speed_knots"], 159.20113065, places=8)
        self.assertAlmostEqual(data["track_deg"], 182.88037755, places=8)
        self.assertEqual(data["airspeed_knots"], 375)
        self.assertEqual(data["airspeed_type"], "tas")
        self.assertEqual(data["heading_deg"], 243.984375)
        self.assertEqual(data["heading_reference"], "magnetic")
        self.assertIn("airborne_velocity:ground", data["latest_events"])
        self.assertIn("airborne_velocity:air", data["latest_events"])
        unavailable_air = decoder.decode(make_airspeed_frame(
            tas=0, airspeed_code=0, heading_valid=0, vertical_code=0,
        ))[0]
        data = store.apply(unavailable_air)["data"]
        self.assertEqual(data["airspeed_knots"], 375)
        self.assertEqual(data["airspeed_type"], "tas")
        self.assertEqual(data["vertical_rate_fpm"], -2304)

    def test_barometric_and_geometric_heights_do_not_erase_each_other(self):
        decoder = ADSBDecoder()
        store = AircraftStore()
        store.apply(decoder.decode(make_position_frame(0xC38))[0])
        data = store.apply(decoder.decode(make_position_frame(0xC90, type_code=20))[0])["data"]
        self.assertEqual(data["altitude_ft"], 38000)
        self.assertEqual(data["gnss_height_ft"], 39000)
        self.assertIn("airborne_position:barometric", data["latest_events"])
        self.assertIn("airborne_position:gnss", data["latest_events"])
        self.assertIsNone(data["latitude"])

    def test_real_zeros_replace_values_and_zero_speed_clears_track(self):
        decoder = ADSBDecoder()
        store = AircraftStore()
        store.apply(decoder.decode(make_velocity_frame())[0])
        stationary = decoder.decode(make_velocity_frame(east_code=1, north_code=1))[0]
        data = store.apply(stationary)["data"]
        self.assertEqual(data["speed_knots"], 0)
        self.assertIsNone(data["track_deg"])
        data = store.apply(DecodedEvent(
            icao="485020", event_type="airborne_position",
            fields={"latitude": 0, "longitude": 0, "altitude_ft": 0},
        ))["data"]
        self.assertEqual(data["latitude"], 0)
        self.assertEqual(data["longitude"], 0)
        self.assertEqual(data["altitude_ft"], 0)

    def test_returns_and_inputs_do_not_mutate_retained_state(self):
        store = AircraftStore()
        event = DecodedEvent(
            icao="40621D", event_type="identification",
            fields={"callsign": "TEST123", "extra": {"value": 1}},
        )
        update = store.apply(event)
        event.fields["extra"]["value"] = 2
        update["data"]["callsign"] = "CHANGED"
        update["event"]["fields"]["extra"]["value"] = 3
        snapshot = store.snapshot()
        self.assertEqual(snapshot[0]["callsign"], "TEST123")
        self.assertEqual(snapshot[0]["latest_events"]["identification"]["fields"]["extra"]["value"], 1)
        snapshot[0]["callsign"] = "ALSO CHANGED"
        self.assertEqual(store.snapshot()[0]["callsign"], "TEST123")

    def test_only_measured_absolute_time_sets_last_seen(self):
        store = AircraftStore()
        event = DecodedEvent(icao="40621D", event_type="identification")
        self.assertIsNone(store.apply(event)["data"]["last_seen"])
        data = store.apply(replace(event, observed_at_unix_ms=1000))["data"]
        self.assertEqual(data["last_seen"], "1970-01-01T00:00:01.000Z")
        data = store.apply(replace(event, observed_at_unix_ms=500))["data"]
        self.assertEqual(data["last_seen_unix_ms"], 1000)

    def test_corrupt_and_unsupported_frames_are_not_published(self):
        published = []
        pipeline = DecoderPipeline(publish=published.append)
        for raw in ("8D40621D58C382D690C8AC2863A6", "0D4840D6202CC371C32CE0576098"):
            self.assertEqual(pipeline.handle_frame(ModeSFrame(hex=raw, sample_index=0)), [])
        self.assertEqual(published, [])
        self.assertEqual(pipeline.store.snapshot(), [])


class JSONLCommandTest(unittest.TestCase):
    def test_fixture_command_delivers_all_seven_events_as_json(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "netradar.pipeline", "--fixture", "fixtures/synthetic_adsb_2msps.json"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        updates = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(updates), 7)
        for update in updates:
            self.assertEqual(update["event"]["metadata"]["source"], "synthetic")
            self.assertIn("raw_frame", update["event"])
        velocity = next(u for u in updates if u["event"]["event_type"] == "airborne_velocity")
        self.assertEqual(velocity["event"]["fields"]["vertical_rate_fpm"], -832)
        self.assertAlmostEqual(updates[1]["data"]["latitude"], 52.2657801741, places=8)

    def test_another_process_can_pipe_a_frame_on_stdin(self):
        frame = make_airspeed_frame(subtype=4)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "netradar.pipeline"], cwd=ROOT,
            input=json.dumps(asdict(frame)) + "\n", text=True, capture_output=True, check=True,
        )
        update = json.loads(result.stdout)
        self.assertEqual(update["event"]["fields"]["airspeed_knots"], 1500)
        self.assertIsNone(update["data"]["speed_knots"])
        self.assertEqual(update["data"]["airspeed_knots"], 1500)

    def test_mismatched_fixture_sample_rate_is_rejected(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "netradar.pipeline", "--fixture",
             "fixtures/synthetic_adsb_2msps.json", "--sample-rate-hz", "1000000"],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
