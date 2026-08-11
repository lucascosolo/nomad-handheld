"""One reader of the input stream, lent out one question at a time (D44).

The defect this closes has two halves. Nothing could react to a press while no
question was up — so a wake button had nowhere to live — and a press made in
that gap was not discarded but *queued*, and delivered to the next question as
though it had been made in answer to it.
"""

from __future__ import annotations

import asyncio

from nomad.core.config import InputConfig
from nomad.input.broker import InputBroker, InputEvent
from nomad.input.choice import ChoiceOutcome, InputChoicePrompter
from nomad.input.events import ChoiceSelection, InputAction
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.protocol.messages import ButtonId, InputButton, InputChoice, KeyPhase


class Recorder:
    def __init__(self) -> None:
        self.frames: list[tuple[str, list[str], int]] = []

    async def __call__(self, question: str, options: list[str], highlighted: int) -> None:
        self.frames.append((question, list(options), highlighted))


class Collector:
    """An idle handler that just remembers what it was given."""

    def __init__(self) -> None:
        self.seen: list[InputEvent] = []

    async def __call__(self, event: InputEvent) -> None:
        self.seen.append(event)


async def _wired(
    idle: object | None = None,
) -> tuple[InputBroker, InputStream]:
    stream = InputStream(InputMapper(InputConfig()))
    await stream.start()
    broker = InputBroker(stream, idle=idle)  # type: ignore[arg-type]
    await broker.start()
    return broker, stream


async def _press(stream: InputStream, button: ButtonId = ButtonId.A) -> None:
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.PRESS))
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.RELEASE))


async def _settle(check: object, tries: int = 200) -> None:
    for _ in range(tries):
        if check():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the broker never reached the expected state")


# -- the idle half ---------------------------------------------------------


async def test_a_press_with_no_question_up_reaches_the_idle_handler() -> None:
    """The whole reason a wake button can exist: somebody is listening between
    questions."""
    idle = Collector()
    broker, stream = await _wired(idle)
    try:
        await _press(stream)
        await _settle(lambda: len(idle.seen) >= 1)
        assert any(isinstance(event, InputAction) for event in idle.seen)
    finally:
        await broker.stop()
        await stream.stop()


async def test_a_tap_on_an_option_reaches_the_idle_handler_too() -> None:
    """The poke button is drawn as a one-option choice, so the event that
    arrives is a `ChoiceSelection`, not a button."""
    idle = Collector()
    broker, stream = await _wired(idle)
    try:
        await stream.feed_choice(InputChoice(index=0, option="Poke"))
        await _settle(lambda: len(idle.seen) >= 1)
        selection = idle.seen[0]
        assert isinstance(selection, ChoiceSelection)
        assert selection.option == "Poke"
    finally:
        await broker.stop()
        await stream.stop()


async def test_an_idle_handler_that_raises_does_not_kill_the_reader() -> None:
    calls = 0

    async def angry(event: InputEvent) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("no")

    broker, stream = await _wired(angry)
    try:
        await _press(stream)
        await _settle(lambda: calls >= 1)
        await _press(stream)
        # Still reading. A reader that died on one bad handler would take
        # every future press with it.
        await _settle(lambda: calls >= 2)
    finally:
        await broker.stop()
        await stream.stop()


async def test_with_no_idle_handler_presses_are_counted_not_lost_silently() -> None:
    broker, stream = await _wired(None)
    try:
        await _press(stream)
        await _settle(lambda: broker.unclaimed >= 1)
    finally:
        await broker.stop()
        await stream.stop()


# -- lending it out --------------------------------------------------------


async def test_while_a_question_holds_the_stream_the_idle_handler_sees_nothing() -> None:
    idle = Collector()
    broker, stream = await _wired(idle)
    show = Recorder()
    prompter = InputChoicePrompter(broker, show=show, default_timeout_s=2.0)
    try:
        asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
        await _settle(lambda: broker.lent)
        await _press(stream)  # CONFIRM
        result = await asking
        assert result.outcome is ChoiceOutcome.ANSWERED
        assert result.option == "Yes"
        # The press answered the question. It must not also have poked Nomad.
        assert idle.seen == []
    finally:
        await broker.stop()
        await stream.stop()


async def test_the_stream_goes_back_to_idle_when_the_question_ends() -> None:
    idle = Collector()
    broker, stream = await _wired(idle)
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Yes", "No"]))
        await _settle(lambda: broker.lent)
        await _press(stream)
        await asking
        await _settle(lambda: not broker.lent)

        await _press(stream)
        await _settle(lambda: len(idle.seen) >= 1)
    finally:
        await broker.stop()
        await stream.stop()


