"""Where the next occurrence of a repeating notification lands, and the two
questions that answer has to get right.

**A daily alarm is not 86400 seconds.** Fixed-interval arithmetic drifts an
hour twice a year, and an alarm clock that is an hour wrong for six months is
not an alarm clock. So `daily` and `weekly` are computed as *calendar* steps in
an explicit zone: take the wall-clock reading, add a day to the date, and
re-attach the zone, which is what makes 07:00 stay 07:00 across a DST
transition. `interval:<seconds>` remains available and means exactly what it
says — for anything that genuinely is a duration, like "every 25 minutes".

**A device that was off for a week must not fire a week of backlog.** The
catch-up rule is that a repeat advances until it is in the future, and then
stops. Eleven missed hourly reminders produce one notification and a correctly
armed twelfth, not eleven rows racing each other onto the screen. This is the
behaviour that separates a durable queue from a replayed log, and it is the
reason the advance loop is here rather than inlined at the call site where the
next reader would simplify it into a single addition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nomad.notifications.errors import NotificationRefused

#: Ceiling on the catch-up loop. A rule that has not reached the present after
#: this many steps is a rule with a degenerate interval, and grinding through
#: millions of iterations on a Pi is a worse failure than refusing.
_MAX_ADVANCE_STEPS = 4000

#: Smallest interval a repeat may declare. Below this a "repeating" timer is a
#: busy loop wearing a notification's clothes.
MIN_INTERVAL_SECONDS = 5

DAILY = "daily"
WEEKLY = "weekly"
INTERVAL = "interval"
_INTERVAL_PREFIX = f"{INTERVAL}:"


def parse_repeat_rule(rule: str) -> tuple[str, int]:
    """Validate a rule, returning `(kind, seconds)` with seconds 0 for calendar rules.

    Parsing is separated from applying so a bad rule is refused at *creation*.
    A rule that only fails when the alarm is due fails at 07:00, silently, to
    an operator who is asleep.
    """
    cleaned = rule.strip().casefold()
    if cleaned in (DAILY, WEEKLY):
        return cleaned, 0
    if cleaned.startswith(_INTERVAL_PREFIX):
        raw = cleaned[len(_INTERVAL_PREFIX) :].strip()
        try:
            seconds = int(raw)
        except ValueError as exc:
            raise NotificationRefused(
                f"'{rule}' is not a repeat rule; use 'daily', 'weekly' or 'interval:<seconds>'",
                {"rule": rule},
            ) from exc
        if seconds < MIN_INTERVAL_SECONDS:
            raise NotificationRefused(
                f"a repeat interval of {seconds}s is below the {MIN_INTERVAL_SECONDS}s "
                "floor; that is a loop, not a reminder",
                {"rule": rule, "min_seconds": MIN_INTERVAL_SECONDS},
            )
        return INTERVAL, seconds
    raise NotificationRefused(
        f"'{rule}' is not a repeat rule; use 'daily', 'weekly' or 'interval:<seconds>'",
        {"rule": rule},
    )


def resolve_repeat_tz(name: str | None) -> tzinfo:
    """The zone a calendar repeat is anchored in. `None` means UTC.

    UTC is the honest default rather than the system zone: the composition root
    knows where the operator is and the queue does not, and a fallback that
    quietly reads `TZ` would make the same alarm behave differently on a laptop
    and on the device.
    """
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NotificationRefused(
            f"unknown timezone '{name}' for a repeating notification", {"timezone": name}
        ) from exc


def _step(current: datetime, kind: str, seconds: int, zone: tzinfo) -> datetime:
    if kind == INTERVAL:
        return current + timedelta(seconds=seconds)
    local = current.astimezone(zone)
    days = 1 if kind == DAILY else 7
    # Re-attach the zone to the *wall clock* reading rather than adding a
    # duration, so 07:00 survives a DST transition as 07:00.
    naive_next = local.replace(tzinfo=None) + timedelta(days=days)
    return naive_next.replace(tzinfo=zone).astimezone(UTC)


def next_occurrence(
    previous_due: datetime,
    *,
    rule: str,
    now: datetime,
    tz_name: str | None = None,
) -> datetime:
    """The first occurrence strictly after `now`, starting from `previous_due`.

    Never returns a time in the past, and never returns the backlog it skipped.
    """
    kind, seconds = parse_repeat_rule(rule)
    zone = resolve_repeat_tz(tz_name)
    candidate = previous_due
    for _ in range(_MAX_ADVANCE_STEPS):
        candidate = _step(candidate, kind, seconds, zone)
        if candidate > now:
            return candidate
    raise NotificationRefused(
        f"repeat rule '{rule}' did not reach the present in {_MAX_ADVANCE_STEPS} steps",
        {"rule": rule},
    )
