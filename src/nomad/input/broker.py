"""One reader of the input stream, and a way to lend it out (D44).

`InputStream.events()` is a single queue: two `async for` loops over it steal
from each other, and input that arrives every *other* press is worse than input
that never arrives because the first looks like flaky hardware. So the contract
has always been "exactly one consumer", and `InputChoicePrompter` was it.

That contract is also why the device had no way to say *anything* to itself
between questions. The prompter reads the stream only while a question is up;
the rest of the time — which on a handheld is nearly all of the time — nobody
reads it at all. Two consequences, and the second is the interesting one:

* **Nothing could react to a press while idle.** A wake button is exactly that
  press, so an idle affordance had nowhere to live. It could not simply open a
  second reader without breaking the contract above.
* **Idle presses did not vanish; they *queued*.** The queue is bounded and
  drops the oldest, so a tap made while nothing was listening sat there until
  the next question opened a reader — and was then delivered to that question
  as though it had been made in answer to it. For an authorization prompt that
  is the difference between approving what the operator read and approving
  what happened to be on the glass ten minutes earlier. `ChoiceSelection` was
  already defended against it by label (`InputChoicePrompter._selected`), but a
  bare `CONFIRM` action carries no label to check.

This module makes the single reader an explicit, permanent one. It delivers
every event to whoever has *borrowed* the stream, and to an idle handler when
nobody has. A borrow starts empty, so nothing an operator did while idle can
ever be read as an answer to a question that did not exist yet.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from nomad.core.logging import get_logger
from nomad.input.events import ChoiceSelection, InputAction, TouchEvent
from nomad.input.stream import InputStream

logger = get_logger(__name__)

InputEvent = InputAction | TouchEvent | ChoiceSelection

#: What sees presses when no question is up. Async because the thing it does —
#: starting a turn, drawing a screen — is async everywhere else on this device.
IdleHandler = Callable[[InputEvent], Awaitable[None]]

#: A borrower's inbox. Small on purpose: it exists to cover the microseconds
#: between the broker reading an event and the borrower's loop waking, not to
#: bank input. Bounded and oldest-dropping for the same reason `InputStream` is
#: (D6) — and at this depth, if it ever overflows the operator is holding a
#: button down and the newest press is the one they still mean.
_BORROW_QUEUE_DEPTH = 32


class InputBroker:
    """The one consumer of `InputStream.events()`. Lends it to one borrower."""

    name = "input_broker"

    def __init__(self, stream: InputStream, *, idle: IdleHandler | None = None) -> None:
        self._stream = stream
        self._idle = idle
        self._borrower: asyncio.Queue[InputEvent] | None = None
        self._task: asyncio.Task[None] | None = None
        #: Events that reached nobody, because no borrower held the stream and
        #: no idle handler was wired. Counted rather than logged per event: on
        #: a headless build this is every press, and it is not an error.
        self.unclaimed = 0

    @property
    def lent(self) -> bool:
        """Is a question currently holding the stream?"""
        return self._borrower is not None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def set_idle_handler(self, idle: IdleHandler | None) -> None:
        """Wire the idle handler after construction.

        The composition root builds the broker before the thing that reacts to
        an idle press, because the prompter needs the broker and the idle
        affordance needs the prompter's screen. Rather than thread a
        placeholder through three constructors, the handler is set once
        everything exists.
        """
        self._idle = idle

    async def _run(self) -> None:
        try:
            async for event in self._stream.events():
                await self._deliver(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead broker is silent hardware
            # Logged at error for the same reason `InputRouter` does it: the
            # symptom is a panel that draws perfectly and answers nothing.
            logger.error("Input broker stopped", extra={"error": f"{type(exc).__name__}: {exc}"})

    async def _deliver(self, event: InputEvent) -> None:
        borrower = self._borrower
        if borrower is not None:
            self._offer(borrower, event)
            return
        if self._idle is None:
            self.unclaimed += 1
            return
        try:
            await self._idle(event)
        except Exception:  # noqa: BLE001 - an idle handler must never kill the reader
            logger.warning("Idle input handler raised", exc_info=True)

    @staticmethod
    def _offer(queue: asyncio.Queue[InputEvent], event: InputEvent) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - needs 32 presses in one tick
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def borrow(self) -> AsyncIterator[AsyncIterator[InputEvent]]:
        """Take the stream for the length of one question.

        The inbox is created here and dropped on exit, which is what makes a
        borrow start empty: presses made before the question existed went to
        the idle handler and are gone, so none of them can be read as an answer
        to it.

        **A second concurrent borrow is refused, and refused by ending.** It
        should not happen — an authorization prompt holds the screen
        exclusively for its whole exchange (D36) — so if it does, something
        above is wrong and the safe reading of "wrong" is *no answer*. A stream
        that ends immediately makes `InputChoicePrompter` return `NO_OPERATOR`,
        which denies. Waiting instead would leave two questions racing for one
        operator's finger.
        """
        if self._borrower is not None:
            logger.warning("Input stream is already lent out; refusing a second borrower")
            yield _closed()
            return

        queue: asyncio.Queue[InputEvent] = asyncio.Queue(maxsize=_BORROW_QUEUE_DEPTH)
        self._borrower = queue
        try:
            yield _drain(queue)
        finally:
            self._borrower = None


async def _drain(queue: asyncio.Queue[InputEvent]) -> AsyncIterator[InputEvent]:
    while True:
        yield await queue.get()


async def _closed() -> AsyncIterator[InputEvent]:
    """A stream with nothing in it, ever."""
    return
    yield  # pragma: no cover - unreachable, but this is what makes it a generator
