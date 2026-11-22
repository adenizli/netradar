import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import netradar.telemetry as telemetry


class DiagnosticResourcesTest(unittest.TestCase):
    def setUp(self):
        self.previous = telemetry._throttling_cache
        telemetry._throttling_cache = (float("-inf"), None)
        self.addCleanup(setattr, telemetry, "_throttling_cache", self.previous)

    def test_firmware_probe_is_cached_and_preserves_the_flag_mask(self):
        with patch.object(telemetry.time, "monotonic", side_effect=[100, 101, 131]), \
                patch.object(telemetry.shutil, "which", return_value="/usr/bin/vcgencmd"), \
                patch.object(telemetry.subprocess, "run", return_value=SimpleNamespace(stdout="throttled=0x50005\n")) as run:
            self.assertEqual(telemetry.throttling_mask(), 0x50005)
            self.assertEqual(telemetry.throttling_mask(), 0x50005)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(telemetry.throttling_mask(), 0x50005)
            self.assertEqual(run.call_count, 2)

    def test_missing_or_failed_probe_is_unknown_and_request_path_never_probes(self):
        with patch.object(telemetry.shutil, "which", return_value=None):
            self.assertIsNone(telemetry.throttling_mask())
        telemetry._throttling_cache = (float("-inf"), None)
        with patch.object(telemetry.shutil, "which", return_value="vcgencmd"), \
                patch.object(telemetry.subprocess, "run", side_effect=subprocess.TimeoutExpired("vcgencmd", .25)):
            self.assertIsNone(telemetry.throttling_mask())
        with patch.object(telemetry, "throttling_mask", side_effect=AssertionError("request path must remain nonblocking")):
            self.assertIsNone(telemetry.process_resources()["throttling_mask"])


if __name__ == "__main__":
    unittest.main()
