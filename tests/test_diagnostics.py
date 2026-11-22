"""Diagnostics wire, lifecycle and Prometheus checks without an SDR."""
from copy import deepcopy
from importlib.util import find_spec
import json
import unittest

from netradar.metrics import FunnelMetrics

HAS_BACKEND = find_spec("fastapi") is not None and find_spec("httpx") is not None
if HAS_BACKEND:
    from fastapi.testclient import TestClient
    from netradar.backend.diagnostics import MAX_BODY_BYTES, RETAINED_CAPTURES, STALE_SECONDS, Diagnostics
    from netradar.backend.server import create_app
    from tests.test_stream import position


@unittest.skipUnless(HAS_BACKEND, "Install requirements-dev.txt")
class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.diagnostics = Diagnostics(clock=lambda: self.now, expected_producer=True)
        self.app = create_app(diagnostics=self.diagnostics, allowed_origins=("*",))

    def client(self, address="127.0.0.1"):
        return TestClient(self.app, client=(address, 12345))

    def capture(self, source="synthetic", **kwargs):
        return FunnelMetrics(
            source, clock_ns=lambda: self.now * 10**9,
            wall_clock_ns=lambda: self.now * 10**9, **kwargs,
        )

    @staticmethod
    def payload(metrics, sequence=1):
        return {
            "sequence": sequence, "capture": metrics.snapshot(),
            "resources": {"cpu_seconds": 4.2, "peak_rss_bytes": 8400,
                          "temperature_celsius": None, "throttling_mask": None},
        }

    def send(self, client, metrics, sequence=1):
        response = client.post("/api/diagnostics", json=self.payload(metrics, sequence))
        self.assertEqual(response.status_code, 204, response.text)

    def test_synthetic_lifecycle_and_activity_time(self):
        metrics = self.capture()
        with self.client() as client:
            get = lambda: client.get("/api/diagnostics").json()
            self.assertEqual(get()["receiver_state"], "starting")
            self.assertIsNone(get()["capture"])
            self.now += 5
            self.assertEqual(get()["receiver_state"], "receiver_down")
            metrics.observe("acquisition")
            self.send(client, metrics)
            self.assertEqual(get()["receiver_state"], "receiver_silent")
            self.assertIsNone(get()["last_iq_at_unix_ms"])
            self.now += 1
            metrics.observe("acquisition", counters={"rf_bytes": 4, "complex_samples": 2})
            self.send(client, metrics, 2)
            report = get()
            self.assertEqual(report["receiver_state"], "no_valid_traffic")
            self.assertEqual(report["capture"]["source"], "synthetic")
            self.assertEqual(report["last_iq_at_unix_ms"], 106_000)
            self.assertIsNone(report["last_valid_crc_at_unix_ms"])
            metrics.observe("decoder", counters={"crc_clean": 1}, duration_ns=100_000)
            self.send(client, metrics, 3)
            self.assertEqual(get()["receiver_state"], "receiving")
            self.assertEqual(get()["last_valid_crc_at_unix_ms"], 106_000)
            self.now += 5
            self.assertEqual(get()["receiver_state"], "telemetry_stale")
            self.send(client, metrics, 4)
            self.assertEqual(get()["receiver_state"], "receiver_silent")
            self.assertEqual(get()["last_iq_at_unix_ms"], 106_000)
            metrics.finish("failed")
            self.send(client, metrics, 5)
            self.assertEqual(get()["receiver_state"], "receiver_down")
            self.now += 1
            replay = self.capture("replay")
            replay.finish()
            self.send(client, replay)
            final = get()
            self.assertEqual(final["receiver_state"], "completed")
            self.assertEqual(final["capture"]["source"], "replay")
            self.assertIsNone(final["last_iq_at_unix_ms"])
            self.assertEqual(final["retained_captures"][0]["capture_id"], metrics.capture_id)

    def test_ordered_resets_and_bounded_retention(self):
        with self.client() as client:
            oldest = self.capture("rf")
            oldest.observe("acquisition", counters={"rf_bytes": 2})
            self.send(client, oldest, 3)
            self.assertEqual(client.post("/api/diagnostics", json=self.payload(oldest, 2)).status_code, 422)
            bad = self.payload(oldest, 4)
            bad["capture"]["stages"]["acquisition"]["counters"]["rf_bytes"] = 0
            self.assertEqual(client.post("/api/diagnostics", json=bad).status_code, 422)
            for _ in range(RETAINED_CAPTURES + 2):
                self.now += 1
                self.send(client, self.capture("rf"))
            self.assertEqual(client.post("/api/diagnostics", json=self.payload(oldest, 99)).status_code, 422)
            self.assertEqual(len(client.get("/api/diagnostics").json()["retained_captures"]), RETAINED_CAPTURES)

    def test_new_capture_after_producer_clock_steps_backwards(self):
        # The backend clock (self.now) only moves forward; the producer's wall clock
        # steps back by an hour, e.g. a Pi without an RTC correcting its time.
        self.now = 100_000

        def capture(wall_offset_s):
            return FunnelMetrics("rf", clock_ns=lambda: self.now * 10**9,
                                 wall_clock_ns=lambda: (self.now + wall_offset_s) * 10**9)

        with self.client() as client:
            first = capture(0)
            first.observe("acquisition", counters={"rf_bytes": 2})
            self.send(client, first)
            # While the first capture is live, an earlier-starting capture is a conflict.
            self.now += 1
            self.assertEqual(client.post("/api/diagnostics", json=self.payload(capture(-3600))).status_code, 422)
            # The first producer stops sending heartbeats and is restarted with the stepped clock.
            self.now += STALE_SECONDS
            restarted = capture(-3600)
            self.send(client, restarted)
            report = client.get("/api/diagnostics").json()
            self.assertEqual(report["capture"]["capture_id"], restarted.capture_id)
            self.assertEqual(report["retained_captures"][0]["capture_id"], first.capture_id)
            # A finished capture never blocks the next one, even if that one starts earlier.
            restarted.finish()
            self.send(client, restarted, 2)
            self.now += 1
            self.send(client, capture(-7200))
            # Returning to a retained capture is still rejected.
            self.assertEqual(client.post("/api/diagnostics", json=self.payload(first, 9)).status_code, 422)

    def test_malformed_oversized_cross_origin_and_non_loopback(self):
        payload = self.payload(self.capture())
        with self.client() as client:
            for headers in ({"Origin": "http://evil.test"}, {"Sec-Fetch-Site": "same-origin"},
                            {"X-Forwarded-For": "127.0.0.1"}):
                self.assertEqual(client.post("/api/diagnostics", json=payload, headers=headers).status_code, 403)
            self.assertEqual(client.post("/api/diagnostics", content=b"{}").status_code, 415)
            headers = {"Content-Type": "application/json"}
            self.assertEqual(client.post("/api/diagnostics", content=b"x" * (MAX_BODY_BYTES + 1),
                                         headers=headers).status_code, 413)
            for key, value in (("elapsed_seconds", float("nan")), ("sample_rate_hz", 0),
                               ("capture_id", "A" * 97)):
                bad = deepcopy(payload)
                bad["capture"][key] = value
                self.assertEqual(client.post("/api/diagnostics", content=json.dumps(bad),
                                             headers=headers).status_code, 422)
            self.assertIsNone(client.get("/api/diagnostics").json()["capture"])
        with self.client("192.0.2.1") as client:
            self.assertEqual(client.post("/api/diagnostics", json=payload).status_code, 403)

    def test_prometheus_units_and_backend_counts(self):
        metrics = self.capture("synthetic", sample_rate_hz=2_000_000)
        self.now += 2
        metrics.observe("acquisition", counters={"rf_bytes": 400, "complex_samples": 200,
                                                 "queue_dropped_chunks": 2, "queue_drop_events": 1,
                                                 "queue_dropped_samples": 20}, duration_ns=100_000)
        metrics.observe("decoder")
        metrics.observe_queue(depth=3, capacity=7)
        metrics.observe_queue(depth=1, capacity=7)
        metrics.observe("delivery")
        metrics.observe_queue(stage="delivery", depth=2, capacity=5)
        metrics.observe_queue(stage="delivery", depth=0, capacity=5)
        for name in ("queue_dwell_latency", "consumer_chunk_latency"):
            metrics.observe_acquisition_latency(name, self.now * 10**9 - 100_000)
        payload = self.payload(metrics)
        with self.client() as client:
            legacy = deepcopy(payload)
            legacy["capture"]["stages"]["acquisition"]["counters"].pop("queue_drop_events")
            self.assertEqual(client.post("/api/diagnostics", json=legacy).status_code, 204)
            payload["sequence"] = 2
            invalid = deepcopy(payload)
            invalid["capture"]["stages"]["acquisition"]["counters"]["queue_drop_events"] = 3
            self.assertEqual(client.post("/api/diagnostics", json=invalid).status_code, 422)
            self.assertEqual(client.post("/api/diagnostics", json=payload).status_code, 204)
            self.assertEqual(client.post("/api/events", json=position().to_dict()).status_code, 200)
            self.assertEqual(client.post("/api/events", json={}).status_code, 422)
            report = client.get("/api/diagnostics")
            self.assertEqual(report.headers["cache-control"], "no-store")
            queue = report.json()["queue"]
            self.assertEqual(queue["drop_fraction"], 0.1)
            self.assertEqual(queue["fill_ratio"], 1 / 7)
            self.assertEqual(queue["dropped_samples_per_second"], 10)
            body = client.get("/metrics")
            self.assertIn("version=0.0.4", body.headers["content-type"])
            self.assertIn("netradar_backend_events_accepted_total 1", body.text)
            self.assertIn("netradar_backend_events_rejected_total 1", body.text)
            self.assertIn('netradar_acquisition_rf_bytes_total{source="synthetic"} 400', body.text)
            self.assertIn('netradar_acquisition_queue_drop_events_total{source="synthetic"} 1', body.text)
            self.assertIn('netradar_acquisition_queue_dropped_samples_per_second{source="synthetic"} 10.0', body.text)
            self.assertIn('netradar_decoder_crc_clean_total{source="synthetic"} 0', body.text)
            self.assertIn('netradar_acquisition_latency_seconds_sum{source="synthetic"} 0.0001', body.text)
            self.assertIn('netradar_acquisition_latency_seconds_bucket{source="synthetic",le="+Inf"} 1', body.text)
            for name in ("queue_dwell_latency", "consumer_chunk_latency"):
                self.assertIn(f'netradar_acquisition_{name}_seconds_count{{source="synthetic"}} 1', body.text)
            self.assertIn('netradar_delivery_queue_high_water{source="synthetic"} 2', body.text)
            self.assertIn('netradar_delivery_queue_depth{source="synthetic"} 0', body.text)
            self.assertNotIn("netradar_dsp_samples_total", body.text)
            self.assertNotIn(metrics.capture_id, body.text)
            self.assertNotIn("icao=", body.text)
            self.assertNotIn("ABC123", body.text)
            self.assertIn('netradar_distinct_icao{source="synthetic",window_seconds="300"} 0', body.text)
            self.assertEqual(body.text.count("# TYPE netradar_distinct_icao gauge"), 1)
