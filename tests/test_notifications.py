"""Chunk N: the queue that exists because the bus drops things (D6).

Every test here injects a clock. Nothing sleeps for real time: a policy number
proved by waiting is a test that fails once a month on a two-core laptop and
gets deleted rather than fixed (see `resources/clock.py` for the same argument).
The clock is a plain `Callable[[], datetime]` rather than `resources.Clock`
because *wall* time is what a durable notification is anchored to — a monotonic
clock resets at boot, which is precisely the event these rows have to survive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nomad.core.config import NotificationsConfig
from nomad.notifications.errors import NotificationRefused
from nomad.notifications.models import (
    Notification,
    NotificationKind,
    NotificationState,
    default_dedup_key,
    normalize_dedup_key,
)
from nomad.notifications.queue import NotificationQueue
from nomad.notifications.repeat import next_occurrence, parse_repeat_rule
from nomad.storage.db import Database
from nomad.storage.migrations import migrate

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FakeClock:
    """Wall time that moves only when a test moves it."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def queue(db: Database, clock: FakeClock) -> NotificationQueue:
    return NotificationQueue(db, clock=clock)


# -- the shape of a row ------------------------------------------------------


async def test_a_notification_is_a_row_that_outlives_the_object_that_made_it(
    tmp_path, clock: FakeClock
) -> None:
    """The whole premise: close the database, reopen it, the timer is still there."""
    path = tmp_path / "durable.db"
    first = Database(path)
    await first.start()
    await migrate(first)
    made = await NotificationQueue(first, clock=clock).raise_notification(
        "tea", kind=NotificationKind.TIMER, due_at=clock.now + timedelta(minutes=3)
    )
    await first.stop()

    second = Database(path)
    await second.start()
    try:
        reopened = await NotificationQueue(second, clock=clock).get(made.id)
        assert reopened is not None
        assert reopened.title == "tea"
        assert reopened.state is NotificationState.PENDING
        assert reopened.due_at == made.due_at
    finally:
        await second.stop()


