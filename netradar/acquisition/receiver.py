"""Acquisition layer (owner: Yasir): CU8 IQ from a saved file or a live RTL-SDR.

Both sources yield ``netradar.contracts.IqChunk`` objects. Standard library only.

What DSP (Arda) can rely on
---------------------------
* ``chunk.data`` holds the raw CU8 bytes exactly as the dongle produced them or
  as they are stored in the file: ``[I0, Q0, I1, Q1, ...]``, uint8, zero near
  127.5. No offset removal, no magnitude, no filtering -- that is DSP's job.
* Every chunk has an even number of bytes and starts on an even byte offset,
  so ``data[0]`` is always an I byte.
* ``start_sample_index`` is zero-based, counted in COMPLEX samples (not
  bytes), and strictly increasing.
* Every chunk is ``chunk_samples`` long except possibly the last one.

Gap / overflow policy (``stream_rtlsdr`` only)
----------------------------------------------
A background thread drains the ``rtl_sdr`` pipe into a bounded queue, so the
USB/pipe buffers keep emptying while DSP is briefly busy. If the queue is full,
the NEWEST block is dropped (the reader never blocks) and counted in
``StreamStats.dropped_samples``. The next chunk that is emitted then starts
later than the previous one ended::

    chunk.start_sample_index > prev.start_sample_index + prev.sample_count

so indices keep matching real time (index / sample_rate = seconds since the
stream started). DSP must treat this as a discontinuity: discard the overlap
buffer (the last 239 samples) instead of stitching it to the new chunk,
otherwise a frame could be assembled from samples on both sides of the gap.

``replay_cu8`` never drops data; its chunks are always contiguous.
"""
from __future__ import annotations

import collections
import json
import logging
import math
from os import fstat
import queue
import subprocess
import threading
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from netradar.contracts import IqChunk

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE_HZ = 2_000_000
DEFAULT_CENTER_FREQUENCY_HZ = 1_090_000_000
TROUBLESHOOTING_HINT = "See docs/TROUBLESHOOTING.md."
# rtl_sdr / librtlsdr stderr lines that indicate lost samples or a device problem.
_STDERR_ALERTS = ("lost", "short read", "usb_claim_interface")


def _check_positive(name: str, value) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _read_full(stream, nbytes: int) -> bytes:
    """Read ``nbytes``, looping over short reads; returns fewer only at EOF."""
    buf = bytearray(nbytes)
    view = memoryview(buf)
    got = 0
    while got < nbytes:
        n = stream.readinto(view[got:])
        if not n:
            break
        got += n
    return bytes(view[:got])


# --------------------------------------------------------------------------- replay


def _sidecar_metadata(path: Path) -> dict:
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.warn(f"Ignoring unreadable sidecar {sidecar}: {exc}", RuntimeWarning, stacklevel=3)
        return {}
    return meta if isinstance(meta, dict) else {}


def replay_cu8(
    path,
    *,
    chunk_samples: int = 8192,
    sample_rate_hz: int | None = None,
    center_frequency_hz: int | None = None,
    strict: bool = False,
) -> Iterator[IqChunk]:
    """Stream a saved CU8 capture as contiguous ``IqChunk`` objects.

    The file is read ``chunk_samples * 2`` bytes at a time and never loaded
    whole. The last chunk may be shorter; an empty file yields nothing. A file
    with an odd byte count (truncated capture) loses its final dangling byte
    and triggers a ``RuntimeWarning``.
    ``strict=True`` rejects an odd-sized capture before yielding any chunks;
    use this for the decoder pipeline rather than silently accepting truncation.

    Metadata left as ``None`` is taken from the JSON sidecar next to the file
    (same stem, ``.json``; keys ``sample_rate_hz`` / ``center_frequency_hz``)
    and otherwise defaults to 2 MS/s at 1090 MHz. Explicit arguments win.

    Raises ``ValueError`` for ``chunk_samples <= 0`` and ``FileNotFoundError``
    for a missing file, both immediately rather than on first iteration.
    """
    _check_positive("chunk_samples", chunk_samples)
    path = Path(path)
    path.stat()  # FileNotFoundError now, not at the first next()
    meta = _sidecar_metadata(path) if sample_rate_hz is None or center_frequency_hz is None else {}
    if sample_rate_hz is None:
        sample_rate_hz = meta.get("sample_rate_hz", DEFAULT_SAMPLE_RATE_HZ)
    if center_frequency_hz is None:
        center_frequency_hz = meta.get("center_frequency_hz", DEFAULT_CENTER_FREQUENCY_HZ)
    _check_positive("sample_rate_hz", sample_rate_hz)
    _check_positive("center_frequency_hz", center_frequency_hz)
    return _replay(path, chunk_samples, sample_rate_hz, center_frequency_hz, strict)


