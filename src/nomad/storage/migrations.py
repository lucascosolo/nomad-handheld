"""Numbered migrations, applied in order inside a transaction. Idempotent (D7).

`schema_version` tracks the highest applied migration number. Each migration
is a plain function `(Database) -> None` (uses raw SQL via `db.execute`,
run inside a transaction by the caller). Migrations must be safe to run
against a database that already has them applied — `CREATE TABLE IF NOT
EXISTS` etc.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nomad.storage.db import Database

Migration = Callable[[Database], Awaitable[None]]


async def _migration_001_initial_schema(db: Database) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            status TEXT NOT NULL
                CHECK (status IN (
                    'pending', 'running', 'awaiting_grant',
                    'complete', 'failed', 'aborted'
                )),
            started_at TEXT,
            finished_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status)")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES turns(id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id)")

    # Security layer schema (D4) — populated by chunk D's repositories.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS grants (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT REFERENCES turns(id),
            tool TEXT NOT NULL,
            target TEXT NOT NULL,
            scope TEXT,
            source TEXT NOT NULL CHECK (source IN ('auto', 'human', 'session', 'model')),
            granted_at TEXT NOT NULL,
            expires_at TEXT,
            used_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_grants_session ON grants(session_id)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_grants_tool_target ON grants(tool, target)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_authorizations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT REFERENCES turns(id),
            tool TEXT NOT NULL,
            target TEXT NOT NULL,
            params_json TEXT NOT NULL,
            risk TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            resolved_at TEXT,
            resolution TEXT CHECK (
                resolution IS NULL
                OR resolution IN ('approved', 'denied', 'expired', 'timeout')
            )
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_auth_session "
        "ON pending_authorizations(session_id)"
    )


async def _migration_002_memories(db: Database) -> None:
    """The memory Nomad owns (chunk M).

    **No FTS5, on purpose.** The corpus is a few hundred rows on a handheld;
    a linear scan with `LIKE`-style scoring in `memory/store.py` is fast
    enough and is testable as ordinary code. An FTS5 virtual table, by
    contrast, may be missing from whatever sqlite a Pi image happens to ship,
    the failure shows up only on the device, and rebuilding a corrupt index
    offline on a handheld is maintenance nobody will do. The portability risk
    is not worth buying speed the corpus size does not need.

    `normalized_text` is UNIQUE: that constraint *is* the dedup rule. Two
    memories that fold to the same text are the same fact, and a session alive
    for weeks will re-state the same fact daily.

    `forgotten_at` makes forgetting a soft delete — every belief the device
    ever held stays auditable, which is the rule for every other decision on
    this device too.

    `source_session_id` carries no foreign key deliberately: a memory must
    outlive the session that formed it, including across the session rollover
    in `memory/rollover.py`.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            normalized_text TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL
                CHECK (kind IN ('operator', 'preference', 'project', 'fact')),
            keywords_json TEXT NOT NULL DEFAULT '[]',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_recalled_at TEXT,
            recall_count INTEGER NOT NULL DEFAULT 0,
            source_session_id TEXT,
            forgotten_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_forgotten ON memories(forgotten_at)"
    )


MIGRATIONS: list[Migration] = [
    _migration_001_initial_schema,
    _migration_002_memories,
]


async def _ensure_version_table(db: Database) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )
        """
    )
    row = await db.fetch_one("SELECT version FROM schema_version LIMIT 1")
    if row is None:
        await db.execute("INSERT INTO schema_version (version) VALUES (0)")


async def current_version(db: Database) -> int:
    await _ensure_version_table(db)
    row = await db.fetch_one("SELECT version FROM schema_version LIMIT 1")
    return int(row["version"]) if row else 0


async def migrate(db: Database) -> int:
    """Apply all migrations after the current schema version, in order.

    Each migration runs inside its own transaction. Safe to call repeatedly
    — already-applied migrations are skipped by version number, and each
    migration function is itself idempotent as a second line of defence.
    Returns the resulting schema version.
    """
    version = await current_version(db)
    for index, migration in enumerate(MIGRATIONS, start=1):
        if index <= version:
            continue
        async with db.transaction():
            await migration(db)
            await db.execute("UPDATE schema_version SET version = ?", (index,))
        version = index
    return version
