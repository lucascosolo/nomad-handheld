"""The screen is never blank while a turn runs, and never stale when it ends.

`agent.event` had publishers and no subscribers before chunk F1. These tests
pin the two properties that fixing that has to deliver, which pull against
each other: a *fast* mid-turn screen (lossy, best effort) and a *correct*
end-of-turn screen (authoritative, immune to the bus dropping frames — D6).
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

import pytest

from nomad.agent.backends.base import AgentEvent, AgentEventKind
from nomad.agent.session import (
    EVENT_AGENT_EVENT,
    EVENT_TURN_FINISHED,
    EVENT_TURN_STARTED,
    TurnOutcome,
    TurnOutcomeStatus,
)
from nomad.core.errors import ConfigError
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.hardware.headless_display import DEFAULT_HISTORY_LIMIT, HeadlessDisplay
from nomad.view.renderer import TurnRenderer
from nomad.view.screen import ScreenOwner
from nomad.view.server import ScreenServer

SESSION = "s1"
TURN = "t1"


def _started(text: str = "hello") -> Event:
    return Event(
        type=EVENT_TURN_STARTED,
        source="agent_session",
        payload={"session_id": SESSION, "turn_id": TURN, "text": text},
    )


def _agent(kind: AgentEventKind, **fields: object) -> Event:
    event = AgentEvent(kind=kind, session_id=SESSION, turn_id=TURN, **fields)  # type: ignore[arg-type]
    return Event(
        type=EVENT_AGENT_EVENT, source="agent_session", payload=event.model_dump(mode="json")
    )


def _finished(outcome: TurnOutcome) -> Event:
    return Event(
        type=EVENT_TURN_FINISHED,
        source="agent_session",
        payload={"session_id": SESSION, **outcome.model_dump(mode="json")},
    )


@pytest.fixture
def screen() -> HeadlessDisplay:
    return HeadlessDisplay()


@pytest.fixture
def renderer(screen: HeadlessDisplay, event_bus: EventBus) -> TurnRenderer:
    return TurnRenderer(screen, bus=event_bus)


# -- never blank -------------------------------------------------------------


async def test_a_turn_shows_a_working_state_before_any_text_arrives(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """The whole defect, in one assertion."""
    assert screen.screen.text == ""
    await renderer.handle(_started())
    assert screen.screen.text != ""
    assert "Thinking" in screen.screen.text
    assert renderer.active_turn_id == TURN


async def test_a_tool_call_before_any_text_names_the_tool(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TOOL_CALL, tool_name="read_file"))
    assert "read_file" in screen.screen.text


async def test_streamed_text_reaches_the_display_as_it_arrives(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TEXT, text="Battery "))
    first = screen.screen.text
    await renderer.handle(_agent(AgentEventKind.TEXT, text="41%"))
    assert "Battery " in first
    assert "Battery 41%" in screen.screen.text
    # Three draws so far: the working state and one per chunk.
    assert len(screen.history) == 3


async def test_an_error_event_is_drawn_immediately(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.ERROR, error="backend went away"))
    assert "backend went away" in screen.screen.text


# -- never stale -------------------------------------------------------------


async def test_the_final_answer_is_correct_when_intermediate_events_are_dropped(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """`EventBus` drops slow subscribers by design (D6).

    So the renderer must never derive the final screen from the chunks it
    happened to receive. Here it sees the first chunk and the last, and the
    two in the middle are dropped — the finished screen is still whole.
    """
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TEXT, text="Battery "))
    # "41%, " and "~3h " dropped by the bus.
    await renderer.handle(_agent(AgentEventKind.TEXT, text="left"))
    assert screen.screen.text != "Battery 41%, ~3h left"  # what it saw is wrong

    await renderer.handle(
        _finished(
            TurnOutcome(
                turn_id=TURN,
                status=TurnOutcomeStatus.COMPLETED,
                text="Battery 41%, ~3h left",
            )
        )
    )
    assert screen.screen.text == "Battery 41%, ~3h left"
    assert renderer.active_turn_id is None


async def test_a_turn_that_produced_nothing_still_clears_the_working_state(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(
        _finished(TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.COMPLETED))
    )
    assert "Thinking" not in screen.screen.text


async def test_a_failed_turn_shows_the_error_not_the_spinner(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(
        _finished(
            TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.FAILED, error="timed out")
        )
    )
    assert "timed out" in screen.screen.text
    assert "Thinking" not in screen.screen.text


async def test_an_interrupted_turn_says_so(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(
        _finished(
            TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.INTERRUPTED, text="half an ")
        )
    )
    assert "Interrupted" in screen.screen.text
    assert "half an " in screen.screen.text


async def test_a_long_answer_is_truncated_from_the_front(
    screen: HeadlessDisplay, event_bus: EventBus
) -> None:
    """NOMAD.md: lead with the answer. On a screen that cannot scroll, the
    first line is the only one guaranteed to be read."""
    renderer = TurnRenderer(screen, bus=event_bus, max_chars=20)
    await renderer.handle(_started())
    await renderer.handle(
        _finished(
            TurnOutcome(
                turn_id=TURN, status=TurnOutcomeStatus.COMPLETED, text="A" * 10 + "B" * 90
            )
        )
    )
    assert screen.screen.text.startswith("A" * 10)
    assert "B" * 90 not in screen.screen.text
    assert "+80 chars" in screen.screen.text


# -- as a bus subscriber -----------------------------------------------------


async def test_the_renderer_subscribes_and_unsubscribes_with_its_lifecycle(
    screen: HeadlessDisplay, event_bus: EventBus
) -> None:
    renderer = TurnRenderer(screen, bus=event_bus)
    await renderer.start()
    assert renderer.state is ComponentState.STARTED

    await event_bus.publish(_started())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if screen.screen.text:
            break
    assert "Thinking" in screen.screen.text

    await renderer.stop()
    assert renderer.state is ComponentState.STOPPED
    before = len(screen.history)
    await event_bus.publish(_started())
    await asyncio.sleep(0.05)
    assert len(screen.history) == before


async def test_other_agent_events_are_ignored_rather_than_drawn(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """One subscription covers `agent.*`, so it sees mode changes too."""
    await renderer.handle(
        Event(type="agent.mode_changed", source="agent_session", payload={"to": "auto"})
    )
    assert screen.history == []


# -- the display itself ------------------------------------------------------


async def test_screen_history_is_bounded(screen: HeadlessDisplay) -> None:
    """A streaming renderer redraws several times a second, for days."""
    for index in range(DEFAULT_HISTORY_LIMIT + 25):
        await screen.show_text(str(index))
    assert len(screen.history) == DEFAULT_HISTORY_LIMIT
    assert screen.history[-1].text == str(DEFAULT_HISTORY_LIMIT + 24)


# -- serving it --------------------------------------------------------------


async def test_the_server_shows_the_current_screen(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0, refresh_seconds=2.0)
    await server.start()
    try:
        await screen.show_text("<b>not html</b>")
        body = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert "&lt;b&gt;not html&lt;/b&gt;" in body
        assert 'content="2.0"' in body

        fragment = urllib.request.urlopen(server.url + "screen", timeout=5).read().decode()
        assert fragment == screen.screen.html
    finally:
        await server.stop()


async def test_the_server_404s_an_unknown_path(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0)
    await server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(server.url + "secrets", timeout=5)
        assert excinfo.value.code == 404
    finally:
        await server.stop()


async def test_the_server_refuses_a_non_loopback_bind(screen: HeadlessDisplay) -> None:
    """Loopback is enforced here, not left to whoever edits the config."""
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        server = ScreenServer(lambda: screen.screen.html, host=host, port=0)
        with pytest.raises(ConfigError):
            await server.start()
        assert server.state is ComponentState.FAILED


async def test_the_server_accepts_the_loopback_spellings(screen: HeadlessDisplay) -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        server = ScreenServer(lambda: screen.screen.html, host=host, port=0)
        await server.start()
        try:
            assert server.state is ComponentState.STARTED
        finally:
            await server.stop()


async def test_stopping_the_server_twice_is_inert(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0)
    await server.stop()
    await server.start()
    await server.stop()
    await server.stop()
    assert server.state is ComponentState.STOPPED


# -- one screen, one writer at a time (D36) ----------------------------------


async def test_writers_share_the_screen_while_nobody_holds_it(
    screen: HeadlessDisplay,
) -> None:
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    await owner.view("renderer").show_text("a turn")
    assert screen.screen.text == "a turn"
    await owner.view("model").show_card("card", "body", [("k", "v")])
    assert "body" in screen.screen.text
    assert owner.suppressed == 0
    assert owner.holder is None


async def test_a_holder_locks_every_other_writer_out(screen: HeadlessDisplay) -> None:
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    other = owner.view("renderer")
    async with owner.exclusive("auth_prompt") as held:
        assert owner.holder == "auth_prompt"
        await held.show_text("the question")
        await other.show_text("a streamed chunk")
        await other.show_list("menu", [("a", None)])
        await other.show_choice("really?", ["yes"])
        assert screen.screen.text == "the question"
        assert owner.suppressed == 3
    assert owner.holder is None
    await other.show_text("after")
    assert screen.screen.text == "after"


async def test_a_frame_queued_behind_a_draw_loses_to_a_claim_taken_meanwhile(
    screen: HeadlessDisplay,
) -> None:
    """The re-check under the draw lock, which is the actual race D36 cares about."""
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    gate = asyncio.Event()

    class SlowDisplay:
        async def show_text(self, text: str, *, title: str | None = None) -> None:
            await gate.wait()
            await screen.show_text(text, title=title)

    owner._display = SlowDisplay()  # type: ignore[assignment]
    slow = asyncio.ensure_future(owner.view("renderer").show_text("first"))
    await asyncio.sleep(0)
    queued = asyncio.ensure_future(owner.view("renderer").show_text("second"))
    await asyncio.sleep(0)

    async with owner.exclusive("auth_prompt"):
        gate.set()
        await slow
        await queued
        assert screen.screen.text == "first"
        assert owner.suppressed == 1
