"""One event-loop owner for aircraft state, decoder and bounded subscribers."""
import asyncio
from dataclasses import dataclass
from uuid import uuid4

from netradar.backend.state import AircraftStore
from netradar.contracts import DecodedEvent, ModeSFrame
from netradar.decoder.decode import ADSBDecoder
from netradar.pipeline import DecoderPipeline


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue
    close_code: int | None = None


class TrafficService:
    """Call async methods on the ASGI loop; use HTTP from other processes.

    State mutation and fan-out have no await between them. A snapshot therefore
    cannot observe a half-applied event, and slow clients never block decoding.
    """

    def __init__(self, *, store: AircraftStore | None = None,
                 decoder: ADSBDecoder | None = None, queue_size: int = 256):
        if queue_size < 1:
            raise ValueError("Subscriber queue size must be positive")
        self.store = store if store is not None else AircraftStore()
        self.pipeline = DecoderPipeline(store=self.store, decoder=decoder, publish=self._publish)
        self.queue_size = queue_size
        self._subscribers: set[Subscription] = set()
        self._loop = None
        self._closed = False
        self.stream_id = uuid4().hex
        self._sequence = 0
        self._aircraft_sequences: dict[str, int] = {}

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if loop is not self._loop or self._closed:
            raise RuntimeError("TrafficService requires its active owner event loop")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        self._check_loop()
        subscription = Subscription(asyncio.Queue(maxsize=self.queue_size))
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription):
        self._subscribers.discard(subscription)

    @staticmethod
    def _stop(subscription: Subscription, code: int):
        subscription.close_code = code
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(None)

    def _publish(self, update: dict):
        self._sequence += 1
        self._aircraft_sequences[update['data']['icao']] = self._sequence
        update.update(stream_id=self.stream_id, stream_sequence=self._sequence)
        update['data'].update(stream_id=self.stream_id, stream_sequence=self._sequence)
        for subscription in self._subscribers:
            if subscription.close_code is not None:
                continue
            try:
                subscription.queue.put_nowait(update)
            except asyncio.QueueFull:
                # Explicit reconnect + snapshot, rather than silently losing events.
                self._stop(subscription, 1013)

    async def ingest(self, event: DecodedEvent) -> dict:
        self._check_loop()
        update = self.store.apply(event)
        self._publish(update)
        return update

    async def handle_frame(self, frame: ModeSFrame) -> list[dict]:
        self._check_loop()
        return self.pipeline.handle_frame(frame)

    async def snapshot(self) -> list[dict]:
        self._check_loop()
        snapshot = self.store.snapshot()
        for aircraft in snapshot:
            aircraft.update(stream_id=self.stream_id,
                            stream_sequence=self._aircraft_sequences.get(aircraft['icao'], 0))
        return snapshot

    async def close(self):
        self._check_loop()
        self._closed = True
        for subscription in self._subscribers:
            self._stop(subscription, 1001)
