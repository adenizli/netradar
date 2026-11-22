import json
import unittest

from netradar.metrics import FunnelMetrics, STAGE_COUNTERS


class FunnelMetricsTest(unittest.TestCase):
    def test_no_activity_and_unavailable_have_distinct_status(self):
        clock = iter((0, 1_000_000_000, 2_000_000_000))
        metrics = FunnelMetrics("rf", sample_rate_hz=2_000_000, clock_ns=lambda: next(clock),
                                wall_clock_ns=lambda: 42_000_000_000)
        metrics.observe("acquisition")
        metrics.observe("decoder")
        running = metrics.snapshot()
        for name, stage in running["stages"].items():
            self.assertEqual(stage["counters"], dict.fromkeys(STAGE_COUNTERS[name], 0))
            self.assertIsNone(stage["last_activity_unix_ms"])
            self.assertIsNone(stage["latency"]["p99_ms"])
        self.assertEqual(running["run_status"], "running")
        self.assertEqual(running["stages"]["acquisition"]["status"], "measured")
        self.assertEqual(running["stages"]["acquisition"]["throughput"]["samples_per_second"], 0)
        self.assertEqual(running["stages"]["display"]["status"], "unavailable")
        self.assertIsNone(running["stages"]["display"]["throughput"]["transitions_per_second"])
        self.assertEqual(running["distinct_icao_windows"]["windows_seconds"]["60"],
                         {"count": 0, "lower_bound": False})
        metrics.finish()
        final = metrics.snapshot()
        self.assertEqual(final["run_status"], "completed")
        self.assertEqual(final["elapsed_seconds"], 2)
        json.dumps(final, allow_nan=False)

    def test_crc_activity_latency_and_monotonic_counters(self):
        clock = iter((1, 1_000_000_001, 2_000_000_001, 3_000_000_001))
        metrics = FunnelMetrics("rf", capture_id="test", sample_rate_hz=10,
                                clock_ns=lambda: next(clock), wall_clock_ns=lambda: 100_000_000_000)
        metrics.observe("decoder", counters={"frames": 1, "crc_clean": 1, "decoded_events": 1},
                        duration_ns=1000, head_sample_index=1,
                        valid_icao="40621D", icao_sample_index=0)
        metrics.observe("decoder", counters={"frames": 1, "crc_rejected": 1}, duration_ns=2000,
                        head_sample_index=2)
        with self.assertRaises(ValueError):
            metrics.observe("decoder", counters={"frames": 1, "crc_clean": -1})
        report = metrics.snapshot()
        decoder = report["stages"]["decoder"]
        self.assertEqual(report["capture_id"], "test")
        self.assertEqual(report["capture_head_sample_index"], 2)
        self.assertEqual(decoder["last_activity_elapsed_seconds"], 1)
        self.assertEqual(decoder["last_activity_unix_ms"], 101_000)
        self.assertEqual(decoder["counters"]["frames"], 2)
        self.assertEqual(decoder["throughput"]["messages_per_second"], 0.5)
        self.assertEqual(decoder["latency"]["sample_count"], 2)
        self.assertEqual(decoder["latency"]["sum_ms"], 0.003)
        self.assertEqual(decoder["latency"]["p50_ms"], 0.001)
        self.assertEqual(decoder["latency"]["p95_ms"], 0.002)
        self.assertEqual(decoder["latency"]["p99_ms"], 0.002)
        self.assertEqual(decoder["latency"]["buckets"][-1], {"upper_bound_ms": None, "count": 2})

    def test_windows_deduplicate_expire_and_indicate_eviction(self):
        metrics = FunnelMetrics("replay", sample_rate_hz=1, icao_capacity=2)
        for icao, index in (("40621D", 0), ("4840D6", 55), ("485020", 70), ("485020", 60)):
            metrics.observe("decoder", counters={"crc_clean": 1}, head_sample_index=index + 1,
                            valid_icao=icao, icao_sample_index=index)
        self.assertEqual(metrics.snapshot()["distinct_icao_windows"]["windows_seconds"], {
            "60": {"count": 2, "lower_bound": False},
            "300": {"count": 2, "lower_bound": True},
            "900": {"count": 2, "lower_bound": True},
        })
        metrics.observe("acquisition", head_sample_index=901)
        self.assertEqual(metrics.snapshot()["distinct_icao_windows"]["windows_seconds"], {
            "60": {"count": 0, "lower_bound": False},
            "300": {"count": 0, "lower_bound": False},
            "900": {"count": 2, "lower_bound": False},
        })
