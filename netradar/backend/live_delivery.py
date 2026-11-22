"""Bounded FIFO delivery for live aircraft events."""
from collections import deque
from threading import Condition, Thread
from urllib.error import HTTPError

from netradar.backend.forward import EventPoster


class LiveDelivery:
    def __init__(self, url, metrics, *, capacity, request_timeout, drain_timeout):
        self._post = EventPoster(url, request_timeout)
        self._metrics = metrics
        self._capacity = capacity
        self._drain_timeout = drain_timeout
        self._condition = Condition()
        self._pending = deque()
        self._active = False
        self._closed = False
        self._error = None
        self._counts = dict.fromkeys(("accepted", "rejected", "undelivered", "unconfirmed"), 0)
        self._record()
        self._worker = Thread(target=self._run, name="netradar-event-delivery", daemon=True)
        self._worker.start()

    def check(self):
        with self._condition:
            if self._error is not None:
                raise RuntimeError(self.summary())

    def submit(self, event):
        with self._condition:
            if self._error is not None:
                self._record("undelivered")
            elif len(self._pending) == self._capacity:
                # Keep the previously queued events in order and drain them.
                self._error = "aircraft delivery queue full"
                self._record("undelivered")
            else:
                self._pending.append(event)
                self._condition.notify()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        try:
            self._worker.join(self._drain_timeout)
        finally:
            with self._condition:
                if self._active or self._pending:
                    self._abort("aircraft delivery drain interrupted or timed out")
        self.check()

    def summary(self):
        counts = " ".join(f"{name}={count}" for name, count in self._counts.items())
        return f"{self._error} ({counts})" if self._error is not None else counts

    def _record(self, outcome=None, count=1, started=None):
        counters = None
        if outcome is not None:
            self._counts[outcome] += count
            counters = {outcome: count}
        if self._metrics is not None:
            self._metrics.observe("delivery", counters=counters,
                                  duration_ns=self._metrics.elapsed_latency(started))

    def _abort(self, reason):
        self._error = self._error or reason
        if self._active:
            self._record("unconfirmed")
            self._active = False
        if self._pending:
            self._record("undelivered", len(self._pending))
            self._pending.clear()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending:
                    return
                event = self._pending.popleft()
                self._active = True
            started = self._metrics.begin_latency("delivery") if self._metrics is not None else None
            outcome = "accepted"
            error = None
            try:
                self._post(event)
            except Exception as exc:
                error = f"aircraft HTTP delivery failed: {exc}"
                outcome = "rejected" if isinstance(exc, HTTPError) and 400 <= exc.code < 500 else "unconfirmed"
            with self._condition:
                if not self._active:
                    return  # The caller finalized a timed-out drain.
                self._active = False
                self._record(outcome, started=started)
                if error is not None:
                    self._abort(error)
                    return
