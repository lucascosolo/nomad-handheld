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
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from nomad.core.logging import get_logger
from nomad.input.actions import ACTION_BACK, ACTION_CONFIRM, ACTION_NAV_DOWN, ACTION_NAV_UP
from nomad.input.events import ActionPhase, ChoiceSelection, InputAction
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


@dataclass(frozen=True)
class PendingQuestion:
    """One unanswered question, as an *immutable* snapshot.

    Frozen and rebound as a whole rather than mutated in place, because the
    reader is on another thread: `ScreenServer` serves the browser from
    `http.server`'s pool while the prompter lives on the event loop. A reader
    therefore sees the previous question or the next one, never half of one —
    the same trick `HeadlessDisplay` uses for the screen itself.
    """

    #: Unguessable, and regenerated per question. An answer names the question
    #: it was given, so a click on a stale page cannot resolve the prompt that
    #: replaced it — which for an authorization prompt is the difference
    #: between approving what you read and approving what arrived while you
    #: were reading. It is *not* a session token and is not a substitute for
    #: authentication: it scopes an answer, it does not identify an operator.
    token: str
    question: str
    options: tuple[str, ...]

    @classmethod
    def create(cls, question: str, options: list[str]) -> PendingQuestion:
        return cls(token=secrets.token_urlsafe(16), question=question, options=tuple(options))


@dataclass
class _Waiter:
    question: PendingQuestion
    future: asyncio.Future[ChoiceResult] = field(default_factory=asyncio.Future)


class ExternalChoicePrompter:
    """Answered from somewhere that is not the input stream — today, a browser.

    **Why this exists rather than a second feeder into `InputStream`.** The
    obvious alternative was to have the browser post `NAV_UP`/`CONFIRM` and
    push them through `InputStream.feed_button`, reusing `InputChoicePrompter`
    wholesale. It was rejected for two reasons and neither is style:

    * The highlight would live in `InputChoicePrompter`'s generator and the
      browser would only learn of it by re-reading the drawn screen, so an
      answer would be three round trips against a screen refreshing on a
      timer — and the operator would be confirming whatever the highlight
      happened to be, not the option they clicked. Here the answer names an
      option *and* the question it belongs to (`token`), so a click cannot
      resolve something the operator never read.
    * `InputStream.events()` is single-consumer by contract (`app.py`). Feeding
      it from HTTP keeps that literally true but makes it useless in practice:
      once the panel exists, a browser press and a joystick press become
      indistinguishable and two operators drive one highlight. This class
      touches the stream not at all, so the invariant holds by construction —
      in a headless build nothing reads `InputStream` at all, exactly as
      before.

    Like `NullChoicePrompter` it still *draws* the question, because the screen
    is what a browser is watching. Unlike it, an unanswered question is
    `TIMED_OUT` and not `NO_OPERATOR`: a browser view is an attached input
    device, so retrying can help, and D32's distinction is precisely that
    `NO_OPERATOR` means it never can.

    One question at a time. `AuthorizationPrompter` already holds the screen
    exclusively for the whole exchange (D36), so a second concurrent `ask` is a
    bug elsewhere; it is refused here rather than silently replacing a prompt
    the operator is mid-way through reading.
    """

    def __init__(
        self,
        *,
        show: ShowChoice | None = None,
        default_timeout_s: float = DEFAULT_CHOICE_TIMEOUT_S,
    ) -> None:
        self._show = show
        self._default_timeout_s = default_timeout_s
        self._waiter: _Waiter | None = None
        #: Read cross-thread. Written only on the loop, always as a whole.
        self._pending: PendingQuestion | None = None

    @property
    def pending(self) -> PendingQuestion | None:
        """The unanswered question, safe to read from any thread."""
        return self._pending

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResult:
        if not options:
            return ChoiceResult(outcome=ChoiceOutcome.NO_OPERATOR)
        if self._waiter is not None:
            logger.warning(
                "A choice is already pending; refusing to ask a second",
                extra={"question": question},
            )
            return ChoiceResult(outcome=ChoiceOutcome.NO_OPERATOR)

        waiter = _Waiter(question=PendingQuestion.create(question, options))
        self._waiter = waiter
        self._pending = waiter.question
        try:
            if self._show is not None:
                await self._show(question, options, 0)
            timeout = self._default_timeout_s if timeout_s is None else timeout_s
            try:
                return await asyncio.wait_for(asyncio.shield(waiter.future), timeout=timeout)
            except TimeoutError:
                logger.info("Choice prompt expired", extra={"question": question})
                return ChoiceResult(outcome=ChoiceOutcome.TIMED_OUT)
        finally:
            self._waiter = None
            self._pending = None

    async def answer(self, token: str, index: int) -> bool:
        """Resolve the pending question. `False` if there is nothing to resolve.

        Runs on the event loop — a caller on another thread reaches it through
        `asyncio.run_coroutine_threadsafe`, which is what keeps the future's
        completion on the loop that is awaiting it.
        """
        return self._resolve(
            token,
            lambda question: (
                ChoiceResult(
                    outcome=ChoiceOutcome.ANSWERED,
                    option=question.options[index],
                    index=index,
                )
                if 0 <= index < len(question.options)
                else None
            ),
        )

    async def cancel(self, token: str) -> bool:
        """The operator declined without choosing. The BACK button, by another road."""
        return self._resolve(token, lambda _question: ChoiceResult(outcome=ChoiceOutcome.CANCELLED))

    def _resolve(self, token: str, build: Callable[[PendingQuestion], ChoiceResult | None]) -> bool:
        waiter = self._waiter
        if waiter is None:
            return False
        # `compare_digest` raises on non-ASCII text, and the token arrives from
        # an HTTP body: a rejected answer must never be a 500.
        if not token.isascii() or not secrets.compare_digest(token, waiter.question.token):
            # Almost always a stale page. Logged rather than raised: the
            # question it was answering is gone, so there is nothing to fail.
            logger.info("Choice answer named a question that is no longer pending")
            return False
        result = build(waiter.question)
        if result is None or waiter.future.done():
            return False
        waiter.future.set_result(result)
        return True


