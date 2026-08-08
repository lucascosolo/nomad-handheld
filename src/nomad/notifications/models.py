"""What a notification is, and why it is a row rather than an event.

`EventBus` drops slow subscribers on purpose (D6) — a stalled WebSocket client
must never be able to freeze the display or the agent loop. That decision is
right, and it is also exactly why a notification cannot be an event. Nomad's
screen is off most of the time and its process restarts; a timer published to a
bus that nobody is subscribed to has not fired, it has evaporated. So the unit
here is a *persisted row with a state*, and "delivery" is a transition that
something later performs, not a side effect of publishing.

The identity rule follows from the same place. A session alive for weeks will
raise "battery low" every poll, and a queue that accumulates two hundred
unread copies of one fact is a queue the operator stops reading. `dedup_key` is
what makes two raises of the same thing one row — but only while that row is
still *pending*. Once it has been shown, the key is free again, because an
alarm that already went off this morning must still be allowed to go off
tomorrow. That single scoping choice is what lets one table serve both a
deduplicated status notice and a repeating alarm.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

_WHITESPACE = re.compile(r"\s+")

#: A notification is a line on a small screen, not a document. Anything longer
#: is truncated at the edge rather than at the point of display, so no consumer
#: has to invent its own ceiling and disagree with the next one.
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 500


class NotificationKind(StrEnum):
    """What raised this, which is also what the operator expects it to sound like.

    Kept to five for the same reason `MemoryKind` is kept to four: enough for
    the caller to file something honestly, few enough that it does not spend a
    turn choosing. `TIMER` and `ALARM` are separate because they expire
    differently — a timer that fired an hour ago is stale, an alarm is not.
    """

    TIMER = "timer"
    ALARM = "alarm"
    REMINDER = "reminder"
    SYSTEM = "system"
    AGENT = "agent"


class NotificationState(StrEnum):
    """Where a notification is in its one-way life.

    Exactly one of these is *open*: `PENDING`. Everything else is terminal for
    that row. The database enforces "at most one open row per `dedup_key`" with
    a partial unique index over this column, so the dedup rule cannot drift
    away from the code that thinks it is applying it.
    """

    PENDING = "pending"
    #: Handed to something that was watching. Terminal: a repeating
    #: notification re-arms by inserting a *new* pending row, never by
    #: reopening this one, so the record of what fired when stays intact.
    DELIVERED = "delivered"
    #: The operator saw it and dismissed it. Strictly better news than
    #: `DELIVERED`, and the only state that says a human was actually present.
    ACKNOWLEDGED = "acknowledged"
    #: Its `expires_at` passed while it was still pending. Nobody was watching
    #: in time; showing it now would be worse than not showing it.
    EXPIRED = "expired"
    CANCELLED = "cancelled"


#: The states that still occupy a `dedup_key`. Deliberately a one-element set
#: with a name: the partial unique index in migration 003 hard-codes the same
#: predicate, and this constant is where a reader finds out why.
OPEN_STATES: frozenset[NotificationState] = frozenset({NotificationState.PENDING})


class Notification(BaseModel):
    """One durable notification, as stored and as handed back."""

    id: str
    #: Identity. Two raises with the same key, while one is still pending, are
    #: the same notification restated — never a second row.
    dedup_key: str
    kind: NotificationKind = NotificationKind.AGENT
    state: NotificationState = NotificationState.PENDING
    title: str = Field(max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    created_at: datetime
    #: When this becomes deliverable. In the past means "now" — a device that
    #: was powered off through a timer's due time fires it at boot rather than
    #: discarding it, which is the whole reason the row is durable.
    due_at: datetime
    #: After this, delivering it is worse than dropping it. `None` means never.
    expires_at: datetime | None = None
    delivered_at: datetime | None = None
    resolved_at: datetime | None = None
    #: `daily`, `weekly`, or `interval:<seconds>`. `None` is a one-shot.
    repeat_rule: str | None = None
    #: The zone a calendar repeat is anchored in. An alarm set for 07:00 means
    #: 07:00 where the operator is, across a DST boundary, which fixed seconds
    #: cannot express.
    repeat_tz: str | None = None
    #: How many times this exact row was re-raised while pending. Visible so a
    #: chatty caller shows up as one loud row rather than as silence.
    raise_count: int = 1
    source: str = "agent"
    payload: dict[str, str] = Field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


def normalize_dedup_key(text: str) -> str:
    """Fold a key to its identity: casefolded, single-spaced, trimmed.

    Same shape as `memory.models.normalize_text` and for the same reason: the
    UNIQUE constraint compares these strings, so whatever this function does
    *is* the dedup rule, and it belongs next to the model rather than inside
    whichever call site happened to need it first.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def default_dedup_key(kind: NotificationKind, title: str) -> str:
    """The key a caller gets when it does not supply one.

    Kind-scoped so a timer called "tea" and a reminder called "tea" are two
    things. Callers that want a repeating alarm to collapse across restatements
    should pass their own stable key instead of relying on the title.
    """
    return f"{kind}:{normalize_dedup_key(title)}"
