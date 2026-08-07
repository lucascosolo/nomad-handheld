"""Persisted event storage: append and paged query (D6, D7).

Storage's job is to persist what the event bus hands it and answer queries
— it does not decide what is worth persisting. A subscriber to `EventBus`
(wired up in a higher layer) calls `append` per event.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel

from nomad.core.events import Event
from nomad.storage.db import Database


class StoredEvent(BaseModel):
    id: str
    type: str
    source: str
    payload: dict
    ts: datetime


class EventsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, event: Event) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO events (id, type, source, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.id, event.type, event.source, json.dumps(event.payload), event.ts.isoformat()),
        )

    async def query(
        self,
        *,
        type_prefix: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StoredEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if type_prefix is not None:
            clauses.append("type LIKE ?")
            params.append(f"{type_prefix}%")
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until.isoformat())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = await self._db.fetch_all(
            f"SELECT * FROM events {where} ORDER BY ts ASC LIMIT ? OFFSET ?",
            params,
        )
        return [
            StoredEvent(
                id=row["id"],
                type=row["type"],
                source=row["source"],
                payload=json.loads(row["payload_json"]),
                ts=datetime.fromisoformat(row["ts"]),
            )
            for row in rows
        ]
