from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import unittest

from netradar.backend.forward import send_event
from tests.test_stream import position


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def receiver():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"accepted": true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ForwardTest(unittest.TestCase):
    def test_producer_posts_full_event_without_timestamp_fabrication(self):
        event = position(None)
        with receiver() as (url, received):
            self.assertEqual(send_event(url, event), {"accepted": True})
        self.assertEqual(received, [("/api/events", event.to_dict())])

    def test_fixture_pipeline_output_can_be_forwarded_on_stdin(self):
        pipeline = subprocess.run(
            [sys.executable, "-m", "netradar.pipeline", "--fixture", "fixtures/synthetic_adsb_2msps.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        with receiver() as (url, received):
            result = subprocess.run(
                [sys.executable, "-m", "netradar.backend.forward", "--url", url],
                input=pipeline.stdout, cwd=ROOT, capture_output=True, text=True, check=True,
            )
        self.assertEqual(result.stdout, "")
        expected = [json.loads(line)["event"] for line in pipeline.stdout.splitlines()]
        self.assertEqual([event for path, event in received], expected)
        self.assertEqual(len(received), 7)

    def test_invalid_input_stops_before_network_and_reports_line(self):
        result = subprocess.run(
            [sys.executable, "-m", "netradar.backend.forward"],
            input='\n{"type":"aircraft_update","event":null}\n',
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 2", result.stderr)
        self.assertIn("JSON object", result.stderr)

    def test_invalid_backend_url_is_rejected(self):
        for url in ("file:///tmp/data", "http://user:password@localhost", "http://localhost?q=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                send_event(url, position())


if __name__ == "__main__":
    unittest.main()