def _replay(path: Path, chunk_samples: int, sample_rate_hz: int, center_frequency_hz: int,
            strict: bool = False) -> Iterator[IqChunk]:
    block_bytes = chunk_samples * 2
    index = 0
    with open(path, "rb") as f:
        if strict and fstat(f.fileno()).st_size % 2:
            raise ValueError("CU8 file ends with an unpaired I/Q byte")
        while True:
            data = _read_full(f, block_bytes)
            if len(data) % 2:
                if strict:
                    raise ValueError("CU8 file ends with an unpaired I/Q byte")
                warnings.warn(
                    f"{path}: odd byte count (truncated capture); dropping the final dangling byte",
                    RuntimeWarning,
                    stacklevel=2,
                )
                data = data[:-1]
            if not data:
                return
            yield IqChunk(data, index, sample_rate_hz, center_frequency_hz)
            index += len(data) // 2
            if len(data) < block_bytes:
                return


# --------------------------------------------------------------------------- live RTL-SDR


@dataclass
class StreamStats:
    """Counters for ``stream_rtlsdr``; pass an instance in and it is updated in place."""

    samples_emitted: int = 0
    chunks_emitted: int = 0
    dropped_samples: int = 0
    samples_received: int = 0  # read from rtl_sdr, including dropped ones
    elapsed_s: float = 0.0  # since rtl_sdr was started
    pid: int | None = None
    returncode: int | None = None  # set once rtl_sdr has exited and been reaped


class _RateMonitor:
    """Detects when fewer samples arrive than the wall clock says rtl_sdr should deliver."""

    def __init__(
        self,
        sample_rate_hz: int,
        *,
        warmup_s: float = 3.0,
        tolerance: float = 0.01,
        slack_samples: int = 131_072,  # one default rtl_sdr async buffer
        interval_s: float = 10.0,
        clock=time.monotonic,
    ):
        self.sample_rate_hz = sample_rate_hz
        self.warmup_s = warmup_s
        self.tolerance = tolerance
        self.slack_samples = slack_samples
        self.interval_s = interval_s
        self._clock = clock
        self._t0: float | None = None
        self._base = 0
        self._last_report: float | None = None

    def update(self, samples_received: int) -> float | None:
        """Return the deficit as a fraction when it should be reported, else ``None``."""
        now = self._clock()
        if self._t0 is None:
            # Start the clock at the first data, so rtl_sdr startup time is not a deficit.
            self._t0, self._base = now, samples_received
            return None
        elapsed = now - self._t0
        if elapsed < self.warmup_s:
            return None
        expected = elapsed * self.sample_rate_hz
        deficit = expected - (samples_received - self._base)
        if deficit <= self.tolerance * expected + self.slack_samples:
            return None
        if self._last_report is not None and now - self._last_report < self.interval_s:
            return None
        self._last_report = now
        return deficit / expected


class _ReaderState:
    def __init__(self):
        self.received = 0
        self.dropped = 0
        self.limit_reached = False
        self.error: BaseException | None = None


def _rtl_sdr_command(
    rtl_sdr_cmd: str | Sequence[str],
    *,
    sample_rate_hz: int,
    center_frequency_hz: int,
    gain_db: float | None,
    ppm: int,
    device_index: int,
    max_samples: int | None,
) -> list[str]:
    cmd = [rtl_sdr_cmd] if isinstance(rtl_sdr_cmd, str) else list(rtl_sdr_cmd)
    gain = "0" if gain_db is None else f"{gain_db:g}"  # rtl_sdr: -g 0 = automatic gain
    cmd += [
        "-f", str(center_frequency_hz),
        "-s", str(sample_rate_hz),
        "-g", gain,
        "-p", str(ppm),
        "-d", str(device_index),
    ]
    if max_samples is not None:
        cmd += ["-n", str(max_samples)]
    cmd.append("-")  # write samples to stdout
    return cmd


