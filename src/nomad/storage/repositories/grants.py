"""Authorization grants and pending authorizations (D4, D14).

Every decision, grant, and pending authorization is persisted so the whole
permission pipeline is auditable regardless of which mode produced it.

Layering note: this module defines its own row-shaped models (as
`conversations.py` does) rather than importing the domain objects from
`nomad.tools.permissions`. Storage sits below tools; an import in that
direction would invert the dependency graph. The tools layer converts.

Decisions have no table of their own in migration 001 — they are persisted as
events, which is exactly the audit trail `ARCHITECTURE.md` describes. The
broker builds one `Event` per decision, publishes it on the bus, and passes
the same object to `record_decision`; the insert is `INSERT OR IGNORE` keyed
on the event id, so a bus-to-storage subscriber writing the same event is
harmless.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from nomad.core.events import Event
from nomad.storage.db import Database
from nomad.storage.repositories.events import EventsRepository

GrantSourceValue = Literal["auto", "human", "session", "model"]
ResolutionValue = Literal["approved", "denied", "expired", "timeout"]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class GrantRecord(BaseModel):
    """One row of `grants` — the persisted form of an `AuthorizationGrant`."""

    id: str
    session_id: str
    turn_id: str | None
    tool: str
    target: str
    scope: str | None
    source: GrantSourceValue
    granted_at: datetime
    expires_at: datetime | None
    used_at: datetime | None


class PendingAuthorizationRecord(BaseModel):
    """One row of `pending_authorizations` — a prompt awaiting a human."""

    id: str
    session_id: str
    turn_id: str | None
    tool: str
    target: str
    params: dict[str, Any]
    risk: str
    created_at: datetime
    expires_at: datetime | None
    resolved_at: datetime | None
    resolution: ResolutionValue | None


def _grant_from_row(row: dict[str, Any]) -> GrantRecord:
    return GrantRecord(
        id=row["id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        tool=row["tool"],
        target=row["target"],
        scope=row["scope"],
        source=row["source"],
        granted_at=datetime.fromisoformat(row["granted_at"]),
        expires_at=_parse_dt(row["expires_at"]),
        used_at=_parse_dt(row["used_at"]),
    )


def _pending_from_row(row: dict[str, Any]) -> PendingAuthorizationRecord:
    return PendingAuthorizationRecord(
        id=row["id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        tool=row["tool"],
        target=row["target"],
        params=json.loads(row["params_json"]),
        risk=row["risk"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_parse_dt(row["expires_at"]),
        resolved_at=_parse_dt(row["resolved_at"]),
        resolution=row["resolution"],
    )


class GrantsRepository:
    """Persistence for the D4 pipeline: decisions, grants, pending prompts."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._events = EventsRepository(db)

    # -- decisions ---------------------------------------------------------

    async def record_decision(self, event: Event) -> None:
        """Persist a decision event. Idempotent on the event id."""
        await self._events.append(event)

    # -- grants ------------------------------------------------------------

    async def save_grant(self, grant: GrantRecord) -> None:
        await self._db.execute(
            "INSERT INTO grants "
            "(id, session_id, turn_id, tool, target, scope, source, granted_at, "
            " expires_at, used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant.id,
                grant.session_id,
                grant.turn_id,
                grant.tool,
                grant.target,
                grant.scope,
                grant.source,
                grant.granted_at.isoformat(),
                _iso(grant.expires_at),
                _iso(grant.used_at),
            ),
        )

    async def get_grant(self, grant_id: str) -> GrantRecord | None:
        row = await self._db.fetch_one("SELECT * FROM grants WHERE id = ?", (grant_id,))
        return _grant_from_row(row) if row else None

    async def mark_grant_used(self, grant_id: str, used_at: datetime) -> None:
        await self._db.execute(
            "UPDATE grants SET used_at = COALESCE(used_at, ?) WHERE id = ?",
            (used_at.isoformat(), grant_id),
        )

    async def find_session_grants(
        self,
        *,
        session_id: str,
        tool: str,
        target: str,
        scope: str | None,
        now: datetime,
    ) -> list[GrantRecord]:
        """Standing session grants matching `(tool, target, scope)` exactly (D14).

        The scope is part of the key on purpose: approving `write_file` under
        the workspace must not approve it outside.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM grants WHERE session_id = ? AND tool = ? AND target = ? "
            "AND scope IS ? AND source = 'session' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY granted_at DESC",
            (session_id, tool, target, scope, now.isoformat()),
        )
        return [_grant_from_row(row) for row in rows]

    async def list_grants(self, session_id: str, *, limit: int = 200) -> list[GrantRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM grants WHERE session_id = ? ORDER BY granted_at DESC LIMIT ?",
            (session_id, limit),
        )
        return [_grant_from_row(row) for row in rows]

    async def revoke_session_grants(self, session_id: str, *, now: datetime) -> int:
        """Expire every standing session grant. Used on a mode downgrade."""
        rows = await self._db.fetch_all(
            "SELECT id FROM grants WHERE session_id = ? AND source = 'session' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (session_id, now.isoformat()),
        )
        await self._db.execute(
            "UPDATE grants SET expires_at = ? WHERE session_id = ? AND source = 'session' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (now.isoformat(), session_id, now.isoformat()),
        )
        return len(rows)

    # -- pending authorizations -------------------------------------------

    async def create_pending(self, pending: PendingAuthorizationRecord) -> None:
        await self._db.execute(
            "INSERT INTO pending_authorizations "
            "(id, session_id, turn_id, tool, target, params_json, risk, created_at, "
            " expires_at, resolved_at, resolution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pending.id,
                pending.session_id,
                pending.turn_id,
                pending.tool,
                pending.target,
                json.dumps(pending.params),
                pending.risk,
                pending.created_at.isoformat(),
                _iso(pending.expires_at),
                _iso(pending.resolved_at),
                pending.resolution,
            ),
        )

    async def get_pending(self, pending_id: str) -> PendingAuthorizationRecord | None:
        row = await self._db.fetch_one(
            "SELECT * FROM pending_authorizations WHERE id = ?", (pending_id,)
        )
        return _pending_from_row(row) if row else None

    async def resolve_pending(
        self, pending_id: str, resolution: ResolutionValue, resolved_at: datetime
    ) -> None:
        await self._db.execute(
            "UPDATE pending_authorizations SET resolution = ?, resolved_at = ? "
            "WHERE id = ? AND resolution IS NULL",
            (resolution, resolved_at.isoformat(), pending_id),
        )

    async def list_unresolved(self, session_id: str) -> list[PendingAuthorizationRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM pending_authorizations WHERE session_id = ? AND resolution IS NULL "
            "ORDER BY created_at ASC",
            (session_id,),
        )
        return [_pending_from_row(row) for row in rows]

    # -- session mode ------------------------------------------------------

    async def set_session_mode(self, session_id: str, mode: str) -> None:
        """Persist a runtime permission-mode switch (D14).

        This lives here rather than in `ConversationsRepository` only because
        that module predates the runtime mode switch and belongs to another
        chunk. It is a candidate to move once ownership allows.
        """
        await self._db.execute(
            "UPDATE sessions SET mode = ? WHERE id = ?", (mode, session_id)
        )

    async def get_session_mode(self, session_id: str) -> str | None:
        row = await self._db.fetch_one("SELECT mode FROM sessions WHERE id = ?", (session_id,))
        return str(row["mode"]) if row else None
