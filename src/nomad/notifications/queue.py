"""The durable queue: raise, claim, deliver, expire (D6, D7, D38).

**Why a table and not the bus.** D6 gives every subscriber a bounded queue and
drops the slow ones, so that a stalled client can never freeze the display or
the agent loop. That is the right trade for events and the wrong one for a
timer: Nomad's screen is dark most of the time and its process restarts, so a
"tea is ready" published to nobody has not been missed, it has ceased to exist.
Everything here is therefore a row that survives a reboot, and delivery is a
state transition performed by whatever is finally watching.

**Delivery is claimed, not fired.** `deliver_due` hands each row to a sink and
only marks it delivered once the sink returns. A sink that raises leaves the
row `PENDING`, so the next poll retries it — which is the entire property the
bus cannot offer, and the reason the sink is a parameter rather than a publish
call inside this module. The queue depends on `core` and `storage` and nothing
else; it does not know what a screen or a speaker is.

**Repeats re-arm forward, never backward.** A fired repeating notification is
terminal and a *new* pending row is inserted for the next occurrence, computed
past `now` (see `repeat.py`). A device off for a week wakes to one reminder and
a correctly armed next one, not to a week of backlog racing onto the screen.

**Who runs it.** The queue is passive and holds no task. D38 names the
notification queue as `INTERACTIVE` — never preempted — but registering it is
the composition root's job, because a workload registration here would put a
resource policy underneath storage. Polling `deliver_due` from an interactive
component is the whole of the wiring.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from nomad.core.config import NotificationsConfig
from nomad.core.logging import get_logger
from nomad.notifications.errors import NotificationRefused
from nomad.notifications.models import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    Notification,
    NotificationKind,
    NotificationState,
    default_dedup_key,
    normalize_dedup_key,
)
from nomad.notifications.repeat import next_occurrence, parse_repeat_rule, resolve_repeat_tz
from nomad.storage.db import Database

logger = get_logger(__name__)

#: What `deliver_due` calls for each claimed row. Async so a sink may draw,
#: speak, or write; returning normally is what marks the row delivered.
NotificationSink = Callable[[Notification], Awaitable[None]]

_COLUMNS = (
    "id, dedup_key, kind, state, title, body, created_at, due_at, expires_at, "
    "delivered_at, resolved_at, repeat_rule, repeat_tz, raise_count, source, payload_json"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_notification(row: dict[str, Any]) -> Notification:
    return Notification(
        id=row["id"],
        dedup_key=row["dedup_key"],
        kind=NotificationKind(row["kind"]),
        state=NotificationState(row["state"]),
        title=row["title"],
        body=row["body"],
        created_at=datetime.fromisoformat(row["created_at"]),
        due_at=datetime.fromisoformat(row["due_at"]),
        expires_at=_parse(row["expires_at"]),
        delivered_at=_parse(row["delivered_at"]),
        resolved_at=_parse(row["resolved_at"]),
        repeat_rule=row["repeat_rule"],
        repeat_tz=row["repeat_tz"],
        raise_count=int(row["raise_count"]),
        source=row["source"],
        payload=dict(json.loads(row["payload_json"])),
    )


class NotificationQueue:
    """Durable, deduplicated notifications over a `Database`."""

    def __init__(
        self,
        db: Database,
        *,
        config: NotificationsConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config = config or NotificationsConfig()
        self._clock = clock or _now

    @property
    def config(self) -> NotificationsConfig:
        return self._config

    def now(self) -> datetime:
        """The queue's notion of now, so callers share its injected clock.

        Public because timers and alarms compute a `due_at` *before* handing it
        here, and a caller reading the wall clock while the queue is on a test
        clock produces a row that is due in 1970.
        """
        return self._clock()

    # -- writing -----------------------------------------------------------

    async def raise_notification(
        self,
        title: str,
        *,
        body: str = "",
        kind: NotificationKind = NotificationKind.AGENT,
        due_at: datetime | None = None,
        expires_at: datetime | None = None,
        dedup_key: str | None = None,
        repeat_rule: str | None = None,
        repeat_tz: str | None = None,
        source: str = "agent",
        payload: dict[str, str] | None = None,
    ) -> Notification:
        """Store one notification, or restate the pending one that is already it.

        Restating never inserts a twin: the open row keeps its `id` and
        `created_at`, takes the new text and timing, and bumps `raise_count`.
        A caller that polls a condition every thirty seconds therefore produces
        one loud row rather than a queue nobody will read to the bottom of.
        """
        clean_title = " ".join(title.split())
        if not clean_title:
            raise NotificationRefused("refusing to raise a notification with no title")
        if len(clean_title) > MAX_TITLE_CHARS:
            raise NotificationRefused(
                f"title is {len(clean_title)} characters; the limit is {MAX_TITLE_CHARS}. "
                "A notification is a line on a small screen — put the detail in the body.",
                {"length": len(clean_title), "limit": MAX_TITLE_CHARS},
            )
        clean_body = body.strip()[:MAX_BODY_CHARS]

        if repeat_rule is not None:
            parse_repeat_rule(repeat_rule)
            resolve_repeat_tz(repeat_tz)

        now = self._clock()
        due = due_at or now
        if expires_at is not None and expires_at <= due:
            raise NotificationRefused(
                "this notification expires before it is due, so it could never be "
                "delivered",
                {"due_at": due.isoformat(), "expires_at": expires_at.isoformat()},
            )

        key = normalize_dedup_key(dedup_key) if dedup_key else default_dedup_key(kind, clean_title)
        existing = await self._find_open(key)
        if existing is not None:
            return await self._restate(
                existing,
                title=clean_title,
                body=clean_body,
                due_at=due,
                expires_at=expires_at,
                repeat_rule=repeat_rule,
                repeat_tz=repeat_tz,
                payload=payload,
            )

        notification = Notification(
            id=uuid.uuid4().hex,
            dedup_key=key,
            kind=kind,
            state=NotificationState.PENDING,
            title=clean_title,
            body=clean_body,
            created_at=now,
            due_at=due,
            expires_at=expires_at,
            repeat_rule=repeat_rule,
            repeat_tz=repeat_tz,
            source=source,
            payload=payload or {},
        )
        await self._insert(notification)
        await self._prune_history()
        logger.info(
            "Raised a notification",
            extra={
                "notification_id": notification.id,
                "kind": str(kind),
                "due_at": due.isoformat(),
            },
        )
        return notification

    async def _insert(self, notification: Notification) -> None:
        await self._db.execute(
            f"INSERT INTO notifications ({_COLUMNS}) "  # noqa: S608 - fixed column list
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notification.id,
                notification.dedup_key,
                str(notification.kind),
                str(notification.state),
                notification.title,
                notification.body,
                notification.created_at.isoformat(),
                notification.due_at.isoformat(),
                notification.expires_at.isoformat() if notification.expires_at else None,
                notification.delivered_at.isoformat() if notification.delivered_at else None,
                notification.resolved_at.isoformat() if notification.resolved_at else None,
                notification.repeat_rule,
                notification.repeat_tz,
                notification.raise_count,
                notification.source,
                json.dumps(notification.payload),
            ),
        )

    async def _restate(
        self,
        existing: Notification,
        *,
        title: str,
        body: str,
        due_at: datetime,
        expires_at: datetime | None,
        repeat_rule: str | None,
        repeat_tz: str | None,
        payload: dict[str, str] | None,
    ) -> Notification:
        """Fold a repeat raise into the open row. Never inserts, never resets.

        `created_at` is kept — this is the same notice, first raised then — and
        `due_at` moves to the new value so that re-raising a snoozed timer is
        how a caller reschedules it. A restatement that omits the repeat rule
        leaves the existing one alone rather than silently disarming an alarm.
        """
        updated = existing.model_copy(
            update={
                "title": title,
                "body": body or existing.body,
                "due_at": due_at,
                "expires_at": expires_at if expires_at is not None else existing.expires_at,
                "repeat_rule": repeat_rule if repeat_rule is not None else existing.repeat_rule,
                "repeat_tz": repeat_tz if repeat_tz is not None else existing.repeat_tz,
                "raise_count": existing.raise_count + 1,
                "payload": payload if payload is not None else existing.payload,
            }
        )
        await self._db.execute(
            "UPDATE notifications SET title = ?, body = ?, due_at = ?, expires_at = ?, "
            "repeat_rule = ?, repeat_tz = ?, raise_count = ?, payload_json = ? WHERE id = ?",
            (
                updated.title,
                updated.body,
                updated.due_at.isoformat(),
                updated.expires_at.isoformat() if updated.expires_at else None,
                updated.repeat_rule,
                updated.repeat_tz,
                updated.raise_count,
                json.dumps(updated.payload),
                updated.id,
            ),
        )
        logger.debug(
            "Restated an open notification",
            extra={"notification_id": updated.id, "raise_count": updated.raise_count},
        )
        return updated

    async def mark_delivered(self, notification_id: str) -> Notification | None:
        """Record that something showed it, and re-arm it if it repeats.

        Returns the *new* pending row for a repeating notification, or `None`
        for a one-shot. Returning the re-armed row rather than a bool is what
        lets a caller log "next at 07:00" without a second query.
        """
        now = self._clock()
        existing = await self.get(notification_id)
        if existing is None:
            return None
        if existing.state is not NotificationState.PENDING:
            raise NotificationRefused(
                f"notification {notification_id} is already {existing.state}",
                {"state": str(existing.state)},
            )
        await self._db.execute(
            "UPDATE notifications SET state = ?, delivered_at = ?, resolved_at = ? WHERE id = ?",
            (
                str(NotificationState.DELIVERED),
                now.isoformat(),
                now.isoformat(),
                notification_id,
            ),
        )
        if not existing.repeat_rule:
            return None
        following = next_occurrence(
            existing.due_at, rule=existing.repeat_rule, now=now, tz_name=existing.repeat_tz
        )
        rearmed = existing.model_copy(
            update={
                "id": uuid.uuid4().hex,
                "state": NotificationState.PENDING,
                "created_at": now,
                "due_at": following,
                "delivered_at": None,
                "resolved_at": None,
                "raise_count": 1,
                # A window that was relative to the old due time has to move
                # with it, or a re-armed alarm inherits an expiry in the past
                # and is swept before it can ring.
                "expires_at": (
                    following + (existing.expires_at - existing.due_at)
                    if existing.expires_at is not None
                    else None
                ),
            }
        )
        await self._insert(rearmed)
        logger.info(
            "Re-armed a repeating notification",
            extra={"notification_id": rearmed.id, "due_at": following.isoformat()},
        )
        return rearmed

    async def acknowledge(self, notification_id: str) -> bool:
        """The operator dismissed it. False if unknown or already acknowledged.

        Allowed from `DELIVERED` as well as `PENDING`, because acknowledging
        something that was shown *is* the normal case, and it is the only
        transition that records a human was actually present (D38's reason for
        putting this queue in the interactive tier).
        """
        return await self._resolve(
            notification_id,
            NotificationState.ACKNOWLEDGED,
            allowed_from=(NotificationState.PENDING, NotificationState.DELIVERED),
        )

    async def cancel(self, notification_id: str) -> bool:
        """Called off before it fired — a timer stopped, an alarm turned off.

        Only from `PENDING`. Cancelling something already delivered would be a
        claim about the past, and the queue does not rewrite what happened.
        """
        return await self._resolve(
            notification_id,
            NotificationState.CANCELLED,
            allowed_from=(NotificationState.PENDING,),
        )

    async def _resolve(
        self,
        notification_id: str,
        state: NotificationState,
        *,
        allowed_from: tuple[NotificationState, ...],
    ) -> bool:
        existing = await self.get(notification_id)
        if existing is None or existing.state not in allowed_from:
            return False
        now = self._clock().isoformat()
        await self._db.execute(
            "UPDATE notifications SET state = ?, resolved_at = ? WHERE id = ?",
            (str(state), now, notification_id),
        )
        return True

    async def expire_overdue(self) -> int:
        """Sweep pending rows whose window has closed. Returns how many.

        Showing a timer forty minutes late is worse than not showing it, and a
        queue that never forgets is a queue whose first screen is archaeology.
        """
        now = self._clock().isoformat()
        rows = await self._db.fetch_all(
            "SELECT id FROM notifications WHERE state = ? AND expires_at IS NOT NULL "
            "AND expires_at <= ?",
            (str(NotificationState.PENDING), now),
        )
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in rows)
        await self._db.execute(
            f"UPDATE notifications SET state = ?, resolved_at = ? WHERE id IN ({placeholders})",  # noqa: S608
            (str(NotificationState.EXPIRED), now, *[row["id"] for row in rows]),
        )
        logger.info("Expired overdue notifications", extra={"count": len(rows)})
        return len(rows)

    # -- reading -----------------------------------------------------------

    async def get(self, notification_id: str) -> Notification | None:
        row = await self._db.fetch_one(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        )
        return _row_to_notification(row) if row is not None else None

    async def due(self, *, limit: int | None = None) -> list[Notification]:
        """Pending rows whose time has come, oldest first.

        Overdue rows are swept first, so a notification is never handed over
        after its own expiry just because nothing else called `expire_overdue`.
        """
        await self.expire_overdue()
        now = self._clock().isoformat()
        size = max(1, limit if limit is not None else self._config.max_deliveries_per_poll)
        rows = await self._db.fetch_all(
            "SELECT * FROM notifications WHERE state = ? AND due_at <= ? "
            "ORDER BY due_at ASC, created_at ASC LIMIT ?",
            (str(NotificationState.PENDING), now, size),
        )
        return [_row_to_notification(row) for row in rows]

    async def undelivered(self, *, limit: int | None = None) -> list[Notification]:
        """Every pending row, due or not — the "what is waiting" read."""
        size = max(1, limit if limit is not None else self._config.list_limit)
        rows = await self._db.fetch_all(
            "SELECT * FROM notifications WHERE state = ? ORDER BY due_at ASC LIMIT ?",
            (str(NotificationState.PENDING), size),
        )
        return [_row_to_notification(row) for row in rows]

    async def history(
        self,
        *,
        states: Iterable[NotificationState] | None = None,
        limit: int | None = None,
    ) -> list[Notification]:
        """Recent rows in any state, newest first. The audit read."""
        size = max(1, limit if limit is not None else self._config.list_limit)
        wanted: Sequence[str] = [str(s) for s in (states or list(NotificationState))]
        placeholders = ", ".join("?" for _ in wanted)
        rows = await self._db.fetch_all(
            f"SELECT * FROM notifications WHERE state IN ({placeholders}) "  # noqa: S608
            "ORDER BY created_at DESC, id ASC LIMIT ?",
            (*wanted, size),
        )
        return [_row_to_notification(row) for row in rows]

    async def pending_count(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM notifications WHERE state = ?",
            (str(NotificationState.PENDING),),
        )
        return int(row["n"]) if row else 0

    # -- delivery ----------------------------------------------------------

    async def deliver_due(self, sink: NotificationSink, *, limit: int | None = None) -> int:
        """Hand every due notification to `sink`, marking each one after it returns.

        A sink that raises leaves its row `PENDING` and stops the batch. That is
        deliberate and it is the whole difference from an event: a screen that
        was not ready gets the notification on the next poll instead of losing
        it. Remaining rows are left alone rather than delivered out of order to
        a sink that has just demonstrated it is not working.
        """
        delivered = 0
        for notification in await self.due(limit=limit):
            await sink(notification)
            await self.mark_delivered(notification.id)
            delivered += 1
        return delivered

    # -- internals ---------------------------------------------------------

    async def _find_open(self, dedup_key: str) -> Notification | None:
        row = await self._db.fetch_one(
            "SELECT * FROM notifications WHERE dedup_key = ? AND state = ?",
            (dedup_key, str(NotificationState.PENDING)),
        )
        return _row_to_notification(row) if row is not None else None

    async def _prune_history(self) -> None:
        """Keep resolved rows bounded. Pending rows are never pruned.

        Bookkeeping, not forgetting: a pending notification is a promise the
        device made, so the cap can only ever fall on rows that already
        happened.
        """
        limit = max(1, self._config.max_history)
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM notifications WHERE state != ?",
            (str(NotificationState.PENDING),),
        )
        surplus = (int(row["n"]) if row else 0) - limit
        if surplus <= 0:
            return
        await self._db.execute(
            "DELETE FROM notifications WHERE id IN ("
            "  SELECT id FROM notifications WHERE state != ? "
            "  ORDER BY COALESCE(resolved_at, created_at) ASC LIMIT ?"
            ")",
            (str(NotificationState.PENDING), surplus),
        )
        logger.info("Pruned resolved notifications", extra={"count": surplus})
