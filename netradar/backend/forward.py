"""Forward decoded-event or pipeline-update JSONL to the local FastAPI service."""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from netradar.contracts import DecodedEvent


def send_event(base_url: str, event: DecodedEvent, *, timeout: float = 5.0):
    """Blocking producer adapter; never call on the ASGI event loop."""
    url = urlsplit(base_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("Backend URL must be http/https without credentials, query or fragment")
    request = Request(
        base_url.rstrip("/") + "/api/events", data=event.to_json().encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--input", type=Path, help="JSONL path; otherwise read stdin")
    args = parser.parse_args()
    line_number = 0
    try:
        stream = args.input.open() if args.input else sys.stdin
        try:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict) and payload.get("type") == "aircraft_update":
                    payload = payload.get("event")
                send_event(args.url, DecodedEvent.from_dict(payload))
        finally:
            if stream is not sys.stdin:
                stream.close()
    except (ValueError, TypeError, KeyError, OSError, URLError) as exc:
        parser.exit(2, f"netradar.backend.forward: line {line_number}: {exc}\n")


if __name__ == "__main__":
    main()
