"""In-process, fire-and-forget, error-isolating event bus (D6).

No broker, no Redis. Handler exceptions are caught, logged, and republished
as a `system.handler_error` event; they never propagate to the publisher. A
slow subscriber gets a bounded queue and is dropped from rather than allowed
to apply backpressure to the publisher.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[["Event"], Awaitable[None]]
Unsubscribe = Callable[[], None]

_HANDLER_ERROR_TYPE = "system.handler_error"
_DEFAULT_QUEUE_SIZE = 100
_STOP_DRAIN_TIMEOUT = 5.0


def _new_id() -> str:
    return uuid.uuid4().hex


class Event(BaseModel):
    id: str = Field(default_factory=_new_id)
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _matches(pattern: str, event_type: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return event_type == pattern[:-2] or event_type.startswith(pattern[:-1])
    return pattern == event_type


@dataclass
class _Subscriber:
    id: int
    pattern: str
    handler: Handler
    queue: asyncio.Queue[Event]
    task: asyncio.Task[None] | None = None
    delivered: int = 0
    dropped: int = 0


class EventBus:
    """Component implementing the in-process event bus."""

    name = "event_bus"

    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[int, _Subscriber] = {}
        self._id_counter = itertools.count(1)
        self._state = ComponentState.NEW
        self._publishing_handler_error = False

    @property
    def state(self) -> ComponentState:
        return self._state

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._state = ComponentState.STARTED

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        subscribers = list(self._subscribers.values())
        for sub in subscribers:
            if sub.task is not None:
                sub.task.cancel()
        for sub in subscribers:
            if sub.task is not None:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(_await_cancelled(sub.task)), timeout=_STOP_DRAIN_TIMEOUT
                    )
        self._subscribers.clear()
        self._state = ComponentState.STOPPED

    def subscribe(self, pattern: str, handler: Handler) -> Unsubscribe:
        sub_id = next(self._id_counter)
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        sub = _Subscriber(id=sub_id, pattern=pattern, handler=handler, queue=queue)
        sub.task = asyncio.ensure_future(self._consume(sub))
        self._subscribers[sub_id] = sub

        def _unsubscribe() -> None:
            existing = self._subscribers.pop(sub_id, None)
            if existing is not None and existing.task is not None:
                existing.task.cancel()

        return _unsubscribe

    async def publish(self, event: Event) -> None:
        for sub in list(self._subscribers.values()):
            if not _matches(sub.pattern, event.type):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest queued event to make room, never block the
                # publisher.
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - race guard
                    sub.queue.get_nowait()
                sub.dropped += 1
                with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - race guard
                    sub.queue.put_nowait(event)

    async def _consume(self, sub: _Subscriber) -> None:
        while True:
            event = await sub.queue.get()
            try:
                await sub.handler(event)
                sub.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate handler failures
                logger.error(
                    "Event handler failed",
                    extra={"pattern": sub.pattern, "event_type": event.type, "error": str(exc)},
                )
                await self._publish_handler_error(sub, event, exc)

    async def _publish_handler_error(self, sub: _Subscriber, event: Event, exc: Exception) -> None:
        if event.type == _HANDLER_ERROR_TYPE:
            # Guard against infinite recursion: an error handling a
            # handler-error event is only logged, never republished.
            logger.error(
                "Handler for system.handler_error itself failed; not republishing",
                extra={"error": str(exc)},
            )
            return
        error_event = Event(
            type=_HANDLER_ERROR_TYPE,
            source="event_bus",
            payload={
                "subscriber_pattern": sub.pattern,
                "original_event_id": event.id,
                "original_event_type": event.type,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        await self.publish(error_event)

    def stats(self) -> dict[int, dict[str, int]]:
        return {
            sub_id: {"delivered": sub.delivered, "dropped": sub.dropped}
            for sub_id, sub in self._subscribers.items()
        }


async def _await_cancelled(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await task