async def test_a_press_made_while_idle_cannot_answer_the_next_question() -> None:
    """The security half. Before the broker, an idle press queued and was
    handed to whatever question opened next — for an authorization prompt,
    the difference between approving what the operator read and approving
    what arrived while they were away."""
    idle = Collector()
    broker, stream = await _wired(idle)
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=0.3)
    try:
        # A finger moves long before anything is asked.
        await _press(stream)
        await _settle(lambda: len(idle.seen) >= 1)

        result = await prompter.ask("Run rm -rf /?", ["Approve", "Deny"])
        # Nobody answered *this* question, so it expired. It was not approved.
        assert result.outcome is ChoiceOutcome.TIMED_OUT
    finally:
        await broker.stop()
        await stream.stop()


async def test_a_second_concurrent_question_is_refused_rather_than_racing() -> None:
    """Two questions sharing one finger is not something to arbitrate — it is
    a bug above, and the safe reading of a bug is no answer."""
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        first = asyncio.ensure_future(prompter.ask("First?", ["Yes", "No"]))
        await _settle(lambda: broker.lent)

        second = await prompter.ask("Second?", ["Yes", "No"])
        assert second.outcome is ChoiceOutcome.NO_OPERATOR

        await _press(stream)
        assert (await first).outcome is ChoiceOutcome.ANSWERED
    finally:
        await broker.stop()
        await stream.stop()


async def test_the_idle_handler_can_be_wired_after_construction() -> None:
    """The composition root builds the broker before the thing that reacts to
    an idle press, because that thing needs the prompter's screen."""
    broker, stream = await _wired(None)
    idle = Collector()
    try:
        broker.set_idle_handler(idle)
        await _press(stream)
        await _settle(lambda: len(idle.seen) >= 1)
    finally:
        await broker.stop()
        await stream.stop()


async def test_stop_is_safe_before_start() -> None:
    stream = InputStream(InputMapper(InputConfig()))
    await InputBroker(stream).stop()


# -- D47: answering the panel's question from a browser --------------------


async def test_the_browser_can_answer_the_question_on_the_panel() -> None:
    """The operator reads the prompt on a phone and taps Approve. Before this,
    the page rendered the question and could not resolve it."""
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Deny", "Approve"]))
        await _settle(lambda: prompter.pending is not None)
        pending = prompter.pending
        assert pending is not None
        assert await prompter.answer(pending.token, 1)
        result = await asking
        assert result.outcome is ChoiceOutcome.ANSWERED
        assert result.option == "Approve"
    finally:
        await broker.stop()
        await stream.stop()


async def test_a_stale_click_cannot_answer_the_question_that_replaced_it() -> None:
    """The reason this does not go through `feed_choice`. Every authorization
    prompt draws the same three labels, so an index and a label alone cannot
    tell this question from the one the page was showing a moment ago."""
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        first = asyncio.ensure_future(prompter.ask("Run rm -rf /?", ["Deny", "Approve"]))
        await _settle(lambda: prompter.pending is not None)
        stale = prompter.pending
        assert stale is not None
        await _press(stream, ButtonId.B)  # BACK — the operator denied it
        assert (await first).outcome is ChoiceOutcome.CANCELLED

        second = asyncio.ensure_future(prompter.ask("Send email?", ["Deny", "Approve"]))
        await _settle(lambda: prompter.pending is not None)
        fresh = prompter.pending
        assert fresh is not None and fresh.token != stale.token
        # The click the operator made on the *previous* page.
        assert not await prompter.answer(stale.token, 1)

        await _press(stream, ButtonId.B)
        assert (await second).outcome is ChoiceOutcome.CANCELLED
    finally:
        await broker.stop()
        await stream.stop()


async def test_dismissing_from_the_browser_cancels_which_denies() -> None:
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Deny", "Approve"]))
        await _settle(lambda: prompter.pending is not None)
        pending = prompter.pending
        assert pending is not None
        assert await prompter.cancel(pending.token)
        assert (await asking).outcome is ChoiceOutcome.CANCELLED
    finally:
        await broker.stop()
        await stream.stop()


async def test_answering_when_no_question_is_up_is_false_not_an_error() -> None:
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=2.0)
    try:
        assert prompter.pending is None
        assert not await prompter.answer("whatever", 0)
        assert not await prompter.cancel("whatever")
    finally:
        await broker.stop()
        await stream.stop()


async def test_an_index_off_the_end_answers_nothing() -> None:
    broker, stream = await _wired(Collector())
    prompter = InputChoicePrompter(broker, show=Recorder(), default_timeout_s=0.5)
    try:
        asking = asyncio.ensure_future(prompter.ask("Deploy?", ["Deny", "Approve"]))
        await _settle(lambda: prompter.pending is not None)
        pending = prompter.pending
        assert pending is not None
        assert not await prompter.answer(pending.token, 7)
        assert (await asking).outcome is ChoiceOutcome.TIMED_OUT
    finally:
        await broker.stop()
        await stream.stop()
