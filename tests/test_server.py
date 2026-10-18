from dataclasses import replace
import asyncio
from importlib.util import find_spec
import json
from unittest.mock import patch
import unittest

from netradar.backend.state import AircraftStore
from netradar.backend.stream import TrafficService
from netradar.contracts import DecodedEvent
from tests.test_delivery import supported_frames
from tests.test_stream import position


HAS_BACKEND = find_spec("fastapi") is not None and find_spec("httpx") is not None
if HAS_BACKEND:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from netradar.backend.server import create_app


@unittest.skipUnless(HAS_BACKEND, "Install requirements-dev.txt for HTTP/WebSocket integration tests")
class ServerTest(unittest.TestCase):
    @unittest.skipUnless(find_spec("numpy") is not None, "Install requirements-dsp.txt for full IQ integration")
    def test_iq_detection_reaches_decoder_store_and_websocket(self):
        from netradar.acquisition.receiver import replay_cu8
        from netradar.dsp.detector import detect_frames
        from tests.test_dsp import FIXTURE
        service = TrafficService()
        received = []
        with TestClient(create_app(service=service)) as client:
            with client.websocket_connect("/ws/aircraft") as websocket:
                for frame in detect_frames(replay_cu8(FIXTURE, chunk_samples=241), source="synthetic"):
                    updates = client.portal.call(service.handle_frame, frame)
                    for update in updates:
                        self.assertEqual(websocket.receive_json(), update)
                        received.append(update)
                snapshot = client.get("/api/aircraft").json()
                self.assertEqual(len(received), 7)
                self.assertEqual(len(snapshot), 5)
                self.assertEqual(len([a for a in snapshot if a["latitude"] is not None]), 2)
                self.assertTrue(all(u["event"]["metadata"]["source"] == "synthetic" for u in received))

    def test_empty_snapshot_health_and_no_cache(self):
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/healthz").json()["status"], "ok")
            snapshot = client.get("/api/aircraft")
            self.assertEqual(snapshot.json(), [])
            self.assertEqual(snapshot.headers["cache-control"], "no-store")

    def test_two_sockets_snapshot_race_reconnect_and_full_fields(self):
        service = TrafficService()
        event = replace(position(), fields={**position().fields, "future_metric": {"zero": 0, "flag": False}},
                        metadata={"source": "test", "future": [1, None, "uçak"]})
        with TestClient(create_app(service=service)) as client:
            with client.websocket_connect("/ws/aircraft") as first:
                with client.websocket_connect("/ws/aircraft") as second:
                    result = client.post("/api/events", json=event.to_dict())
                    self.assertEqual(result.status_code, 200)
                    update = result.json()
                    self.assertEqual(update["event"], event.to_dict())
                    self.assertEqual(update["stream_sequence"], 1)
                    self.assertEqual(update["data"]["stream_id"], update["stream_id"])
                    # Publish after WS handshake, before HTTP snapshot installation.
                    snapshot = client.get("/api/aircraft").json()
                    self.assertEqual(snapshot[0]["latitude"], 41.1)
                    self.assertEqual(snapshot[0]["stream_sequence"], update["stream_sequence"])
                    self.assertEqual(first.receive_json(), update)
                    self.assertEqual(second.receive_json(), update)
            self.assertEqual(service.subscriber_count, 0)
            with client.websocket_connect("/ws/aircraft") as reconnect:
                self.assertEqual(client.get("/api/aircraft").json()[0]["position_revision"], 1)
                new = client.post("/api/events", json=position(2000, 41.2).to_dict()).json()
                self.assertEqual(new["stream_sequence"], 2)
                self.assertEqual(reconnect.receive_json(), new)

    def test_stale_coordinates_cannot_roll_back_over_http_or_websocket(self):
        now = [0.0]
        service = TrafficService(store=AircraftStore(clock=lambda: now[0], wall_clock=lambda: 1.0))
        with TestClient(create_app(service=service)) as client:
            client.post("/api/events", json=position().to_dict())
            now[0] = 40.0
            with client.websocket_connect("/ws/aircraft") as websocket:
                old = position(500, 40.0)
                result = client.post("/api/events", json=old.to_dict())
                self.assertEqual(result.status_code, 200)
                update = websocket.receive_json()
                self.assertEqual(update["event"], old.to_dict())
                self.assertEqual(update["data"]["latitude"], 41.1)
                identity = DecodedEvent(icao="ABC123", event_type="identification", fields={"callsign": "NEWNAME"})
                client.post("/api/events", json=identity.to_dict())
                self.assertEqual(websocket.receive_json()["data"]["position_age_ms"], 40000)
            self.assertEqual(client.get("/api/aircraft").json()[0]["position_age_ms"], 40000)

    def test_all_supported_decoder_events_cross_network_contract(self):
        from netradar.decoder.decode import ADSBDecoder
        decoder = ADSBDecoder()
        with TestClient(create_app()) as client:
            with client.websocket_connect("/ws/aircraft") as websocket:
                for frame in supported_frames():
                    for event in decoder.decode(frame):
                        response = client.post("/api/events", json=event.to_dict())
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(websocket.receive_json()["event"], event.to_dict())

    def test_invalid_events_return_422_without_mutating_store(self):
        original = position().to_dict()
        invalid = [
            [], {**original, "icao": "abc123"}, {**original, "event_type": "unknown"},
            {**original, "schema_version": "2.0"}, {**original, "unexpected": 1},
            {**original, "observed_at_unix_ms": True}, {**original, "observed_at_unix_ms": -1},
            {**original, "observed_at_unix_ms": 999999999999999},
            {**original, "raw_frame": "bad"}, {**original, "metadata": []},
            {**original, "fields": {"latitude": 91, "longitude": 29}},
            {**original, "fields": {"latitude": 41}},
            {**original, "fields": {"track_deg": 360}},
            {**original, "fields": {"callsign": 123}},
            {**original, "fields": {"speed_knots": True}},
        ]
        with TestClient(create_app()) as client:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.assertEqual(client.post("/api/events", json=payload).status_code, 422)
            for fields in ({"speed_knots": float("nan")}, {"future": {"value": float("inf")}}):
                response = client.post("/api/events", content=json.dumps({**original, "fields": fields}),
                                       headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(client.get("/api/aircraft").json(), [])

    def test_http_cors_and_websocket_origin_validation(self):
        origin = "http://127.0.0.1:5173"
        with TestClient(create_app()) as client:
            response = client.get("/api/aircraft", headers={"Origin": origin})
            self.assertEqual(response.headers["access-control-allow-origin"], origin)
            headers = {"Origin": origin, "Access-Control-Request-Method": "POST",
                       "Access-Control-Request-Headers": "Content-Type"}
            self.assertEqual(client.options("/api/events", headers=headers).status_code, 200)
            headers["Origin"] = "http://untrusted.example"
            self.assertEqual(client.options("/api/events", headers=headers).status_code, 400)
            self.assertEqual(client.post("/api/events", json=position().to_dict(),
                                         headers={"Origin": headers["Origin"]}).status_code, 403)
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect("/ws/aircraft", headers={"Origin": headers["Origin"]}):
                    pass
            self.assertEqual(closed.exception.code, 1008)
            with client.websocket_connect("/ws/aircraft", headers={"Origin": origin}):
                self.assertEqual(client.get("/healthz").status_code, 200)

    def test_client_cannot_ingest_through_outbound_socket(self):
        with TestClient(create_app()) as client:
            with client.websocket_connect("/ws/aircraft") as websocket:
                websocket.send_json(position().to_dict())
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 1008)
            self.assertEqual(client.get("/api/aircraft").json(), [])

    def test_origin_environment_and_explicit_empty_allowlist(self):
        with patch.dict("os.environ", {"NETRADAR_ALLOWED_ORIGINS": "http://receiver.local:5173"}):
            with TestClient(create_app()) as client:
                response = client.get("/api/aircraft", headers={"Origin": "http://receiver.local:5173"})
                self.assertEqual(response.headers["access-control-allow-origin"], "http://receiver.local:5173")
        with TestClient(create_app(allowed_origins=())) as client:
            response = client.get("/api/aircraft", headers={"Origin": "http://127.0.0.1:5173"})
            self.assertNotIn("access-control-allow-origin", response.headers)
        with TestClient(create_app(allowed_origins=("*",))) as client:
            response = client.get("/api/aircraft", headers={"Origin": "http://anything.example"})
            self.assertEqual(response.headers["access-control-allow-origin"], "*")
            with client.websocket_connect("/ws/aircraft", headers={"Origin": "http://anything.example"}):
                pass


