from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class LiveBroker:
    """Small in-process SSE broadcaster; clients recover state through API reads."""

    def __init__(self, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[tuple[str, str]]] = set()

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        if not event.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid SSE event name")
        serialized = json.dumps(payload, separators=(",", ":"), default=str)
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait((event, serialized))
            except asyncio.QueueFull:
                continue

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(
            maxsize=self._queue_size
        )
        self._subscribers.add(queue)
        try:
            yield "retry: 3000\n\n"
            yield ": connected\n\n"
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event}\ndata: {payload}\n\n"
        finally:
            self._subscribers.discard(queue)


live_broker = LiveBroker()
