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


class InputStream:
    """Wraps an `InputMapper` with the asyncio plumbing that keeps repeat
    events flowing without a consumer having to poll `tick()` itself."""

    def __init__(
        self, mapper: InputMapper, *, tick_interval_s: float = _DEFAULT_TICK_INTERVAL_S
    ) -> None:
        self._mapper = mapper
        self._tick_interval_s = tick_interval_s
        self._queue: asyncio.Queue[InputAction | TouchEvent] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

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
            await self._queue.put(event)

    async def feed_joystick(self, payload: InputJoystick) -> None:
        for event in self._mapper.on_joystick(payload):
            await self._queue.put(event)

    async def feed_touch(self, payload: InputTouch) -> None:
        await self._queue.put(self._mapper.on_touch(payload))

    async def events(self) -> AsyncIterator[InputAction | TouchEvent]:
        while True:
            yield await self._queue.get()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval_s)
            for event in self._mapper.tick():
                await self._queue.put(event)
