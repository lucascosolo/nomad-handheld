"""When to start a fresh backend session, and why memory makes that safe.

Nomad's premise is one session that stays alive as long as the device has
power. Taken literally that means a `--session-id` resumed continuously for
six months: a transcript nobody has ever tested at that length, growing more
expensive to compact every week, and eventually the reason the device "feels
broken" with no single moment where it broke.

Rollover is the answer, and it only became affordable once there was a memory
to carry across. The old transcript is *not* replayed into the new session —
that would reintroduce exactly the growth being escaped. The briefing is, and
the rest stays on disk where `recall` can reach it.

`should_roll` is pure and takes its clock as an argument, so both thresholds
and the boundary just under them are testable without waiting a week.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from nomad.core.config import MemoryConfig


class RolloverDecision(BaseModel):
    """Whether to roll, and the reason recorded in the event log."""

    roll: bool
    reason: str


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising mid-turn."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def should_roll(
    *,
    started_at: datetime,
    turn_count: int,
    now: datetime,
    config: MemoryConfig,
) -> RolloverDecision:
    """Decide at a turn boundary whether this backend session has run long enough.

    Either threshold set to zero or below disables that half of the policy, so
    an operator can keep one session forever if they mean to — deliberately,
    in config, rather than by the absence of a policy.
    """
    if config.session_max_turns > 0 and turn_count >= config.session_max_turns:
        return RolloverDecision(
            roll=True,
            reason=f"turn count {turn_count} reached the limit of {config.session_max_turns}",
        )

    if config.session_max_age_hours > 0:
        age = _as_utc(now) - _as_utc(started_at)
        if age >= timedelta(hours=config.session_max_age_hours):
            hours = age.total_seconds() / 3600.0
            return RolloverDecision(
                roll=True,
                reason=(
                    f"session age {hours:.1f}h reached the limit of "
                    f"{config.session_max_age_hours}h"
                ),
            )

    return RolloverDecision(roll=False, reason="within session limits")
