"""The default backend: no subprocess, no network, no credentials (D9, D24).

`mock` is the default in `nomad.toml` on purpose. The whole system — session
lifecycle, permission bridge, MCP hardware, storage, API — must run and be
testable on a laptop with no Claude CLI and no OAuth token. A test suite that
needs credentials is a test suite that stops being run.

It declares the same capabilities as `claude_cli` so the session takes the
same code path in tests as on the device. A mock that exercises a different
branch than production is worse than no mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

from nomad.agent.backends.base import (
    AgentEvent,
    AgentEventKind,
    BackendCapability,
)
from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: A turn plan: given the prompt, produce the events that turn emits. The
#: `session_id` is stamped on by the backend, so a plan never has to know it.
TurnPlanner = Callable[[str], Sequence[AgentEvent]]


def _echo_plan(text: str) -> Sequence[AgentEvent]:
    return [
        AgentEvent(kind=AgentEventKind.TEXT, session_id="", text=f"mock: {text}"),
        AgentEvent(kind=AgentEventKind.TURN_COMPLETE, session_id=""),
    ]


class MockBackend:
    """A scripted backend. Every unhappy path a real backend has, on demand."""

    name = "mock"
    capabilities: frozenset[BackendCapability] = frozenset(
        {
            BackendCapability.OWN_LOOP,
            BackendCapability.OWN_TOOLS,
            BackendCapability.OWN_COMPACTION,
        }
    )

    def __init__(
        self,
        *,
        script: Sequence[Sequence[AgentEvent]] | None = None,
        planner: TurnPlanner | None = None,
    ) -> None:
        """`script` replays one entry per `send()`; `planner` computes each turn.

        `script` running out falls back to the echo plan rather than raising —
        a test that sends one more message than it scripted should fail on its
        own assertion, not on backend plumbing.
        """
        self._script = [list(turn) for turn in (script or [])]
        self._planner = planner or _echo_plan
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._started = False
        self._stopped = False
        self.sent: list[str] = []
        self.interrupts = 0

    async def start(self) -> None:
        self._started = True
        self._stopped = False

    async def stop(self) -> None:
        self._stopped = True
        self._started = False
        await self._queue.put(None)

    async def send(self, text: str, *, session_id: str) -> None:
        if not self._started:
            raise RuntimeError("MockBackend.send() before start()")
        self.sent.append(text)
        plan = self._script.pop(0) if self._script else list(self._planner(text))
        for event in plan:
            await self._queue.put(event.model_copy(update={"session_id": session_id}))

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        self.interrupts += 1
        # Drop anything the interrupted turn had queued but not yet emitted —
        # the point of an interrupt is that its remaining output is unwanted.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race with a consumer
                break
