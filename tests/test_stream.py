import asyncio
from dataclasses import replace
import unittest

from netradar.backend.state import AircraftStore
from netradar.backend.stream import TrafficService
from netradar.contracts import DecodedEvent, ModeSFrame


def position(timestamp=1000, latitude=41.1):
    return DecodedEvent(
        icao="ABC123", event_type="airborne_position", observed_at_unix_ms=timestamp,
        fields={"latitude": latitude, "longitude": 29.1, "altitude_source": "barometric"},
    )


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_fanout_and_snapshot_keep_full_event(self):
        service = TrafficService()
        first, second = service.subscribe(), service.subscribe()
        event = replace(position(), fields={**position().fields, "future": {"zero": 0, "flag": False}})
        update = await service.ingest(event)
        self.assertEqual(await first.queue.get(), update)
        self.assertEqual(await second.queue.get(), update)
        self.assertEqual(update["event"], event.to_dict())
        self.assertEqual((await service.snapshot())[0]["latitude"], 41.1)
        service.unsubscribe(first)
        service.unsubscribe(second)
        self.assertEqual(service.subscriber_count, 0)
        await service.close()

    async def test_slow_client_reconnects_without_blocking_fast_client(self):
        service = TrafficService(queue_size=1)
        slow, fast = service.subscribe(), service.subscribe()
        await service.ingest(position())
        await fast.queue.get()
        latest = await service.ingest(position(2000, 41.2))
        self.assertEqual(await fast.queue.get(), latest)
        self.assertEqual(slow.close_code, 1013)
        self.assertIsNone(await slow.queue.get())
        await service.ingest(position(3000, 41.3))
        self.assertEqual((await service.snapshot())[0]["latitude"], 41.3)
        # Closed subscriber stays stopped until its socket handler unregisters it.
        self.assertTrue(slow.queue.empty())
        await service.close()

    async def test_shutdown_wakes_idle_subscribers(self):
        service = TrafficService()
        subscriber = service.subscribe()
        await service.close()
        self.assertIsNone(await subscriber.queue.get())
        self.assertEqual(subscriber.close_code, 1001)
        with self.assertRaises(RuntimeError):
            await service.ingest(position())

    async def test_decoder_pipeline_publishes_and_rejects_bad_crc(self):
        service = TrafficService()
        subscriber = service.subscribe()
        updates = await service.handle_frame(ModeSFrame(
            hex="8D4840D6202CC371C32CE0576098", sample_index=0, source="synthetic",
        ))
        self.assertEqual(len(updates), 1)
        self.assertEqual((await subscriber.queue.get())["data"]["callsign"], "KLM1023")
        self.assertEqual(await service.handle_frame(ModeSFrame(
            hex="8D40621D58C382D690C8AC2863A6", sample_index=100, source="synthetic",
        )), [])
        self.assertTrue(subscriber.queue.empty())
        await service.close()

    async def test_age_survives_identity_and_snapshot_reconnect(self):
        now = [0.0]
        service = TrafficService(store=AircraftStore(clock=lambda: now[0], wall_clock=lambda: 1.0))
        await service.ingest(position())
        now[0] = 45.0
        identity = DecodedEvent(icao="ABC123", event_type="identification", fields={"callsign": "TEST001"})
        update = await service.ingest(identity)
        self.assertEqual(update["data"]["position_age_ms"], 45000)
        await service.ingest(position(500, 40.0))
        snapshot = (await service.snapshot())[0]
        self.assertEqual(snapshot["latitude"], 41.1)
        self.assertEqual(snapshot["position_revision"], 1)
        self.assertEqual(snapshot["position_age_ms"], 45000)
        await service.close()

    async def test_other_thread_cannot_mutate_service(self):
        service = TrafficService()
        await service.snapshot()
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(lambda: asyncio.run(service.ingest(position())))
        self.assertEqual(await service.snapshot(), [])
        await service.close()


if __name__ == "__main__":
    unittest.main()
