"""A frame the panel never received does not stay lost.

The panel is a stateless renderer: it draws the last `display.state` it was
sent and holds that picture forever. So the failure this file pins is the one
where *every layer reports success and the glass is wrong* — the Pi wrote, the
fanout counted no failure, and the panel was resetting at that exact moment.

The tests are therefore about replay, not about drawing: that the fanout
remembers its last write, that replaying it reaches one surface and disturbs
nothing else, and that the keeper's loop survives the unplugged cable it exists
to recover from.
"""

from __future__ import annotations

import asyncio

import pytest

from nomad.hardware.fanout import DisplayFanout, Surface
from nomad.hardware.panel_keeper import PANEL_SURFACE, PanelKeeper


class RecordingDisplay:
    """Records what it was told to draw, and can be told to refuse."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        self._record(("show_text", text, title))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        self._record(("show_card", title, body, tuple(rows)))

    async def show_list(self, title, items, *, selectable: bool = False) -> None:
        self._record(("show_list", title, tuple(items), selectable))

    async def show_choice(self, question: str, options: list[str]) -> None:
        self._record(("show_choice", question, tuple(options)))

    def _record(self, call: tuple) -> None:
        if self.fail:
            raise OSError("cable")
        self.calls.append(call)


def _fanout() -> tuple[DisplayFanout, RecordingDisplay, RecordingDisplay]:
    panel, mirror = RecordingDisplay(), RecordingDisplay()
    return (
        DisplayFanout([Surface(PANEL_SURFACE, panel), Surface("headless", mirror)]),
        panel,
        mirror,
    )


# -- the fanout's half: remembering, and replaying to one surface ------------


async def test_repaint_before_anything_is_drawn_is_a_no_op() -> None:
    """A keeper started before the first draw must not invent a screen."""
    fanout, panel, _ = _fanout()
    assert await fanout.repaint(PANEL_SURFACE) is False
    assert panel.calls == []


async def test_repaint_of_an_unknown_surface_is_false() -> None:
    fanout, _, _ = _fanout()
    await fanout.show_text("hello")
    assert await fanout.repaint("no-such-screen") is False


async def test_repaint_replays_the_last_write_to_that_surface_only() -> None:
    """The whole point: the panel is brought back into agreement without the
    caller re-deciding what to draw, and the mirror — which never missed
    anything — is not written to twice."""
    fanout, panel, mirror = _fanout()
    await fanout.show_card("Nomad", "ready", [("battery", "82%")])

    assert await fanout.repaint(PANEL_SURFACE) is True

    assert panel.calls == [
        ("show_card", "Nomad", "ready", (("battery", "82%"),)),
        ("show_card", "Nomad", "ready", (("battery", "82%"),)),
    ]
    assert len(mirror.calls) == 1


async def test_repaint_replays_the_most_recent_write_not_the_first() -> None:
    fanout, panel, _ = _fanout()
    await fanout.show_text("first")
    await fanout.show_text("second")
    await fanout.repaint(PANEL_SURFACE)
    assert panel.calls[-1] == ("show_text", "second", None)


async def test_repaint_is_not_counted_as_a_write() -> None:
    """`stats.writes` answers "how many frames did Nomad decide to draw". A
    repaint decided nothing, and counting it would make the number useless for
    the diagnostics it exists for."""
    fanout, _, _ = _fanout()
    await fanout.show_text("hello")
    for _ in range(5):
        await fanout.repaint(PANEL_SURFACE)
    assert fanout.stats.writes == 1


async def test_a_failing_repaint_raises_to_the_keeper() -> None:
    """`_fan` swallows one surface's failure because the others still have a
    screen. A repaint has no others, so the failure must reach the caller —
    otherwise the keeper counts a pulled cable as a successful repaint."""
    panel = RecordingDisplay(fail=True)
    fanout = DisplayFanout([Surface(PANEL_SURFACE, panel)])
    fanout._last = lambda d: d.show_text("x")  # noqa: SLF001 - no other way to arm it
    with pytest.raises(OSError):
        await fanout.repaint(PANEL_SURFACE)


# -- the keeper's half: the tick --------------------------------------------


async def test_the_keeper_repaints_on_a_tick() -> None:
    fanout, panel, _ = _fanout()
    await fanout.show_text("hello")
    keeper = PanelKeeper(fanout, interval_s=0.01)
    await keeper.start()
    try:
        for _ in range(200):
            if keeper.repaints >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await keeper.stop()

    assert keeper.repaints >= 3
    assert keeper.failures == 0
    # Every repaint is the same screen, which is what makes the firmware's
    # identical-payload skip the thing that stops this strobing.
    assert set(panel.calls) == {("show_text", "hello", None)}


async def test_an_unreachable_panel_does_not_stop_the_loop() -> None:
    """The unplugged cable is the state this loop exists to recover from, so
    stopping on it would guarantee the screen stays wrong for exactly the case
    it was written for."""
    panel = RecordingDisplay(fail=True)
    fanout = DisplayFanout([Surface(PANEL_SURFACE, panel), Surface("headless", RecordingDisplay())])
    await fanout.show_text("hello")

    keeper = PanelKeeper(fanout, interval_s=0.01)
    await keeper.start()
    try:
        for _ in range(200):
            if keeper.failures >= 3:
                break
            await asyncio.sleep(0.01)
        # Still running after repeated failures, and able to recover.
        panel.fail = False
        for _ in range(200):
            if keeper.repaints >= 1:
                break
            await asyncio.sleep(0.01)
    finally:
        await keeper.stop()

    assert keeper.failures >= 3
    assert keeper.repaints >= 1


async def test_stop_is_idempotent_and_leaves_no_task() -> None:
    fanout, _, _ = _fanout()
    keeper = PanelKeeper(fanout, interval_s=0.01)
    await keeper.start()
    await keeper.stop()
    await keeper.stop()
    assert keeper._task is None  # noqa: SLF001


async def test_a_zero_interval_never_starts_a_task() -> None:
    """The knob has to be able to turn this off — a repaint tick on a mock
    display is pure noise in a test run."""
    fanout, panel, _ = _fanout()
    await fanout.show_text("hello")
    keeper = PanelKeeper(fanout, interval_s=0)
    await keeper.start()
    await asyncio.sleep(0.03)
    await keeper.stop()
    assert keeper.repaints == 0
    assert len(panel.calls) == 1