class BlockedSocket:
    """A stalled transport, independent of localhost TCP buffering."""
    headers = {}

    def __init__(self, app):
        self.app = app
        self.accepted = asyncio.Event()
        self.sending = asyncio.Event()
        self.release_send = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code = None

    async def accept(self):
        self.accepted.set()

    async def send_json(self, update):
        self.sending.set()
        await self.release_send.wait()

    async def receive(self):
        await self.closed.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, code):
        self.close_code = code
        self.closed.set()


@unittest.skipUnless(HAS_BACKEND, "Install requirements-dev.txt for HTTP/WebSocket integration tests")
class SlowSocketTest(unittest.IsolatedAsyncioTestCase):
    async def start_socket(self, *, timeout=5):
        service = TrafficService(queue_size=1)
        app = create_app(service=service, send_timeout=timeout)
        app.state.traffic = service
        endpoint = next(route.endpoint for route in app.routes if route.path == "/ws/aircraft")
        socket = BlockedSocket(app)
        task = asyncio.create_task(endpoint(socket))
        self.addAsyncCleanup(self.cleanup_socket, task, service)
        await asyncio.wait_for(socket.accepted.wait(), 1)
        await service.ingest(position())
        await asyncio.wait_for(socket.sending.wait(), 1)
        return service, socket, task

    async def cleanup_socket(self, task, service):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()

    async def test_stalled_network_send_closes_with_retry_and_cleans_subscription(self):
        service, socket, task = await self.start_socket(timeout=0.01)
        await asyncio.wait_for(task, 1)
        self.assertEqual(socket.close_code, 1013)
        self.assertEqual(service.subscriber_count, 0)

    async def test_overflow_closes_socket_after_inflight_send(self):
        service, socket, task = await self.start_socket()
        await service.ingest(position(2000, 41.2))
        await service.ingest(position(3000, 41.3))
        socket.release_send.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(socket.close_code, 1013)
        self.assertEqual(service.subscriber_count, 0)
        self.assertEqual((await service.snapshot())[0]["latitude"], 41.3)


if __name__ == "__main__":
    unittest.main()
