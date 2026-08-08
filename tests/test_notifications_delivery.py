"""Timers that actually fire, and never over a live authorization prompt.

The defect these close: `NotificationQueue.deliver_due()` had zero callers in
`src/` and no `NotificationSink` existed anywhere, so chunks N and U shipped a
device that answered "timer '5 minutes' set for 5m 00s" and then did nothing at
all, five minutes later, forever. A confirmed promise silently broken is worse
than a refusal, and it voided two otherwise finished chunks.

The second half is the arbitration. A notification is the fourth writer to one
screen (D36) and the only one for which the existing rule — a suppressed frame
is dropped — is wrong: nothing ever redraws a fired timer. So the sink declines
the screen rather than waiting for it or taking it, and the row it could not
show stays `PENDING`.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from nomad.core.config import NomadConfig, NotificationsConfig
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.notifications.delivery import (
    NOTIFICATION_WRITER,
    NotificationDelivery,
    ScreenNotificationSink,
)
from nomad.notifications.errors import NotificationDeferred
from nomad.notifications.models import Notification, NotificationKind, NotificationState
from nomad.notifications.queue import NotificationQueue
from nomad.resources.workload import Tier
from nomad.storage.db import Database
from nomad.storage.migrations import migrate
from nomad.view.authprompt import AUTH_PROMPT_WRITER
from nomad.view.screen import ScreenOwner


@pytest.fixture
def display() -> HeadlessDisplay:
    return HeadlessDisplay()


@pytest.fixture
def screen(display: HeadlessDisplay) -> ScreenOwner:
    return ScreenOwner(display)  # type: ignore[arg-type]


@pytest.fixture
async def queue(tmp_path) -> NotificationQueue:  # type: ignore[no-untyped-def]
    db = Database(str(tmp_path / "nomad.db"))
    await db.start()
    await migrate(db)
    try:
        yield NotificationQueue(db, config=NotificationsConfig())
    finally:
        await db.stop()


def _sink(screen: ScreenOwner, **kwargs: object) -> ScreenNotificationSink:
    # No dwell by default: the dwell is a readability policy, and a test that
    # waited four real seconds for it would be deleted rather than fixed.
    kwargs.setdefault("dwell_seconds", 0.0)
    return ScreenNotificationSink(screen, **kwargs)  # type: ignore[arg-type]


async def _due(queue: NotificationQueue, **kwargs: object) -> Notification:
    kwargs.setdefault("title", "tea is ready")
    kwargs.setdefault("kind", NotificationKind.TIMER)
    kwargs.setdefault("due_at", datetime.now(UTC) - timedelta(seconds=1))
    return await queue.raise_notification(**kwargs)  # type: ignore[arg-type]


# -- it fires at all ---------------------------------------------------------


async def test_a_due_notification_reaches_the_screen_and_is_marked_delivered(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    row = await _due(queue, body="the kettle boiled")
    delivered = await queue.deliver_due(_sink(screen))

    assert delivered == 1
    assert display.screen.text == "Timer\ntea is ready\nthe kettle boiled"
    stored = await queue.get(row.id)
    assert stored is not None
    assert stored.state is NotificationState.DELIVERED


async def test_the_title_is_nomads_chrome_and_not_the_rows_text(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    """The operator must be able to tell a fired alarm from a line of prose."""
    await _due(queue, title="Approved", kind=NotificationKind.ALARM)
    await queue.deliver_due(_sink(screen))
    assert display.screen.text.splitlines()[0] == "Alarm"


async def test_the_delivery_component_polls_until_the_row_is_due(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    """The whole point: something in `src/` calls `deliver_due` on a clock."""
    delivery = NotificationDelivery(queue, _sink(screen), poll_seconds=0.01)
    await delivery.start()
    try:
        await _due(queue, due_at=datetime.now(UTC) + timedelta(milliseconds=60))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if delivery.delivered:
                break
    finally:
        await delivery.stop()

    assert delivery.delivered == 1
    assert "tea is ready" in display.screen.text


# -- and never over a live authorization prompt (D36) ------------------------


async def test_a_notification_never_paints_over_a_pending_prompt(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    row = await _due(queue)
    sink = _sink(screen)

    async with screen.exclusive(AUTH_PROMPT_WRITER) as prompt:
        await prompt.show_text("Allow this action?", title="Authorization required")
        with pytest.raises(NotificationDeferred):
            await sink(row)
        # Not merely un-drawn: nothing at all reached the glass, so the
        # question the operator is answering is still the question on screen.
        assert display.screen.text == "Authorization required\nAllow this action?"

    stored = await queue.get(row.id)
    assert stored is not None
    assert stored.state is NotificationState.PENDING
    assert sink.deferred == 1
    assert sink.shown == 0


async def test_a_deferred_notification_is_shown_by_the_next_poll(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    """Claimed, not fired: the promise survives the prompt that delayed it."""
    row = await _due(queue)
    sink = _sink(screen)

    async with screen.exclusive(AUTH_PROMPT_WRITER):
        with pytest.raises(NotificationDeferred):
            await queue.deliver_due(sink)

    assert await queue.deliver_due(sink) == 1
    stored = await queue.get(row.id)
    assert stored is not None
    assert stored.state is NotificationState.DELIVERED
    assert "tea is ready" in display.screen.text


async def test_the_loop_survives_a_sink_that_keeps_refusing(
    queue: NotificationQueue, screen: ScreenOwner
) -> None:
    """A delivery loop that dies on its first failure is no loop at all."""
    failures = 0

    async def broken(notification: Notification) -> None:
        nonlocal failures
        failures += 1
        raise RuntimeError("the display fell off")

    delivery = NotificationDelivery(queue, broken, poll_seconds=0.01)
    await _due(queue)
    await delivery.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if failures >= 3:
                break
    finally:
        await delivery.stop()

    assert failures >= 3
    assert delivery.delivered == 0
    assert [n.state for n in await queue.undelivered()] == [NotificationState.PENDING]


async def test_the_notification_holds_the_screen_while_it_is_up(
    queue: NotificationQueue, screen: ScreenOwner, display: HeadlessDisplay
) -> None:
    """A reminder overwritten by the next streamed chunk was not delivered."""
    row = await _due(queue)
    renderer = screen.view("renderer")
    sink = ScreenNotificationSink(screen, dwell_seconds=60.0)  # type: ignore[arg-type]

    showing = asyncio.create_task(sink(row))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if screen.holder == NOTIFICATION_WRITER:
            break
    assert display.screen.text.startswith("Timer")

    await renderer.show_text("…thinking", title="Turn")
    assert display.screen.text.startswith("Timer")
    assert screen.suppressed >= 1

    showing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await showing
    # Cancelled mid-dwell still releases: an abandoned claim would be a screen
    # nothing else could ever draw on again, including a prompt.
    assert screen.holder is None


# -- speaking is a seam, not a call (D37) ------------------------------------


async def test_a_speaker_that_raises_does_not_undeliver_the_notification(
    queue: NotificationQueue, screen: ScreenOwner
) -> None:
    """The screen is what "delivered" means; audio is best-effort on top."""

    async def mute(text: str) -> None:
        raise NotImplementedError("the alsa speaker driver is a stub")

    row = await _due(queue)
    assert await queue.deliver_due(_sink(screen, announce=mute)) == 1
    stored = await queue.get(row.id)
    assert stored is not None
    assert stored.state is NotificationState.DELIVERED


async def test_a_speaker_that_works_is_given_the_notification_text(
    queue: NotificationQueue, screen: ScreenOwner
) -> None:
    spoken: list[str] = []

    async def speaker(text: str) -> None:
        spoken.append(text)

    await _due(queue, body="the kettle boiled")
    await queue.deliver_due(_sink(screen, announce=speaker))
    assert spoken == ["tea is ready\nthe kettle boiled"]


async def test_a_deferred_notification_is_not_spoken_either(
    queue: NotificationQueue, screen: ScreenOwner
) -> None:
    """Announcing a reminder the operator cannot see is the same broken promise."""
    spoken: list[str] = []

    async def speaker(text: str) -> None:
        spoken.append(text)

    row = await _due(queue)
    async with screen.exclusive(AUTH_PROMPT_WRITER):
        with pytest.raises(NotificationDeferred):
            await _sink(screen, announce=speaker)(row)
    assert spoken == []


# -- wired into the device (D38) ---------------------------------------------


async def test_the_app_starts_a_delivery_loop_and_declares_it_interactive(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    from nomad.app import NomadApp

    config = NomadConfig()
    config = config.model_copy(
        update={
            "storage": config.storage.model_copy(update={"path": str(tmp_path / "n.db")}),
            "workspace": config.workspace.model_copy(update={"root": str(tmp_path / "ws")}),
            "view": config.view.model_copy(update={"enabled": False}),
        }
    )
    app = NomadApp(config)
    assert app.delivery is not None
    assert app.delivery.name == NOTIFICATION_WRITER

    await app.start()
    try:
        assert app.states()[NOTIFICATION_WRITER].value == "started"
        assert app.governor is not None
        workloads = {w.name: w for w in app.governor.inventory()}
        # D38 names the notification queue as a tier that is never preempted,
        # and the declaration now names something that is actually running.
        assert workloads[NOTIFICATION_WRITER].tier is Tier.INTERACTIVE
    finally:
        await app.stop()
