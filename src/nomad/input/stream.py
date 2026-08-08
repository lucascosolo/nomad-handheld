"""Async delivery of the mapped event stream, `EventBus`/`Link.messages()`
shaped: `feed_*` pushes onto an internal queue as raw payloads arrive, a
background task calls `InputMapper.tick()` on a fixed cadence so held
controls keep repeating between payloads, and `events()` is an async
generator a consumer iterates exactly like `Link.messages()`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from nomad.input.events import InputAction, TouchEvent
from nomad.input.mapper import InputMapper
from nomad.protocol.messages import InputButton, InputJoystick, InputTouch

_DEFAULT_TICK_INTERVAL_S = 0.05
#: Bounded, and it drops rather than blocking — the same trade D6 makes for
#: `EventBus`, for the same reason. A 20 Hz tick loop with a held button and
#: nobody reading would otherwise grow without limit on a 4 GB Pi, and stale
#: input is worthless anyway: nobody wants the joystick moves they made a
#: minute ago replayed when a menu finally opens.
_DEFAULT_MAX_QUEUED = 256


class InputStream:
    """Wraps an `InputMapper` with the asyncio plumbing that keeps repeat
    events flowing without a consumer having to poll `tick()` itself."""

    #: It already had `start()`/`stop()`; this is what makes it satisfy
    #: `core.lifecycle.Component` so the composition root can own its tick
    #: loop rather than leaking one background task.
    name = "input_stream"

    def __init__(
        self,
        mapper: InputMapper,
        *,
        tick_interval_s: float = _DEFAULT_TICK_INTERVAL_S,
        max_queued: int = _DEFAULT_MAX_QUEUED,
    ) -> None:
        self._mapper = mapper
        self._tick_interval_s = tick_interval_s
        self._queue: asyncio.Queue[InputAction | TouchEvent] = asyncio.Queue(maxsize=max_queued)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._tick_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def feed_button(self, payload: InputButton) -> None:
        for event in self._mapper.on_button(payload):
            self._emit(event)

    async def feed_joystick(self, payload: InputJoystick) -> None:
        for event in self._mapper.on_joystick(payload):
            self._emit(event)

    async def feed_touch(self, payload: InputTouch) -> None:
        self._emit(self._mapper.on_touch(payload))

    def _emit(self, event: InputAction | TouchEvent) -> None:
        """Queue an event, dropping the oldest if nobody is keeping up (D6).

        Dropping the *oldest* rather than the newest is deliberate: when a menu
        finally starts reading, the operator's most recent press is the one
        they still mean.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    async def events(self) -> AsyncIterator[InputAction | TouchEvent]:
        while True:
            yield await self._queue.get()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval_s)
            for event in self._mapper.tick():
                self._emit(event)
