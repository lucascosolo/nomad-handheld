"""The button on the glass that starts a turn (D44).

The defect this closes is the plainest one on the device: with the schedule off
— or simply quiet — an operator holding Nomad had no way to say *go*. Every
turn began somewhere else, over HTTP or on a timer.

Three things have to hold for the button to exist rather than merely be drawn:
a tap has to reach something (`WakeAffordance`), the idle screen has to carry
the option a tap can land on (`TurnRenderer`), and neither may appear on a
device where it could not work (`NomadApp`).
"""

from __future__ import annotations

import asyncio

import pytest

from nomad.agent.session import (
    EVENT_TURN_FINISHED,
    EVENT_TURN_STARTED,
    TurnOutcomeStatus,
)
from nomad.core.config import InputConfig
from nomad.core.events import Event, EventBus
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.input.broker import InputBroker
from nomad.input.events import ActionPhase, InputAction, InputSource, TouchEvent
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.input.wake import WAKE_LABEL, WakeAffordance
from nomad.protocol.messages import InputChoice, TouchPhase
from nomad.view.renderer import DEFAULT_MAX_WAKE_CHARS, TurnRenderer


class Fire:
    """Stands in for `SelfImproveTrigger.fire_now`: says whether it started."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.accepts


def _tap(option: str = WAKE_LABEL, index: int = 0) -> object:
    from nomad.input.events import ChoiceSelection

    return ChoiceSelection(index=index, option=option, ts=0.0)


# -- the tap reaches something ---------------------------------------------


async def test_tapping_wake_starts_a_turn() -> None:
    fire = Fire()
    wake = WakeAffordance(fire)
    await wake(_tap())  # type: ignore[arg-type]
    assert fire.calls == 1
    assert wake.woke == 1


async def test_a_tap_while_a_turn_is_running_is_counted_not_lost() -> None:
    """`fire_now` refusing is not the button failing. The two look identical
    from outside, which is why both are counted."""
    fire = Fire(accepts=False)
    wake = WakeAffordance(fire)
    await wake(_tap())  # type: ignore[arg-type]
    assert wake.woke == 0
    assert wake.ignored == 1


async def test_a_choice_with_another_label_does_not_wake_him() -> None:
    """A stale answer to a question that has gone must not start a turn. The
    label is the whole check — an index would match option 0 of anything."""
    fire = Fire()
    wake = WakeAffordance(fire)
    await wake(_tap(option="Approve"))  # type: ignore[arg-type]
    await wake(_tap(option=""))  # type: ignore[arg-type]
    assert fire.calls == 0


async def test_raw_touch_and_buttons_never_wake_him() -> None:
    """Any-tap-wakes is one pocket brush away from spending money, and the
    panel reports raw touch on every screen."""
    fire = Fire()
    wake = WakeAffordance(fire)
    await wake(TouchEvent(x=10, y=10, phase=TouchPhase.DOWN, ts=0.0))
    await wake(
        InputAction(action="confirm", phase=ActionPhase.PRESS, ts=0.0, source=InputSource.BUTTON)
    )
    assert fire.calls == 0


async def test_a_real_tap_off_the_panel_reaches_the_button() -> None:
    """End to end over the input layer: an `input.choice` frame the panel would
    send, through the stream and the broker, into a started turn."""
    stream = InputStream(InputMapper(InputConfig()))
    await stream.start()
    fire = Fire()
    broker = InputBroker(stream, idle=WakeAffordance(fire))
    await broker.start()
    try:
        await stream.feed_choice(InputChoice(index=0, option=WAKE_LABEL))
        for _ in range(200):
            if fire.calls:
                break
            await asyncio.sleep(0.01)
        assert fire.calls == 1
    finally:
        await broker.stop()
        await stream.stop()


# -- the screen carries the option -----------------------------------------


@pytest.fixture
def display() -> HeadlessDisplay:
    return HeadlessDisplay()


async def _renderer(display: HeadlessDisplay, *, wake: str | None) -> tuple[TurnRenderer, EventBus]:
    bus = EventBus()
    await bus.start()
    renderer = TurnRenderer(display, bus=bus, wake_label=wake)  # type: ignore[arg-type]
    await renderer.start()
    return renderer, bus


async def _finish(renderer: TurnRenderer, *, status: str, text: str = "", error: str = "") -> None:
    await renderer.handle(
        Event(
            type=EVENT_TURN_FINISHED,
            source="test",
            payload={"turn_id": "t1", "status": status, "text": text, "error": error},
        )
    )


async def test_the_idle_screen_carries_the_wake_option(display: HeadlessDisplay) -> None:
    renderer, bus = await _renderer(display, wake=WAKE_LABEL)
    try:
        assert WAKE_LABEL in display.screen.text
        assert "idle" in display.screen.text
    finally:
        await renderer.stop()
        await bus.stop()


async def test_the_answer_screen_carries_it_too(display: HeadlessDisplay) -> None:
    """Nothing redraws an ending, so a button only on the boot screen is a
    button the operator sees once."""
    renderer, bus = await _renderer(display, wake=WAKE_LABEL)
    try:
        await _finish(renderer, status=TurnOutcomeStatus.COMPLETED, text="done")
        assert "done" in display.screen.text
        assert WAKE_LABEL in display.screen.text
    finally:
        await renderer.stop()
        await bus.stop()


async def test_a_failed_turn_still_offers_the_button(display: HeadlessDisplay) -> None:
    """The state the operator most wants to poke is the one that just failed."""
    renderer, bus = await _renderer(display, wake=WAKE_LABEL)
    try:
        await _finish(renderer, status=TurnOutcomeStatus.FAILED, error="backend is logged out")
        assert "backend is logged out" in display.screen.text
        # The title a text screen would have carried moves into the question,
        # or an error reads as an answer.
        assert "Error" in display.screen.text
        assert WAKE_LABEL in display.screen.text
    finally:
        await renderer.stop()
        await bus.stop()


async def test_a_long_answer_cannot_push_the_button_off_the_screen(
    display: HeadlessDisplay,
) -> None:
    """The panel draws the options *below* the wrapped question and skips the
    ones that no longer fit."""
    renderer, bus = await _renderer(display, wake=WAKE_LABEL)
    try:
        await _finish(renderer, status=TurnOutcomeStatus.COMPLETED, text="x" * 5000)
        question = display.screen.text
        assert WAKE_LABEL in question
        assert question.count("x") <= DEFAULT_MAX_WAKE_CHARS
    finally:
        await renderer.stop()
        await bus.stop()


async def test_a_turn_in_flight_draws_no_button(display: HeadlessDisplay) -> None:
    """`fire_now` refuses while the session is busy, and a control that is
    drawn and refuses is worse than one that is absent."""
    renderer, bus = await _renderer(display, wake=WAKE_LABEL)
    try:
        await renderer.handle(
            Event(type=EVENT_TURN_STARTED, source="test", payload={"turn_id": "t1"})
        )
        assert WAKE_LABEL not in display.screen.text
    finally:
        await renderer.stop()
        await bus.stop()


async def test_with_no_wake_label_the_screens_are_unchanged(display: HeadlessDisplay) -> None:
    """The whole feature is off on a device that cannot use it, and off means
    the plain text screens F1 shipped."""
    renderer, bus = await _renderer(display, wake=None)
    try:
        assert "idle" in display.screen.text
        assert WAKE_LABEL not in display.screen.text
        await _finish(renderer, status=TurnOutcomeStatus.COMPLETED, text="done")
        assert display.screen.text.strip() == "done"
    finally:
        await renderer.stop()
        await bus.stop()
