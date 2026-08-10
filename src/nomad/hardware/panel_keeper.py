"""Keep the panel in agreement with what Nomad believes is on screen.

The ESP32-S3 is a **stateless renderer on the end of a cable**. It draws the
last `display.state` it received and then holds that picture forever. Nothing
about that is wrong — it is what makes the panel cheap and the protocol small —
but it means a single frame that never arrives leaves the glass permanently
lying, with every layer above reporting success:

* opening the serial port toggles DTR/RTS, which on an ESP32-S3 is the reset
  line, so the panel restarts *because* Nomad connected and comes back after
  the boot card was already sent;
* a cable knocked loose and pushed back in costs whatever was in flight;
* a brownout on battery restarts the panel and not the Pi.

Every one of those was diagnosed the same way: the operator said "the screen
still says waiting for the Pi" while the Pi's logs said it had drawn. A
one-shot redraw after a fixed settle delay covers exactly the first case and
only if the guess about timing is right — it was in the tree already, and the
screen was still wrong.

So this component does the boring, correct thing instead: **it repaints the
current screen on a tick, forever.** A repaint is idempotent — the panel is
being told what it should already be showing — so the worst case of a tick that
was not needed is a few dozen bytes on a link with capacity to spare, and the
worst case of a missed frame shrinks from "wrong until someone notices" to
"wrong for one interval".

Two things make that affordable rather than wasteful:

* `display.state` is structure, not pixels (D30) — a card is tens of bytes,
  where a rasterised region would be 8-11 KB and this design would be absurd.
* The firmware skips re-rendering a payload identical to the one it already
  drew, so a repaint of an unchanged screen costs no flicker and no CPU on the
  panel. Without that, this loop would make the display strobe.

It writes through `DisplayFanout.repaint`, which replays the fanout's last call
to one surface. That is deliberately not a normal write: it must not disturb
the other surfaces, count as a new frame, or re-decide what should be on screen.
Nomad already decided; this only ensures the panel heard.
"""

from __future__ import annotations

import asyncio
import contextlib

from nomad.core.logging import get_logger
from nomad.hardware.fanout import DisplayFanout

logger = get_logger(__name__)

#: The surface this keeps honest. The headless mirror is in-process and cannot
#: miss a write, so there is nothing to repair there.
PANEL_SURFACE = "esp32"


class PanelKeeper:
    """Repaints the panel on a fixed tick so a lost frame heals itself."""

    name = "panel_keeper"

    def __init__(
        self,
        fanout: DisplayFanout,
        *,
        interval_s: float = 2.0,
        surface: str = PANEL_SURFACE,
    ) -> None:
        self._fanout = fanout
        self._interval_s = interval_s
        self._surface = surface
        self._task: asyncio.Task[None] | None = None
        #: Repaints actually written. Read by diagnostics — a keeper that is
        #: running and never repainting means the fanout has drawn nothing.
        self.repaints = 0
        #: Repaints that raised. A pulled cable is expected, so this is a
        #: counter rather than a reason to stop.
        self.failures = 0

    async def start(self) -> None:
        if self._task is None and self._interval_s > 0:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                if await self._fanout.repaint(self._surface):
                    self.repaints += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dead keeper is a lying screen
                # Never breaks the loop. The panel being unreachable *now* is
                # the normal reason a repaint fails, and it is exactly the
                # state this loop exists to recover from once the cable is
                # back. Stopping on the first failure would guarantee the
                # screen stays wrong for the one case it was written for.
                self.failures += 1
                if self.failures in (1, 10) or self.failures % 100 == 0:
                    # Rate-limited: an unplugged panel would otherwise write a
                    # log line every interval for as long as the device runs.
                    logger.warning(
                        "Panel repaint failed",
                        extra={"error": f"{type(exc).__name__}: {exc}", "failures": self.failures},
                    )
