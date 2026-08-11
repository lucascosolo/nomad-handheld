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

#: What the glass says when no turn is running. Drawn once at startup, and it
#: matters more than its size suggests: a panel holds the last pixels it was
#: sent until it is sent different ones, and nothing here used to send any at
#: boot. So a restart left the previous turn's final frame on the screen —
#: after a crash, a frame from a turn that no longer existed, indefinitely.
#: `PanelKeeper` could not heal it either, because it repaints the last write
#: and there had not been one.
#:
#: A screen that is merely blank is honest. A screen mid-sentence about work
#: that died an hour ago is not, and NOMAD.md's one-second test fails worst
#: exactly when the device has just come back from something going wrong.
_IDLE_TEXT = "idle"
_IDLE_TITLE = "Nomad"

#: How much of a settled answer reaches the glass **when the wake button is
#: drawn under it** (D44). Much shorter than `DEFAULT_MAX_CHARS`, and not for
#: taste: the panel wraps a choice screen's question and then draws the options
#: below it, skipping any that no longer fit. A 600-character answer pushes the
#: button off the bottom of the screen, which is exactly the state — a long
#: answer, nothing happening — where the operator most wants something to tap.
DEFAULT_MAX_WAKE_CHARS = 240

#: How much of a tool's own argument reaches the glass. Long enough for a real
#: command or a path to be recognisable, short enough that it cannot push the
#: title off a screen that does not scroll.
DEFAULT_MAX_DETAIL_CHARS = 120

#: Which argument actually says what a call is *doing*, per tool. A tool not
#: listed here falls back to the single most path-like argument it has, which
#: is right far more often than picking the first key of a dict.
#:
#: This exists because the alternative shipped for a while and was wrong in a
#: way only the operator could see: a long `Bash` call rendered as the title
#: "Running Bash" over a bare ellipsis and stayed that way for minutes.
#: NOMAD.md's promise is that one second of looking tells you whether the
#: device is working, waiting or idle, and an ellipsis satisfies none of it.
_TOOL_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Task": ("description",),
    "Skill": ("command", "name"),
}

#: Tried in order for a tool with no entry above.
_GENERIC_DETAIL_KEYS = ("file_path", "path", "command", "pattern", "query", "url", "name")


def _tool_detail(
    tool: str | None,
    tool_input: object,
    *,
    max_chars: int = DEFAULT_MAX_DETAIL_CHARS,
) -> str:
    """One line saying what this call is doing, or `""` if nothing is worth it.

    Defensive about its input on purpose: `tool_input` arrives as a JSON
    payload off the bus, so it may be absent, not a dict, or hold a value that
    is not a string. A renderer is the last place that should raise — the
    screen going blank because a tool passed a list is a worse failure than
    showing nothing for this one call.

    Whitespace is collapsed because a multi-line heredoc in a `Bash` command
    would otherwise eat the whole screen with its own newlines and leave the
    interesting first token off the top.
    """
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    keys = _TOOL_DETAIL_KEYS.get(tool or "", _GENERIC_DETAIL_KEYS)
    for key in keys:
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        detail = " ".join(value.split())
        if not detail:
            continue
        if len(detail) > max_chars:
            return detail[: max_chars - 1].rstrip() + "…"
        return detail
    return ""


class TurnRenderer:
    """Draws the turn in flight, and then the answer, onto a `DisplayDriver`."""

    name = "view_renderer"

    def __init__(
        self,
        display: DisplayDriver,
        *,
        bus: EventBus,
        max_chars: int = DEFAULT_MAX_CHARS,
        wake_label: str | None = None,
    ) -> None:
        self._display = display
        self._bus = bus
        self._max_chars = max_chars
        #: The one option drawn under a settled screen, or `None` for a plain
        #: text screen. The composition root passes a label only when a tap on
        #: it would actually do something — there has to be a trigger to fire
        #: and a panel able to report the tap (D44). A control that draws and
        #: cannot work is worse than no control.
        self._wake_label = wake_label
        self._state = ComponentState.NEW
        self._unsubscribe: Unsubscribe | None = None
        self._turn_id: str | None = None
        self._chunks: list[str] = []
        self._tool: str | None = None
        self._tool_detail: str = ""

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
        await self._draw_idle()
        self._state = ComponentState.STARTED

    async def _draw_idle(self) -> None:
        """Claim the screen at boot so it cannot keep a dead turn's frame.

        Failure is swallowed. A display that is not answering yet must not
        stop the renderer from subscribing — the subscription is the thing
        that makes every later frame possible, and `PanelKeeper` will repaint
        on its next tick anyway.
        """
        try:
            await self._settle(_IDLE_TEXT, title=_IDLE_TITLE)
        except Exception:  # noqa: BLE001 - a screen must never block startup
            logger.warning("Could not draw the idle screen at startup", exc_info=True)

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
        self._tool_detail = ""
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
            self._tool_detail = _tool_detail(self._tool, payload.get("tool_input"))
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
        self._tool_detail = ""
        status = str(payload.get("status", ""))
        text = str(payload.get("text") or "")
        error = payload.get("error")

        if status == TurnOutcomeStatus.COMPLETED:
            await self._settle(self._head(text) if text else "(no reply)")
            return
        if status == TurnOutcomeStatus.INTERRUPTED:
            await self._settle(self._head(text) if text else "(interrupted)", title="Interrupted")
            return
        # Failed, or a status this renderer does not know: say so rather than
        # leaving whatever happened to be on the glass.
        await self._settle(str(error or "the turn failed"), title="Error")

    # -- drawing -----------------------------------------------------------

    async def _settle(self, text: str, *, title: str | None = None) -> None:
        """A frame drawn when nothing is running — and so the one that carries
        the wake button (D44).

        Every screen this device shows while idle goes through here: the boot
        frame and all three endings. That is the point. A button that appeared
        after a completed turn but not after a failed one would be missing
        precisely when the operator wants to poke the device, and nothing
        redraws an ending — whatever this leaves on the glass stays there until
        the next turn.

        With no wake label it is `show_text`, unchanged. With one, the same
        words become the question of a one-option `choice`, because that is the
        screen shape the panel already reports taps on. The title moves into
        the question: a choice screen's chrome says `Nomad`, so `Error` would
        otherwise be dropped silently — and an error whose only marker was the
        title reads as an answer once the title is gone.

        Mid-turn frames deliberately do not come through here. `fire_now()`
        refuses while the session is busy, and a button that is drawn and
        refuses is worse than one that is absent.
        """
        if self._wake_label is None:
            await self._display.show_text(text, title=title)
            return
        # `Nomad` is the chrome a choice screen already draws (D30), so folding
        # it in would render the idle screen as "Nomad — idle" under the word
        # Nomad.
        prefix = title if title and title != _IDLE_TITLE else None
        question = f"{prefix} — {text}" if prefix else text
        if len(question) > DEFAULT_MAX_WAKE_CHARS:
            question = question[: DEFAULT_MAX_WAKE_CHARS - 1].rstrip() + "…"
        await self._display.show_choice(question, [self._wake_label])

    async def _draw_partial(self) -> None:
        """Mid-turn frame. Lossy on purpose — `turn_finished` corrects it."""
        text = "".join(self._chunks)
        if not text:
            label = f"Running {self._tool}" if self._tool else _THINKING_TITLE
            # The tool's own argument, when there is one. Falling back to the
            # ellipsis is still right for `Thinking` and for a tool that takes
            # nothing worth showing — the point is that a body exists whenever
            # there is something true to put in it.
            await self._display.show_text(self._tool_detail or _WORKING, title=label)
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
