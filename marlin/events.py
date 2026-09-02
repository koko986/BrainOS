"""Thread-safe event fan-out used by the desktop cockpit."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Empty, Queue
from typing import Any


@dataclass(frozen=True, slots=True)
class MarlinEvent:
    type: str
    data: dict[str, Any]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "timestamp": self.timestamp}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Queue[MarlinEvent]] = set()
        self._lock = threading.Lock()

    def publish(self, event_type: str, **data: Any) -> MarlinEvent:
        event = MarlinEvent(event_type, data, datetime.now(UTC).isoformat(timespec="seconds"))
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def subscribe(self) -> Iterator[MarlinEvent]:
        queue: Queue[MarlinEvent] = Queue()
        with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                try:
                    yield queue.get(timeout=30)
                except Empty:
                    yield MarlinEvent("heartbeat", {}, datetime.now(UTC).isoformat(timespec="seconds"))
        finally:
            with self._lock:
                self._subscribers.discard(queue)

    async def next_async(self, queue: Queue[MarlinEvent]) -> MarlinEvent:
        return await asyncio.to_thread(queue.get)

    def create_queue(self) -> Queue[MarlinEvent]:
        queue: Queue[MarlinEvent] = Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def remove_queue(self, queue: Queue[MarlinEvent]) -> None:
        with self._lock:
            self._subscribers.discard(queue)
