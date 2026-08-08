"""Asking the operator something and *waiting for the answer* (D13).

`display_choice` drew a question and returned "asked it" — the one interactive
primitive on the device was write-only. The model could pose a question and
then had to guess or end the turn, which makes every exchange a monologue: you
speak, it answers, done. Waiting for the answer is the difference between a
device and an oracle.

This is where the input stream meets the screen, and it is deliberately the
*only* place that happens. Everything above consumes a `ChoiceResult`.

**No cross-layer import.** The prompter takes a `show` callable rather than a
display driver, so `input` does not import `hardware` or `mcp` to do this. The
one thing it needs from the screen is "render this question with option *n*
highlighted", which is a function signature, not a dependency.

**Every non-answer is distinguishable.** `CANCELLED`, `TIMED_OUT` and
`NO_OPERATOR` are three different facts about the world, and a model told only
"no answer" would reasonably retry the one case where retrying is useless. A
handheld in a pocket with the screen off hits `TIMED_OUT` constantly; that is
normal, not an error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from nomad.core.logging import get_logger
from nomad.input.actions import ACTION_BACK, ACTION_CONFIRM, ACTION_NAV_DOWN, ACTION_NAV_UP
from nomad.input.events import ActionPhase, InputAction
from nomad.input.stream import InputStream

logger = get_logger(__name__)

#: A question nobody answers must not hold a turn open forever. A pocket
#: device whose screen is off is the normal case, not the exception.
DEFAULT_CHOICE_TIMEOUT_S = 60.0

#: `show(question, options, highlighted_index)`.
ShowChoice = Callable[[str, list[str], int], Awaitable[None]]


class ChoiceOutcome(StrEnum):
    ANSWERED = "answered"
    #: The operator pressed BACK. They saw it and declined.
    CANCELLED = "cancelled"
    #: Nobody answered in time. Usually means the device is in a pocket.
    TIMED_OUT = "timed_out"
    #: No input source is wired up, so the question could not be answered at
    #: all. Distinct from a timeout: retrying will never help.
    NO_OPERATOR = "no_operator"


@dataclass(frozen=True)
class ChoiceResult:
    outcome: ChoiceOutcome
    option: str | None = None
    index: int | None = None

    @property
    def answered(self) -> bool:
        return self.outcome is ChoiceOutcome.ANSWERED

    def describe(self) -> str:
        """What the model is told. Plain, and never pretends to an answer."""
        if self.outcome is ChoiceOutcome.ANSWERED:
            return f"operator chose '{self.option}'"
        if self.outcome is ChoiceOutcome.CANCELLED:
            return "operator dismissed the question without choosing"
        if self.outcome is ChoiceOutcome.TIMED_OUT:
            return "no answer before the prompt expired; the operator may not be looking"
        return "no input device is attached, so the question cannot be answered"


class NullChoicePrompter:
    """What you get before any input hardware exists: an honest refusal.

    It still *draws* the question — showing it is useful even when nobody can
    answer — but it reports `NO_OPERATOR` rather than inventing a choice.
    """

    def __init__(self, show: ShowChoice | None = None) -> None:
        self._show = show

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResult:
        if self._show is not None:
            await self._show(question, options, 0)
        return ChoiceResult(outcome=ChoiceOutcome.NO_OPERATOR)


class InputChoicePrompter:
    """Draws a question and consumes the action stream until it is answered.

    Navigation is edge-triggered — `PRESS` only — so holding the stick does not
    race through the options. That is the menu half of the one-stream design in
    `input/mapper.py`: this consumer simply ignores `REPEAT`.
    """

    def __init__(
        self,
        stream: InputStream,
        *,
        show: ShowChoice,
        default_timeout_s: float = DEFAULT_CHOICE_TIMEOUT_S,
    ) -> None:
        self._stream = stream
        self._show = show
        self._default_timeout_s = default_timeout_s

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResult:
        if not options:
            return ChoiceResult(outcome=ChoiceOutcome.NO_OPERATOR)

        timeout = self._default_timeout_s if timeout_s is None else timeout_s
        try:
            return await asyncio.wait_for(self._run(question, options), timeout=timeout)
        except TimeoutError:
            logger.info("Choice prompt expired", extra={"question": question})
            return ChoiceResult(outcome=ChoiceOutcome.TIMED_OUT)

    async def _run(self, question: str, options: list[str]) -> ChoiceResult:
        highlighted = 0
        await self._show(question, options, highlighted)

        async for event in self._stream.events():
            if not isinstance(event, InputAction) or event.phase is not ActionPhase.PRESS:
                # Touch events and REPEAT are not menu navigation. Ignoring
                # REPEAT here is what makes this edge-triggered.
                continue

            if event.action == ACTION_NAV_UP:
                highlighted = (highlighted - 1) % len(options)
            elif event.action == ACTION_NAV_DOWN:
                highlighted = (highlighted + 1) % len(options)
            elif event.action == ACTION_CONFIRM:
                return ChoiceResult(
                    outcome=ChoiceOutcome.ANSWERED,
                    option=options[highlighted],
                    index=highlighted,
                )
            elif event.action == ACTION_BACK:
                return ChoiceResult(outcome=ChoiceOutcome.CANCELLED)
            else:
                continue

            await self._show(question, options, highlighted)

        return ChoiceResult(outcome=ChoiceOutcome.NO_OPERATOR)  # pragma: no cover