def stream_rtlsdr(
    *,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    center_frequency_hz: int = DEFAULT_CENTER_FREQUENCY_HZ,
    gain_db: float | None = 35.0,
    ppm: int = 0,
    device_index: int = 0,
    chunk_samples: int = 16384,
    max_samples: int | None = None,
    rtl_sdr_cmd: str | Sequence[str] = "rtl_sdr",
    stats: StreamStats | None = None,
    queue_max_chunks: int = 64,
) -> Iterator[IqChunk]:
    """Stream live IQ from the ``rtl_sdr`` CLI (apt package ``rtl-sdr``).

    ``gain_db=None`` selects automatic gain. ``max_samples`` stops after that
    many complex samples (also passed to rtl_sdr as ``-n``). ``rtl_sdr_cmd``
    may be a string or a list, so tests can substitute a fake program.

    Chunks are contiguous unless the internal queue (``queue_max_chunks``
    blocks) overflowed; then the next chunk's ``start_sample_index`` jumps
    forward by the dropped amount -- see the module docstring. DSP must not
    stitch its overlap buffer across such a gap.

    The process is started on the first ``next()``. If rtl_sdr exits before
    producing any samples (no device, device busy, DVB driver bound), or exits
    with an error later, ``RuntimeError`` is raised with its last stderr lines.
    Closing the generator (``break`` in a for loop, ``close()``, an exception,
    KeyboardInterrupt) terminates rtl_sdr and joins the reader threads. Use
    ``contextlib.closing`` when not exhausting the generator in a for loop, so
    the dongle is released promptly.
    """
    _check_positive("sample_rate_hz", sample_rate_hz)
    _check_positive("center_frequency_hz", center_frequency_hz)
    _check_positive("chunk_samples", chunk_samples)
    _check_positive("queue_max_chunks", queue_max_chunks)
    if max_samples is not None:
        _check_positive("max_samples", max_samples)
    if type(ppm) is not int or type(device_index) is not int or device_index < 0:
        raise ValueError("ppm must be an integer and device_index a nonnegative integer")
    if gain_db is not None and (type(gain_db) not in (float, int) or not math.isfinite(gain_db)):
        raise ValueError("gain_db must be finite or None for automatic gain")
    if not rtl_sdr_cmd or (not isinstance(rtl_sdr_cmd, str) and
                          any(not isinstance(part, str) or not part for part in rtl_sdr_cmd)):
        raise ValueError("rtl_sdr_cmd must name an executable")
    cmd = _rtl_sdr_command(
        rtl_sdr_cmd,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=center_frequency_hz,
        gain_db=gain_db,
        ppm=ppm,
        device_index=device_index,
        max_samples=max_samples,
    )
    return _stream(
        cmd,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=center_frequency_hz,
        chunk_samples=chunk_samples,
        max_samples=max_samples,
        stats=stats if stats is not None else StreamStats(),
        queue_max_chunks=queue_max_chunks,
    )


def _pump_samples(stdout, block_bytes, limit_bytes, chunks, stop, done, state, monitor):
    """Reader thread: drain rtl_sdr stdout into ``chunks`` as (start_index, bytes)."""
    index = 0
    dropped_in_episode = 0
    try:
        while not stop.is_set():
            want = block_bytes
            if limit_bytes is not None:
                want = min(want, limit_bytes - 2 * state.received)
                if want <= 0:
                    state.limit_reached = True
                    break
            data = _read_full(stdout, want)
            if len(data) % 2:
                log.warning("rtl_sdr output ended on an odd byte; dropping it")
                data = data[:-1]
            if not data:
                break
            n = len(data) // 2
            state.received += n
            try:
                chunks.put_nowait((index, data))
            except queue.Full:
                if not dropped_in_episode:
                    log.warning("IQ queue full (consumer too slow); dropping samples from index %d", index)
                dropped_in_episode += n
                state.dropped += n
            else:
                if dropped_in_episode:
                    log.warning(
                        "Dropped %d samples; stream resumes at index %d (discontinuity)",
                        dropped_in_episode, index,
                    )
                    dropped_in_episode = 0
            index += n
            deficit = monitor.update(state.received)
            if deficit is not None:
                log.warning(
                    "Receiving %.1f%% fewer samples than %d S/s; samples are probably lost "
                    "upstream (USB hub / power supply). %s",
                    100 * deficit, monitor.sample_rate_hz, TROUBLESHOOTING_HINT,
                )
            if len(data) < want:
                break  # EOF
    except Exception as exc:  # re-raised in the consumer
        state.error = exc
    finally:
        done.set()


