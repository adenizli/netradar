#!/usr/bin/env python3
"""Relay rtl_sdr CU8 to the pipeline and retain small, contiguous sample windows."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread


def relay(source, sink, directory, *, capture_id, rate, frequency, period, length, slots, max_samples):
    """Copy the stream unchanged, saving only the requested sample intervals."""
    period_bytes, length_bytes = 2 * rate * period, 2 * rate * length
    offset = 0
    slot = 0
    snippet = None
    with ExitStack() as files:
        while offset < 2 * max_samples:
            block = source.read1(min(131_072, 2 * max_samples - offset))
            if not block:
                break
            sink.write(block)
            end = offset + len(block)
            while slot < slots and slot * period_bytes < end:
                start = slot * period_bytes
                stop = start + length_bytes
                if snippet is None:
                    path = directory / f"snippet-{slot:03d}.cu8"
                    snippet = files.enter_context(path.open("xb"))
                snippet.write(block[max(0, start - offset):stop - offset])
                if end < stop:
                    break
                # Publish a sidecar only when the entire contiguous window exists.
                files.pop_all().close()
                path.with_suffix(".json").write_text(json.dumps({
                    "kind": "real_receiver_capture", "file": path.name,
                    "capture_id": capture_id, "start_sample_index": start // 2,
                    "sample_rate_hz": rate, "center_frequency_hz": frequency,
                    "sample_format": "cu8_interleaved_iq", "complex_samples": length_bytes // 2,
                }, indent=2) + "\n", encoding="utf-8")
                snippet = None
                slot += 1
            offset = end
    return offset // 2


def copy_diagnostics(source, path):
    with path.open("wb") as log:
        for line in source:
            log.write(line)
            log.flush()
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()


def main():
    settings = json.loads(os.environ["NETRADAR_FIELD_TAP"])
    directory = Path(settings.pop("directory"))
    executable = settings.pop("executable")

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    arguments = sys.argv[1:]
    limit = arguments.index("-n")
    max_samples = int(arguments[limit + 1])
    # rtl_sdr stores its -n limit in a 32-bit BYTE counter. Keep the
    # application-level sample limit for captures larger than 4 GiB.
    del arguments[limit:limit + 2]
    command = [executable, *arguments]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as receiver:
        diagnostics = Thread(target=copy_diagnostics, args=(receiver.stderr, directory.parent / "rtl-sdr.log"))
        diagnostics.start()
        try:
            forwarded = relay(receiver.stdout, sys.stdout.buffer, directory, max_samples=max_samples, **settings)
            sys.stdout.buffer.flush()
            return 0 if forwarded == max_samples else receiver.wait()
        except BrokenPipeError:
            with open(os.devnull, "wb") as devnull:
                os.dup2(devnull.fileno(), sys.stdout.fileno())
            return 1
        finally:
            if receiver.poll() is None:
                receiver.terminate()
                try:
                    receiver.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    receiver.kill()
            diagnostics.join()


if __name__ == "__main__":
    sys.exit(main())
