"""One logical screen, several physical surfaces.

Nomad's face is the ESP32-S3 panel, but it is not the only glass the device may
have in front of it: plug an HDMI monitor into the Pi and the operator
reasonably expects to see the same thing, larger. `DisplayFanout` is how that
happens — it satisfies `DisplayDriver` itself and forwards every call to each
surface it holds.

**This does not weaken D36, because D36 arbitrates *writers*, not surfaces.**
`ScreenOwner` still hands out exactly one `ScreenView` at a time and an
authorization prompt still takes `exclusive()`; a fan-out sits *underneath* all
of that, as the single driver the owner writes to. So mirroring is invisible to
arbitration, which is the reason it costs nothing to add here rather than
threading a list of screens through the whole view layer.

Fanning out is only cheap because E2 chose structure over pixels: a
`display.state` message is a few dozen bytes of card/list/choice, so each
surface renders it in whatever way suits its own panel and fonts. Had the
display vocabulary been a framebuffer, mirroring would mean rasterising once
per screen at a different resolution each time.

**A failing surface must not take the others with it.** An unplugged monitor,
a reset ESP32, a serial cable pulled mid-frame — each is expected on this
device, and none of them is a reason for the operator's remaining screen to go
dark. So every surface is written concurrently, every failure is logged and
counted, and the call as a whole succeeds if *any* surface accepted it. It
raises only when every surface failed, because at that point there genuinely is
no screen and a caller that believes otherwise would be worse off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nomad.core.logging import get_logger
from nomad.hardware.errors import HardwareError

logger = get_logger(__name__)


@dataclass
class Surface:
    """One physical screen, and whether it is currently working."""

    name: str
    driver: object
    #: Consecutive failures. Reset on the first success.
    failures: int = 0
    #: Total writes this surface refused, for `describe()` and diagnostics.
    total_failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.failures == 0


@dataclass
class FanoutStats:
    """Worth watching when a screen is on the end of a cable that can be pulled."""

    writes: int = 0
    #: Writes where at least one surface failed but another succeeded.
    partial: int = 0
    #: Writes where every surface failed.
    blackouts: int = 0
    per_surface_failures: dict[str, int] = field(default_factory=dict)


class DisplayFanout:
    """Mirrors one structural screen state onto every surface it holds."""

    def __init__(self, surfaces: list[Surface]) -> None:
        if not surfaces:
            raise HardwareError("a display fanout needs at least one surface")
        self._surfaces = surfaces
        self.stats = FanoutStats()

    @property
    def surfaces(self) -> list[Surface]:
        return list(self._surfaces)

    @property
    def primary(self) -> object:
        """The first surface's driver.

        Some callers legitimately need one concrete screen rather than all of
        them — the HTTP view reaches for the headless surface's HTML, and there
        is no meaningful way to mirror *that*.
        """
        return self._surfaces[0].driver

    def surface(self, name: str) -> object | None:
        """The driver for `name`, or `None`. Used to find the headless surface
        without the caller having to know the ordering."""
        for surface in self._surfaces:
            if surface.name == name:
                return surface.driver
        return None

    def describe(self) -> str:
        parts = [f"{s.name}{'' if s.healthy else ' (failing)'}" for s in self._surfaces]
        return ", ".join(parts)

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        await self._fan("show_text", lambda d: d.show_text(text, title=title))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        await self._fan("show_card", lambda d: d.show_card(title, body, rows))

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None:
        await self._fan("show_list", lambda d: d.show_list(title, items, selectable=selectable))

    async def show_choice(self, question: str, options: list[str]) -> None:
        await self._fan("show_choice", lambda d: d.show_choice(question, options))

    async def _fan(self, what: str, call: Callable[[object], Awaitable[None]]) -> None:
        """Write to every surface concurrently; succeed if any accepted it.

        Concurrently rather than in sequence because the surfaces are
        independent and one of them is a serial link — serialising would make
        the slowest cable set the frame rate for the fastest screen.
        """
        self.stats.writes += 1
        results = await asyncio.gather(
            *(call(surface.driver) for surface in self._surfaces),
            return_exceptions=True,
        )

        failed: list[str] = []
        for surface, result in zip(self._surfaces, results, strict=True):
            if isinstance(result, BaseException):
                surface.failures += 1
                surface.total_failures += 1
                self.stats.per_surface_failures[surface.name] = surface.total_failures
                failed.append(surface.name)
                # Logged once per failed write, at warning: a pulled cable is
                # expected on this device, so it is not an error, but a screen
                # silently not updating is exactly what we refuse to allow.
                logger.warning(
                    "display surface failed",
                    extra={"surface": surface.name, "call": what, "error": str(result)},
                )
            elif surface.failures:
                logger.info("display surface recovered", extra={"surface": surface.name})
                surface.failures = 0

        if not failed:
            return

        if len(failed) == len(self._surfaces):
            self.stats.blackouts += 1
            raise HardwareError(
                f"every display surface failed on {what}",
                {"surfaces": failed, "call": what},
            )

        self.stats.partial += 1
