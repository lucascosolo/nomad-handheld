"""The screen, driven by the session (D6, D11).

`agent.event` had publishers and no subscribers, which is the whole of why a
stock device showed nothing between a keypress and the end of a turn: unless
the model chose to call `display_card` on itself, Nomad's face stayed blank
while it worked. This module is the missing subscriber.

Two things it must get right, and they pull in different directions:

* **Never blank while a turn is running.** A working state goes up on
  `agent.turn_started`, before the backend has emitted anything, and text is
  drawn as it streams.
* **Never a stale final answer.** The bus drops slow subscribers rather than
  applying backpressure (D6), so the chunks this renderer accumulated may be
  incomplete. `agent.turn_finished` carries the whole answer and is the only
  thing treated as final — a dropped frame costs a flicker, never a wrong
  screen.

It holds a `DisplayDriver`, never a concrete driver, so the same renderer
drives the headless screen on a laptop and the ESP32 on the device.
"""

from __future__ import annotations

from typing import Any

from nomad.agent.backends.base import AgentEventKind
from nomad.agent.session import (
    EVENT_AGENT_EVENT,
    EVENT_TURN_FINISHED,
    EVENT_TURN_STARTED,
    TurnOutcomeStatus,
)
from nomad.core.events import Event, EventBus, Unsubscribe
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.mcp.hardware import DisplayDriver

logger = get_logger(__name__)

#: One subscription, not three. Separate subscriptions get separate queues and
#: therefore no ordering between them — `turn_finished` could be drawn before
#: the last text chunk it supersedes. One queue keeps the sequence the session
#: published.
AGENT_EVENT_PATTERN = "agent.*"

#: How much of a long answer reaches the glass. NOMAD.md's contract is that
#: answers are three to five short lines; this is the backstop for when they
#: are not, so a wall of prose does not push the first line off a screen that
#: cannot scroll.
DEFAULT_MAX_CHARS = 600

_WORKING = "…"
_THINKING_TITLE = "Thinking"


class TurnRenderer:
    """Draws the turn in flight, and then the answer, onto a `DisplayDriver`."""

    name = "view_renderer"

    def __init__(
        self,
        display: DisplayDriver,
        *,
        bus: EventBus,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._display = display
        self._bus = bus
        self._max_chars = max_chars
        self._state = ComponentState.NEW
        self._unsubscribe: Unsubscribe | None = None
        self._turn_id: str | None = None
        self._chunks: list[str] = []
        self._tool: str | None = None

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def active_turn_id(self) -> str | None:
        """The turn currently being drawn, if any. For tests and for F2."""
        return self._turn_id

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._unsubscribe = self._bus.subscribe(AGENT_EVENT_PATTERN, self.handle)
        self._state = ComponentState.STARTED

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._state = ComponentState.STOPPED

    # -- the subscriber ----------------------------------------------------

    async def handle(self, event: Event) -> None:
        """Dispatch one bus event. Unknown `agent.*` types are ignored."""
        if event.type == EVENT_TURN_STARTED:
            await self._on_turn_started(event.payload)
        elif event.type == EVENT_AGENT_EVENT:
            await self._on_agent_event(event.payload)
        elif event.type == EVENT_TURN_FINISHED:
            await self._on_turn_finished(event.payload)

    async def _on_turn_started(self, payload: dict[str, Any]) -> None:
        self._turn_id = str(payload.get("turn_id") or "") or None
        self._chunks = []
        self._tool = None
        await self._display.show_text(_WORKING, title=_THINKING_TITLE)

    async def _on_agent_event(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("kind", ""))

        if kind == AgentEventKind.TEXT:
            chunk = str(payload.get("text", ""))
            if not chunk:
                return
            self._chunks.append(chunk)
            await self._draw_partial()
        elif kind == AgentEventKind.TOOL_CALL:
            self._tool = payload.get("tool_name") or None
            await self._draw_partial()
        elif kind == AgentEventKind.THINKING and not self._chunks:
            await self._display.show_text(_WORKING, title=_THINKING_TITLE)
        elif kind == AgentEventKind.ERROR:
            # Drawn immediately rather than waiting for `turn_finished`: an
            # error is the answer, and the operator should not watch a spinner
            # for the rest of a turn that has already lost.
            await self._display.show_text(
                str(payload.get("error") or "the turn failed"), title="Error"
            )

    async def _on_turn_finished(self, payload: dict[str, Any]) -> None:
        """The authoritative end-of-turn draw. Never derived from the chunks."""
        self._turn_id = None
        self._tool = None
        status = str(payload.get("status", ""))
        text = str(payload.get("text") or "")
        error = payload.get("error")

        if status == TurnOutcomeStatus.COMPLETED:
            await self._display.show_text(self._head(text) if text else "(no reply)")
            return
        if status == TurnOutcomeStatus.INTERRUPTED:
            await self._display.show_text(
                self._head(text) if text else "(interrupted)", title="Interrupted"
            )
            return
        # Failed, or a status this renderer does not know: say so rather than
        # leaving whatever happened to be on the glass.
        await self._display.show_text(str(error or "the turn failed"), title="Error")

    # -- drawing -----------------------------------------------------------

    async def _draw_partial(self) -> None:
        """Mid-turn frame. Lossy on purpose — `turn_finished` corrects it."""
        text = "".join(self._chunks)
        if not text:
            label = f"Running {self._tool}" if self._tool else _THINKING_TITLE
            await self._display.show_text(_WORKING, title=label)
            return
        await self._display.show_text(self._tail(text) + _WORKING)

    def _head(self, text: str) -> str:
        """The start of a finished answer: the first line is the useful one."""
        if len(text) <= self._max_chars:
            return text
        return text[: self._max_chars].rstrip() + f"… (+{len(text) - self._max_chars} chars)"

    def _tail(self, text: str) -> str:
        """The end of a streaming answer, so it reads like it is being typed."""
        if len(text) <= self._max_chars:
            return text
        return "…" + text[-self._max_chars :]
