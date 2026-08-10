"""Sessions, turns, and messages (D7, D11).

Turn state is persisted *before* execution begins so an interrupted turn is
recoverable: create the turn row with status `pending`/`running` first, then
execute, then mark it `complete`/`failed`/`aborted`. `find_incomplete_turns`
lets `NomadCore` recover turns left in a non-terminal state after a power
cut or crash.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from nomad.core.turns import TurnSource
from nomad.storage.db import Database

TurnStatus = Literal["pending", "running", "awaiting_grant", "complete", "failed", "aborted"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "aborted"})


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Session(BaseModel):
    id: str
    created_at: datetime
    mode: str
    metadata: dict[str, Any]


class Turn(BaseModel):
    id: str
    session_id: str
    status: TurnStatus
    started_at: datetime | None
    finished_at: datetime | None
    metadata: dict[str, Any]
    #: Who began it. Defaults to `USER` so a row written before migration 006
    #: and a caller that does not care both read the same, and correctly.
    source: TurnSource = TurnSource.USER


class Message(BaseModel):
    id: str
    turn_id: str
    session_id: str
    role: str
    content: dict[str, Any]
    created_at: datetime


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _session_from_row(row: dict[str, Any]) -> Session:
    return Session(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        mode=row["mode"],
        metadata=json.loads(row["metadata_json"]),
    )


def _turn_from_row(row: dict[str, Any]) -> Turn:
    return Turn(
        id=row["id"],
        session_id=row["session_id"],
        status=row["status"],
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        metadata=json.loads(row["metadata_json"]),
        source=TurnSource(row.get("source") or TurnSource.USER),
    )


def _message_from_row(row: dict[str, Any]) -> Message:
    return Message(
        id=row["id"],
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        role=row["role"],
        content=json.loads(row["content_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class ConversationsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- sessions ---------------------------------------------------------

    async def create_session(
        self, *, mode: str, metadata: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Session:
        sid = session_id or _new_id()
        created_at = _now()
        await self._db.execute(
            "INSERT INTO sessions (id, created_at, mode, metadata_json) VALUES (?, ?, ?, ?)",
            (sid, created_at, mode, json.dumps(metadata or {})),
        )
        return Session(
            id=sid,
            created_at=datetime.fromisoformat(created_at),
            mode=mode,
            metadata=metadata or {},
        )

    async def get_session(self, session_id: str) -> Session | None:
        row = await self._db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _session_from_row(row) if row else None

    # -- turns --------------------------------------------------------------

    async def create_turn(
        self,
        *,
        session_id: str,
        status: TurnStatus = "pending",
        metadata: dict[str, Any] | None = None,
        turn_id: str | None = None,
        source: TurnSource = TurnSource.USER,
    ) -> Turn:
        """Persist a turn *before* execution begins (D11)."""
        tid = turn_id or _new_id()
        started_at = _now() if status != "pending" else None
        await self._db.execute(
            "INSERT INTO turns "
            "(id, session_id, status, started_at, finished_at, metadata_json, source) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (tid, session_id, status, started_at, json.dumps(metadata or {}), str(source)),
        )
        return Turn(
            id=tid,
            session_id=session_id,
            status=status,
            started_at=_parse_dt(started_at),
            finished_at=None,
            metadata=metadata or {},
            source=source,
        )

    async def update_turn_status(
        self, turn_id: str, status: TurnStatus, *, set_started: bool = False
    ) -> None:
        finished_at = _now() if status in TERMINAL_STATUSES else None
        if set_started:
            await self._db.execute(
                "UPDATE turns SET status = ?, started_at = COALESCE(started_at, ?), "
                "finished_at = ? WHERE id = ?",
                (status, _now(), finished_at, turn_id),
            )
        else:
            await self._db.execute(
                "UPDATE turns SET status = ?, finished_at = ? WHERE id = ?",
                (status, finished_at, turn_id),
            )

    async def get_turn(self, turn_id: str) -> Turn | None:
        row = await self._db.fetch_one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return _turn_from_row(row) if row else None

    async def find_incomplete_turns(self) -> list[Turn]:
        """Turns left in a non-terminal state — recoverable after a crash/reboot (D11)."""
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = await self._db.fetch_all(
            f"SELECT * FROM turns WHERE status NOT IN ({placeholders}) ORDER BY started_at ASC",
            list(TERMINAL_STATUSES),
        )
        return [_turn_from_row(row) for row in rows]

    # -- messages -----------------------------------------------------------

    async def add_message(
        self,
        *,
        turn_id: str,
        session_id: str,
        role: str,
        content: dict[str, Any],
        message_id: str | None = None,
    ) -> Message:
        mid = message_id or _new_id()
        created_at = _now()
        await self._db.execute(
            "INSERT INTO messages (id, turn_id, session_id, role, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, turn_id, session_id, role, json.dumps(content), created_at),
        )
        return Message(
            id=mid,
            turn_id=turn_id,
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.fromisoformat(created_at),
        )

    async def get_messages_for_turn(self, turn_id: str) -> list[Message]:
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE turn_id = ? ORDER BY created_at ASC", (turn_id,)
        )
        return [_message_from_row(row) for row in rows]

    async def get_messages_for_session(self, session_id: str, *, limit: int = 200) -> list[Message]:
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
            (session_id, limit),
        )
        return [_message_from_row(row) for row in rows]
