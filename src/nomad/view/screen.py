"""One screen, and one writer at a time (D36).

Before this module there were two independent writers to the same glass and no
arbitration between them: `TurnRenderer` redraws on every streamed chunk, and
the model's `display_*` tools draw whenever the model feels like it (D35 made
them auto-run in every mode). That was survivable while the only thing on the
screen was text nobody was acting on.

It stops being survivable the moment an **authorization prompt** is on the
screen. A question the operator is being asked to answer, overwritten
mid-stream by the next token of the turn that is *waiting on that answer*, is
worse than no prompt at all — the operator sees a flash of something they
cannot read and a joystick press then lands on a decision they never saw. And
the model-driven half is not merely a race: a model that can draw over a live
authorization prompt can hide the question it is being asked about.

So every writer goes through a `ScreenView` handed out by one `ScreenOwner`,
and a caller that needs the screen to itself takes `exclusive()`. While it is
held, every other writer's frames are dropped — not queued. Queuing them would
replay a stale turn frame over the prompt's own resolution the instant it let
go, which is the same bug with a delay.

Dropping is safe for the renderer specifically because of the property F1
already relies on: `agent.turn_finished` is authoritative and redraws the whole
answer (D6). A pending authorization only ever exists *inside* a turn, so every
suppressed frame is followed by a full, correct redraw.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from nomad.core.logging import get_logger
from nomad.mcp.hardware import DisplayDriver

logger = get_logger(__name__)

#: A deferred draw: the call one writer wanted, not yet made.
Draw = Callable[[DisplayDriver], Awaitable[None]]


class ScreenView:
    """One writer's handle on the screen. `DisplayDriver`-shaped, on purpose.

    It satisfies the same protocol the concrete drivers do, so a component
    that already takes a `DisplayDriver` — the renderer, the display tools —
    takes one of these instead with no other change, and cannot tell whether
    it is being suppressed. That is what keeps the arbitration in one place
    rather than as an `if` in every drawing site.
    """

    def __init__(self, owner: ScreenOwner, writer: str) -> None:
        self._owner = owner
        self._writer = writer

    @property
    def writer(self) -> str:
        return self._writer

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        await self._owner.draw(self._writer, lambda d: d.show_text(text, title=title))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        await self._owner.draw(self._writer, lambda d: d.show_card(title, body, rows))

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None:
        await self._owner.draw(
            self._writer, lambda d: d.show_list(title, items, selectable=selectable)
        )

    async def show_choice(self, question: str, options: list[str]) -> None:
        await self._owner.draw(self._writer, lambda d: d.show_choice(question, options))


class ScreenOwner:
    """Arbitrates the one display between the components that draw on it."""

    def __init__(self, display: DisplayDriver) -> None:
        self._display = display
        # Serialises the draws themselves. A real driver's `show_*` is several
        # awaits over a serial link, so without this two writers interleave
        # halfway through a frame and the glass shows neither.
        self._draw_lock = asyncio.Lock()
        # Serialises *claims*. A second claimant waits rather than being
        # refused: the thing that claims this screen is an authorization
        # prompt, and refusing to draw one is the failure this chunk exists to
        # fix. The queue's own timeout still bounds the wait (D21).
        self._claim_lock = asyncio.Lock()
        self._holder: str | None = None
        self.suppressed = 0

    @property
    def display(self) -> DisplayDriver:
        """The driver underneath. For the composition root, not for writers."""
        return self._display

    @property
    def holder(self) -> str | None:
        """Who currently owns the screen exclusively, if anyone."""
        return self._holder

    def view(self, writer: str) -> ScreenView:
        """A handle for one named writer. Handing these out is the whole API."""
        return ScreenView(self, writer)

    async def draw(self, writer: str, draw: Draw) -> None:
        """Draw, unless someone else holds the screen."""
        if self._suppress(writer):
            return
        async with self._draw_lock:
            # Re-checked under the lock: a frame that queued behind another
            # writer's draw may find the screen claimed by the time it runs,
            # and the claim has to win in that case too.
            if self._suppress(writer):
                return
            await draw(self._display)

    def _suppress(self, writer: str) -> bool:
        holder = self._holder
        if holder is None or holder == writer:
            return False
        self.suppressed += 1
        logger.debug(
            "Screen write suppressed", extra={"writer": writer, "holder": holder}
        )
        return True

    @contextlib.asynccontextmanager
    async def exclusive(self, writer: str) -> AsyncIterator[ScreenView]:
        """Take the screen for one writer until the block exits."""
        async with self._claim_lock:
            self._holder = writer
            logger.info("Screen claimed", extra={"writer": writer})
            try:
                yield self.view(writer)
            finally:
                self._holder = None
                logger.info("Screen released", extra={"writer": writer})
