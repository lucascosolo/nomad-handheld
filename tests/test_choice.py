"""Asking the operator something and waiting for the answer (D13).

The defect this closes: `display_choice` drew a question and returned "asked
it". The device's one interactive primitive was write-only, so every exchange
was a monologue.
"""

from __future__ import annotations

import asyncio

import pytest

from nomad.core.config import InputConfig
from nomad.input.choice import (
    ChoiceOutcome,
    InputChoicePrompter,
    NullChoicePrompter,
)
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.protocol.messages import ButtonId, InputButton, InputJoystick, KeyPhase


class Recorder:
    """Captures what would have been drawn, including the highlight."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, list[str], int]] = []

    async def __call__(self, question: str, options: list[str], highlighted: int) -> None:
        self.frames.append((question, list(options), highlighted))


async def _prompter(timeout: float = 2.0) -> tuple[InputChoicePrompter, InputStream, Recorder]:
    stream = InputStream(InputMapper(InputConfig()))
    await stream.start()
    show = Recorder()
    return InputChoicePrompter(stream, show=show, default_timeout_s=timeout), stream, show


async def _press(stream: InputStream, button: ButtonId) -> None:
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.PRESS))
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.RELEASE))


# -- the answer actually comes back -----------------------------------------


async def test_confirming_returns_the_chosen_option() -> None:
    prompter, stream, _ = await _prompter()
    asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
    await asyncio.sleep(0.05)
    await _press(stream, ButtonId.A)  # CONFIRM

    result = await asking
    await stream.stop()
    assert result.outcome is ChoiceOutcome.ANSWERED
    assert result.option == "Yes"
    assert result.index == 0
    assert "chose 'Yes'" in result.describe()


async def test_navigating_then_confirming_returns_the_second_option() -> None:
    prompter, stream, show = await _prompter()
    asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
    await asyncio.sleep(0.05)
    await stream.feed_joystick(InputJoystick(x=0.0, y=1.0))  # NAV_DOWN
    await asyncio.sleep(0.05)
    await _press(stream, ButtonId.A)

    result = await asking
    await stream.stop()
    assert result.option == "No" and result.index == 1
    # The highlight moved on screen, not just internally.
    assert show.frames[-1][2] == 1


async def test_navigation_wraps_around() -> None:
    prompter, stream, _ = await _prompter()
    asking = asyncio.ensure_future(prompter.ask("Pick", ["A", "B", "C"]))
    await asyncio.sleep(0.05)
    await stream.feed_joystick(InputJoystick(x=0.0, y=-1.0))  # NAV_UP from 0
    await asyncio.sleep(0.05)
    await _press(stream, ButtonId.A)

    result = await asking
    await stream.stop()
    assert result.option == "C"


# -- every non-answer is a different fact -----------------------------------


async def test_back_is_a_dismissal_not_a_timeout() -> None:
    prompter, stream, _ = await _prompter()
    asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
    await asyncio.sleep(0.05)
    await _press(stream, ButtonId.B)  # BACK

    result = await asking
    await stream.stop()
    assert result.outcome is ChoiceOutcome.CANCELLED
    assert result.answered is False


async def test_nobody_answering_times_out_rather_than_hanging() -> None:
    """A handheld in a pocket is the normal case, not an error."""
    prompter, stream, _ = await _prompter(timeout=0.1)
    result = await prompter.ask("Deploy?", ["Yes", "No"])
    await stream.stop()
    assert result.outcome is ChoiceOutcome.TIMED_OUT
    assert "may not be looking" in result.describe()


async def test_no_input_hardware_is_distinguishable_from_a_timeout() -> None:
    """Retrying a timeout can work; retrying with no input device never can."""
    show = Recorder()
    result = await NullChoicePrompter(show).ask("Deploy?", ["Yes", "No"])
    assert result.outcome is ChoiceOutcome.NO_OPERATOR
    assert "no input device" in result.describe()
    assert show.frames, "the question should still be drawn"


# -- the menu half of the one-stream design ---------------------------------


async def test_holding_the_stick_does_not_race_through_the_options() -> None:
    """A menu consumes edge transitions and ignores REPEAT (D13)."""
    prompter, stream, _ = await _prompter()
    asking = asyncio.ensure_future(prompter.ask("Pick", ["A", "B", "C", "D"]))
    await asyncio.sleep(0.05)
    await stream.feed_joystick(InputJoystick(x=0.0, y=1.0))  # one PRESS
    await asyncio.sleep(0.3)  # long enough for several REPEATs to be ticked
    await _press(stream, ButtonId.A)

    result = await asking
    await stream.stop()
    assert result.option == "B", "REPEAT events moved the highlight"


async def test_an_empty_option_list_is_refused() -> None:
    prompter, stream, _ = await _prompter()
    result = await prompter.ask("Pick", [])
    await stream.stop()
    assert result.outcome is ChoiceOutcome.NO_OPERATOR


# -- the stream drops rather than growing (D6) ------------------------------


async def test_the_input_queue_drops_the_oldest_rather_than_growing() -> None:
    """A 20Hz tick with nobody reading must not grow without limit on a 4GB Pi."""
    stream = InputStream(InputMapper(InputConfig()), max_queued=4)
    for _ in range(20):
        await _press(stream, ButtonId.A)

    assert stream.dropped > 0
    assert stream._queue.qsize() <= 4  # noqa: SLF001 - the bound is the point


@pytest.mark.parametrize("button", [ButtonId.X, ButtonId.Y])
async def test_unrelated_buttons_do_not_answer_the_question(button: ButtonId) -> None:
    prompter, stream, _ = await _prompter(timeout=0.2)
    asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
    await asyncio.sleep(0.05)
    await _press(stream, button)

    result = await asking
    await stream.stop()
    assert result.outcome is ChoiceOutcome.TIMED_OUT