async def test_an_overdue_notification_survives_the_device_being_off(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    """A timer whose moment passed while the Pi was unplugged still fires."""
    await queue.raise_notification(
        "kettle", kind=NotificationKind.TIMER, due_at=clock.now + timedelta(minutes=5)
    )
    clock.advance(hours=1)
    due = await queue.due()
    assert [n.title for n in due] == ["kettle"]


# -- duplicates and repeats --------------------------------------------------


async def test_raising_the_same_thing_twice_while_pending_updates_one_row(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    first = await queue.raise_notification("battery low", body="18%")
    clock.advance(seconds=30)
    second = await queue.raise_notification("battery low", body="16%")

    assert second.id == first.id
    assert second.raise_count == 2
    assert second.body == "16%"
    assert second.created_at == first.created_at
    assert await queue.pending_count() == 1


async def test_a_key_is_free_again_once_the_row_is_no_longer_pending(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    """Dedup is scoped to `pending` so an alarm may ring again tomorrow."""
    first = await queue.raise_notification("wake up", kind=NotificationKind.ALARM)
    await queue.mark_delivered(first.id)
    clock.advance(hours=24)
    second = await queue.raise_notification("wake up", kind=NotificationKind.ALARM)

    assert second.id != first.id
    assert second.raise_count == 1


async def test_the_database_itself_refuses_two_open_rows_for_one_key(
    db: Database, clock: FakeClock
) -> None:
    """The partial unique index is the rule; the Python check is the courtesy."""
    from nomad.core.errors import StorageError

    queue = NotificationQueue(db, clock=clock)
    existing = await queue.raise_notification("only one")
    with pytest.raises(StorageError):
        await db.execute(
            "INSERT INTO notifications (id, dedup_key, kind, state, title, body, "
            "created_at, due_at, raise_count, source, payload_json) "
            "VALUES ('twin', ?, 'agent', 'pending', 'only one', '', ?, ?, 1, 'test', '{}')",
            (existing.dedup_key, clock.now.isoformat(), clock.now.isoformat()),
        )


async def test_different_kinds_with_the_same_title_are_different_notifications(
    queue: NotificationQueue,
) -> None:
    timer = await queue.raise_notification("tea", kind=NotificationKind.TIMER)
    reminder = await queue.raise_notification("tea", kind=NotificationKind.REMINDER)
    assert timer.id != reminder.id
    assert await queue.pending_count() == 2


async def test_an_explicit_dedup_key_collapses_differently_worded_raises(
    queue: NotificationQueue,
) -> None:
    first = await queue.raise_notification("Disk 91% full", dedup_key="disk-space")
    second = await queue.raise_notification("Disk 94% full", dedup_key="disk-space")
    assert second.id == first.id
    assert second.title == "Disk 94% full"


def test_dedup_keys_normalize_case_and_whitespace() -> None:
    assert normalize_dedup_key("  Battery   Low ") == "battery low"
    assert default_dedup_key(NotificationKind.TIMER, "Tea") == "timer:tea"


# -- delivery ----------------------------------------------------------------


async def test_delivery_marks_the_row_and_takes_it_out_of_the_queue(
    queue: NotificationQueue,
) -> None:
    made = await queue.raise_notification("ping")
    seen: list[Notification] = []

    async def sink(notification: Notification) -> None:
        seen.append(notification)

    assert await queue.deliver_due(sink) == 1
    assert [n.title for n in seen] == ["ping"]
    assert (await queue.get(made.id)).state is NotificationState.DELIVERED
    assert await queue.pending_count() == 0


async def test_a_sink_that_raises_leaves_the_row_pending_for_the_next_poll(
    queue: NotificationQueue,
) -> None:
    """The property an event cannot have: a failed delivery is retried."""
    await queue.raise_notification("ping")

    async def broken(notification: Notification) -> None:
        raise RuntimeError("screen not ready")

    with pytest.raises(RuntimeError):
        await queue.deliver_due(broken)
    assert await queue.pending_count() == 1

    delivered: list[str] = []

    async def working(notification: Notification) -> None:
        delivered.append(notification.title)

    assert await queue.deliver_due(working) == 1
    assert delivered == ["ping"]


async def test_nothing_is_delivered_before_it_is_due(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    await queue.raise_notification("later", due_at=clock.now + timedelta(minutes=10))
    assert await queue.due() == []
    clock.advance(minutes=11)
    assert len(await queue.due()) == 1


async def test_one_poll_hands_over_at_most_the_configured_batch(
    db: Database, clock: FakeClock
) -> None:
    queue = NotificationQueue(
        db, config=NotificationsConfig(max_deliveries_per_poll=2), clock=clock
    )
    for index in range(5):
        await queue.raise_notification(f"item {index}")
    assert len(await queue.due()) == 2


# -- expiry ------------------------------------------------------------------


async def test_an_expired_notification_is_swept_rather_than_shown_late(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    made = await queue.raise_notification(
        "stale timer",
        kind=NotificationKind.TIMER,
        due_at=clock.now + timedelta(minutes=1),
        expires_at=clock.now + timedelta(minutes=10),
    )
    clock.advance(hours=2)
    assert await queue.due() == []
    assert (await queue.get(made.id)).state is NotificationState.EXPIRED


async def test_an_expiry_before_the_due_time_is_refused_at_creation(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    with pytest.raises(NotificationRefused):
        await queue.raise_notification(
            "impossible",
            due_at=clock.now + timedelta(hours=1),
            expires_at=clock.now + timedelta(minutes=1),
        )


# -- repeats -----------------------------------------------------------------


async def test_a_repeating_notification_re_arms_as_a_new_row(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    first = await queue.raise_notification(
        "stand up", due_at=clock.now, repeat_rule="interval:1800"
    )
    clock.advance(seconds=1)
    rearmed = await queue.mark_delivered(first.id)

    assert rearmed is not None
    assert rearmed.id != first.id
    assert rearmed.state is NotificationState.PENDING
    assert rearmed.due_at == first.due_at + timedelta(seconds=1800)
    assert (await queue.get(first.id)).state is NotificationState.DELIVERED


async def test_a_week_offline_produces_one_notification_not_a_backlog(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    """The catch-up rule: advance past `now`, do not replay what was missed."""
    first = await queue.raise_notification("hourly", due_at=clock.now, repeat_rule="interval:3600")
    clock.advance(days=7)
    rearmed = await queue.mark_delivered(first.id)

    assert rearmed is not None
    assert rearmed.due_at > clock.now
    assert rearmed.due_at - clock.now <= timedelta(hours=1)
    assert await queue.pending_count() == 1


async def test_a_daily_alarm_keeps_its_wall_clock_time_across_a_dst_change(
    db: Database,
) -> None:
    """+86400s would be an hour wrong for six months. This is why the rule is text."""
    # 2026-10-25 is the Sunday the UK leaves BST; 06:00 UTC is 07:00 BST.
    before = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    clock = FakeClock(before)
    queue = NotificationQueue(db, clock=clock)
    alarm = await queue.raise_notification(
        "wake up",
        kind=NotificationKind.ALARM,
        due_at=before,
        repeat_rule="daily",
        repeat_tz="Europe/London",
    )
    clock.now = before + timedelta(seconds=1)
    rearmed = await queue.mark_delivered(alarm.id)

    assert rearmed is not None
    from zoneinfo import ZoneInfo

    local = rearmed.due_at.astimezone(ZoneInfo("Europe/London"))
    assert (local.hour, local.minute) == (7, 0)
    # 25 hours of real time, because the clocks went back.
    assert rearmed.due_at - before == timedelta(hours=25)


def test_a_repeat_rule_is_validated_when_it_is_written_not_when_it_fires() -> None:
    assert parse_repeat_rule("daily") == ("daily", 0)
    assert parse_repeat_rule("interval:600") == ("interval", 600)
    with pytest.raises(NotificationRefused):
        parse_repeat_rule("every tuesday")
    with pytest.raises(NotificationRefused):
        parse_repeat_rule("interval:1")


def test_next_occurrence_never_returns_a_time_in_the_past() -> None:
    previous = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    following = next_occurrence(previous, rule="daily", now=now, tz_name="UTC")
    assert following > now


async def test_a_bad_repeat_rule_is_refused_at_creation(queue: NotificationQueue) -> None:
    with pytest.raises(NotificationRefused):
        await queue.raise_notification("nope", repeat_rule="sometimes")
    with pytest.raises(NotificationRefused):
        await queue.raise_notification("nope", repeat_rule="daily", repeat_tz="Mars/Olympus")


# -- resolution and listing --------------------------------------------------


async def test_acknowledging_a_delivered_notification_records_a_human_was_there(
    queue: NotificationQueue,
) -> None:
    made = await queue.raise_notification("ping")
    await queue.mark_delivered(made.id)
    assert await queue.acknowledge(made.id) is True
    assert (await queue.get(made.id)).state is NotificationState.ACKNOWLEDGED
    assert await queue.acknowledge(made.id) is False


async def test_cancelling_a_delivered_notification_is_refused(
    queue: NotificationQueue,
) -> None:
    """The queue does not rewrite what already happened."""
    made = await queue.raise_notification("ping")
    await queue.mark_delivered(made.id)
    assert await queue.cancel(made.id) is False


async def test_cancelling_a_repeating_alarm_stops_the_chain(
    queue: NotificationQueue,
) -> None:
    alarm = await queue.raise_notification("wake up", repeat_rule="daily")
    assert await queue.cancel(alarm.id) is True
    assert await queue.pending_count() == 0


async def test_delivering_something_twice_is_refused(queue: NotificationQueue) -> None:
    made = await queue.raise_notification("ping")
    await queue.mark_delivered(made.id)
    with pytest.raises(NotificationRefused):
        await queue.mark_delivered(made.id)


async def test_undelivered_lists_pending_rows_whether_or_not_they_are_due(
    queue: NotificationQueue, clock: FakeClock
) -> None:
    await queue.raise_notification("now")
    await queue.raise_notification("soon", due_at=clock.now + timedelta(hours=1))
    assert [n.title for n in await queue.undelivered()] == ["now", "soon"]


async def test_history_returns_what_already_happened(queue: NotificationQueue) -> None:
    made = await queue.raise_notification("ping")
    await queue.mark_delivered(made.id)
    rows = await queue.history(states=[NotificationState.DELIVERED])
    assert [n.id for n in rows] == [made.id]


# -- bookkeeping -------------------------------------------------------------


async def test_resolved_rows_are_pruned_but_pending_ones_never_are(
    db: Database, clock: FakeClock
) -> None:
    """A pending notification is a promise; the cap may only fall on the past."""
    queue = NotificationQueue(db, config=NotificationsConfig(max_history=3), clock=clock)
    for index in range(6):
        made = await queue.raise_notification(f"done {index}")
        clock.advance(seconds=1)
        await queue.mark_delivered(made.id)
    for index in range(4):
        await queue.raise_notification(f"waiting {index}")

    assert await queue.pending_count() == 4
    resolved = await queue.history(states=[NotificationState.DELIVERED], limit=50)
    assert len(resolved) <= 3


async def test_an_empty_title_is_refused(queue: NotificationQueue) -> None:
    with pytest.raises(NotificationRefused):
        await queue.raise_notification("   ")


async def test_an_oversized_title_is_refused_rather_than_truncated(
    queue: NotificationQueue,
) -> None:
    with pytest.raises(NotificationRefused):
        await queue.raise_notification("x" * 200)
