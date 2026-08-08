"""Timers and alarms, which are notifications and nothing else.

There is no timer table and there is no `asyncio.sleep` anywhere in this
module. A timer that lives in a task dies with the process, and on a handheld
whose screen is off most of the time and whose agent session is restarted by
D34's rollover, that is not an edge case — it is the normal path. So a timer is
`raise_notification(kind=TIMER, due_at=now+duration)`, an alarm is the same row
with a calendar repeat rule, and every property they need was already built
once: durability, catch-up after a reboot, dedup while pending, expiry when
firing late would be worse than silence.

What is left here is the arithmetic between what a person says and what the
queue stores — "25 minutes" to a `due_at`, "07:00 in Europe/London" to the next
instant that is actually 07:00 there. Both are pure functions of an injected
`now`, so nothing in this file reads a clock and no test that exercises it has
to wait for one.

**Alarms are anchored in a zone, not in seconds.** `07:00 daily` computed as
+86400s drifts an hour twice a year; computed as a calendar step in a named
zone it does not. The zone is required for an alarm and defaulted to UTC only
when the caller genuinely has none, because an alarm silently anchored to the
wrong zone is worse than one that refused.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nomad.core.config import UtilitiesConfig
from nomad.notifications.errors import NotificationRefused
from nomad.notifications.models import Notification, NotificationKind
from nomad.notifications.queue import NotificationQueue
from nomad.notifications.repeat import resolve_repeat_tz
from nomad.utilities.errors import UtilityError

#: A timer under this is not a timer; it is a thing the caller should just do.
MIN_TIMER_SECONDS = 1


def timer_due_at(now: datetime, seconds: float, *, config: UtilitiesConfig) -> datetime:
    """When a timer of `seconds` fires. Pure; refuses a slipped decimal point."""
    if seconds < MIN_TIMER_SECONDS:
        raise UtilityError(
            f"a timer of {seconds}s is below the {MIN_TIMER_SECONDS}s floor",
            {"seconds": seconds},
        )
    ceiling = max(1, config.max_timer_hours) * 3600
    if seconds > ceiling:
        raise UtilityError(
            f"{seconds / 3600:.1f} hours is longer than the {config.max_timer_hours}h "
            "timer ceiling — set an alarm with a date instead",
            {"seconds": seconds, "max_hours": config.max_timer_hours},
        )
    return now + timedelta(seconds=seconds)


def next_wall_clock(
    now: datetime, *, hour: int, minute: int, tz_name: str | None
) -> datetime:
    """The next instant that reads `hour:minute` in `tz_name`, strictly future.

    Computed on the wall clock and then converted, never by adding seconds:
    that is what makes "07:00" survive the day the clocks change, and it is the
    single reason alarms are not just timers with a long duration.
    """
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise UtilityError(
            f"{hour:02d}:{minute:02d} is not a time of day", {"hour": hour, "minute": minute}
        )
    zone = resolve_repeat_tz(tz_name)
    local = now.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = (candidate.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=zone)
    return candidate.astimezone(now.tzinfo or zone)


async def start_timer(
    queue: NotificationQueue,
    *,
    label: str,
    seconds: float,
    now: datetime,
    config: UtilitiesConfig,
    body: str = "",
) -> Notification:
    """One-shot, durable, and delivered late rather than lost.

    The expiry window is the timer's own duration again, floored at the
    configured default: a five-minute timer noticed six hours later is noise,
    but a five-minute timer noticed ninety seconds late is still the answer.
    """
    due = timer_due_at(now, seconds, config=config)
    return await queue.raise_notification(
        label,
        body=body or f"{label} — {seconds / 60:.0f} min timer",
        kind=NotificationKind.TIMER,
        due_at=due,
        expires_at=due + timedelta(seconds=max(seconds, 300)),
        dedup_key=f"timer:{label.casefold().strip()}",
        source="utilities",
        payload={"seconds": str(seconds)},
    )


async def set_alarm(
    queue: NotificationQueue,
    *,
    label: str,
    hour: int,
    minute: int,
    now: datetime,
    tz_name: str | None = None,
    repeat: str | None = None,
    body: str = "",
) -> Notification:
    """A wall-clock alarm, optionally repeating on a calendar rule.

    Never given an `expires_at`. A timer that fired unseen is stale; an alarm
    that fired unseen is the thing the operator most needs to find out about,
    and sweeping it would be the device deciding the missed wake-up did not
    happen.
    """
    if repeat is not None and repeat.strip().casefold() not in ("daily", "weekly"):
        raise UtilityError(
            f"an alarm repeats 'daily' or 'weekly', not '{repeat}'. For a fixed "
            "interval use a repeating reminder instead.",
            {"repeat": repeat},
        )
    try:
        due = next_wall_clock(now, hour=hour, minute=minute, tz_name=tz_name)
    except NotificationRefused as exc:
        raise UtilityError(str(exc), exc.details) from exc
    return await queue.raise_notification(
        label,
        body=body or f"Alarm {hour:02d}:{minute:02d}",
        kind=NotificationKind.ALARM,
        due_at=due,
        dedup_key=f"alarm:{label.casefold().strip()}",
        repeat_rule=repeat.strip().casefold() if repeat else None,
        repeat_tz=tz_name,
        source="utilities",
        payload={"hour": str(hour), "minute": str(minute)},
    )