def _pump_stderr(stderr, tail):
    """Stderr thread: keep the last lines for error messages and surface sample-loss alerts."""
    for raw in iter(stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        tail.append(line)
        if any(alert in line.lower() for alert in _STDERR_ALERTS):
            log.warning("rtl_sdr: %s", line)
        else:
            log.debug("rtl_sdr: %s", line)


def _terminate(proc: subprocess.Popen, timeout_s: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log.warning("rtl_sdr did not exit after terminate(); killing it")
        proc.kill()
        proc.wait()


def _rtl_sdr_error(cmd, message: str, tail) -> RuntimeError:
    lines = "\n".join(f"  {line}" for line in tail) or "  (no stderr output)"
    return RuntimeError(f"{cmd[0]} {message}\nLast stderr lines:\n{lines}\n{TROUBLESHOOTING_HINT}")


def _stream(cmd, *, sample_rate_hz, center_frequency_hz, chunk_samples, max_samples, stats, queue_max_chunks):
    chunks: queue.Queue = queue.Queue(maxsize=queue_max_chunks)
    stop = threading.Event()
    reader_done = threading.Event()
    state = _ReaderState()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    t_start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
    except OSError as exc:
        raise RuntimeError(
            f"Could not start {cmd[0]!r}: {exc}. Install it with 'sudo apt install rtl-sdr'. "
            f"{TROUBLESHOOTING_HINT}"
        ) from exc
    stats.pid, stats.returncode = proc.pid, None

    reader = threading.Thread(
        target=_pump_samples,
        args=(
            proc.stdout,
            chunk_samples * 2,
            None if max_samples is None else max_samples * 2,
            chunks,
            stop,
            reader_done,
            state,
            _RateMonitor(sample_rate_hz),
        ),
        name="netradar-rtl-reader",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_pump_stderr, args=(proc.stderr, stderr_tail), name="netradar-rtl-stderr", daemon=True
    )

    def sync_stats():
        stats.dropped_samples = state.dropped
        stats.samples_received = state.received
        stats.elapsed_s = time.monotonic() - t_start

    try:
        reader.start()
        stderr_reader.start()
        while True:
            try:
                start, data = chunks.get(timeout=0.1)
            except queue.Empty:
                if reader_done.is_set() and chunks.empty():
                    break
                continue
            chunk = IqChunk(data, start, sample_rate_hz, center_frequency_hz)
            stats.samples_emitted += chunk.sample_count
            stats.chunks_emitted += 1
            sync_stats()
            yield chunk

        if state.error is not None:
            raise RuntimeError(f"Reading samples from {cmd[0]} failed: {state.error}") from state.error
        if not state.limit_reached:
            # rtl_sdr closed its stdout on its own: find out why.
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                returncode = None
            stderr_reader.join(timeout=2)
            if returncode is None:
                raise _rtl_sdr_error(cmd, "closed stdout without exiting", stderr_tail)
            if state.received == 0:
                raise _rtl_sdr_error(
                    cmd,
                    f"exited (code {returncode}) before producing any samples -- "
                    "device missing, busy, or claimed by the DVB kernel driver?",
                    stderr_tail,
                )
            if returncode:
                raise _rtl_sdr_error(cmd, f"exited unexpectedly with code {returncode}.", stderr_tail)
            if max_samples is not None and state.received < max_samples:
                raise _rtl_sdr_error(
                    cmd, f"ended after {state.received} samples; expected {max_samples}", stderr_tail,
                )
    finally:
        stop.set()
        _terminate(proc)
        if reader.ident is not None:
            reader.join(timeout=2)
        if stderr_reader.ident is not None:
            stderr_reader.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        stats.returncode = proc.returncode
        sync_stats()