class InputChoicePrompter:
    """Draws a question and consumes the action stream until it is answered.

    Two ways to answer, one code path. The joystick moves a highlight and a
    button confirms it; a finger names an option directly, because the panel
    resolved the tap into an index before it reached the wire (D13). Nothing
    here hit-tests a coordinate.

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

    @staticmethod
    def _selected(event: ChoiceSelection, options: list[str]) -> ChoiceResult | None:
        """A tap on an option, or `None` if it cannot be trusted to be one.

        A tap is navigation and confirmation in one gesture — there is no
        highlight to move first — so a good selection answers immediately.

        Two ways it is refused, and both are dropped silently rather than
        answered wrongly. An index off the end means the sender drew a screen
        this consumer is not showing. A stated label that does not match the
        option at that index means the same thing more precisely: the screen
        was replaced between the draw and the finger. On an authorization
        prompt that is the difference between approving what the operator read
        and approving what arrived while they were reading, so it can never
        resolve into an answer. The question simply stays up.
        """
        if not 0 <= event.index < len(options):
            logger.info("Ignored a selection for an option this question does not have")
            return None
        if event.option and event.option != options[event.index]:
            logger.info("Ignored a selection whose label does not match the question on screen")
            return None
        return ChoiceResult(
            outcome=ChoiceOutcome.ANSWERED,
            option=options[event.index],
            index=event.index,
        )

    async def _run(self, question: str, options: list[str]) -> ChoiceResult:
        highlighted = 0
        await self._show(question, options, highlighted)

        async for event in self._stream.events():
            if isinstance(event, ChoiceSelection):
                result = self._selected(event, options)
                if result is not None:
                    return result
                continue

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
