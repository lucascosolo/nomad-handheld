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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_grants_tool_target ON grants(tool, target)")

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
        "CREATE INDEX IF NOT EXISTS idx_pending_auth_session ON pending_authorizations(session_id)"
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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_forgotten ON memories(forgotten_at)")


async def _migration_003_notifications(db: Database) -> None:
    """The durable notification queue (chunk N).

    **This table is the answer to D6.** The event bus drops slow subscribers on
    purpose so a stalled client cannot freeze the display — correct for events,
    fatal for a timer, because Nomad's screen is dark most of the time and its
    process restarts. A notification published to nobody has not been missed;
    it has ceased to exist. So it is a row, and "delivery" is a state
    transition something performs later.

    `state` is a column rather than a set of nullable timestamps because the
    dedup rule is scoped to one of its values and a partial index can only
    reference what is actually stored. The index below is the load-bearing
    line: **at most one *pending* row per `dedup_key`.** A caller polling a
    condition every thirty seconds folds into one row; an alarm that already
    rang this morning has freed its key and may ring again tomorrow. Scoping
    uniqueness to `pending` — rather than to "not cancelled" — is what lets one
    table serve both cases without a second concept.

    `repeat_rule` is text, not seconds, because a daily alarm is not 86400
    seconds: fixed-interval arithmetic drifts an hour twice a year, and
    `repeat_tz` is what `notifications/repeat.py` re-anchors 07:00 to. A
    re-armed occurrence is a **new row** — the fired one stays terminal, so the
    record of what went off when survives the repeat.

    No foreign key to `sessions`: a timer set on Tuesday must outlive the
    session that set it, including the rollover in D34.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            dedup_key TEXT NOT NULL,
            kind TEXT NOT NULL
                CHECK (kind IN ('timer', 'alarm', 'reminder', 'system', 'agent')),
            state TEXT NOT NULL
                CHECK (state IN (
                    'pending', 'delivered', 'acknowledged', 'expired', 'cancelled'
                )),
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            expires_at TEXT,
            delivered_at TEXT,
            resolved_at TEXT,
            repeat_rule TEXT,
            repeat_tz TEXT,
            raise_count INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'agent',
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_open_dedup "
        "ON notifications(dedup_key) WHERE state = 'pending'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_due ON notifications(state, due_at)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_kind ON notifications(kind)")


async def _migration_004_utilities(db: Database) -> None:
    """Notes and stopwatches (chunk U).

    Timers and alarms are deliberately **absent** here: a timer is a row in
    `notifications`, because "fires once, durably, when something is finally
    watching" is precisely what that table already is. A second table would
    have needed its own delivery path, its own dedup rule and its own catch-up
    behaviour after a reboot, and would have got at least one of them wrong.

    A stopwatch is stored as `(started_at, accumulated_seconds, running)`
    rather than as an elapsed number, so reading it is arithmetic against the
    current time and nothing has to tick. That is what makes it survive the
    screen going off and the process restarting without a task anywhere.

    Notes carry `deleted_at` rather than being deleted, matching `memories`: on
    a device with no undo button, a soft delete is the difference between a
    mistake and a loss.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_notes_deleted ON notes(deleted_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at)")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS stopwatches (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            running INTEGER NOT NULL DEFAULT 1,
            started_at TEXT,
            accumulated_seconds REAL NOT NULL DEFAULT 0.0,
            laps_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stopped_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_stopwatches_running ON stopwatches(running)")


async def _migration_005_offline(db: Database) -> None:
    """Evidence for the offline tier's promotion path (chunk O).

    **Both tables exist because a count that dies with the process is not
    evidence.** Nomad's session rolls over by design (D34) and the device is
    power-cycled in a pocket; "the operator asks this every morning" is a claim
    that can only be made from rows that outlived several of both.

    `offline_asks.normalized` is UNIQUE and it is the canonical form
    `offline/intents.py` matches on, not the raw text. That is deliberate: the
    router treats "What's my battery?" and "whats my battery" as one question,
    so evidence that split them would undercount every phrase the operator says
    two ways and never reach a count worth acting on. `sample` keeps one raw
    spelling so a proposal can quote the operator back to themselves.

    `observed_days_json` is a bounded list of dates rather than a counter,
    because the question being asked of it is "on how many *distinct* days", and
    a counter cannot answer that without also knowing whether today is already
    counted. It is capped in `ledger.py`; stability only ever asks "at least N".

    `offline_promotions.state` is why there is a second table rather than three
    more columns on the first. A proposal is a thing with a lifecycle an
    operator drives — proposed, then accepted or rejected — and the rejected
    rows are load-bearing: `candidates()` joins against this table so a phrase
    the operator has already refused is never proposed again. A device that
    re-asks a settled question has made "no" more expensive than "yes".

    Neither table carries a foreign key to `sessions`: evidence about what the
    operator asks must outlive the session that heard it, the same reasoning
    migrations 002, 003 and 004 all give.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_asks (
            id TEXT PRIMARY KEY,
            normalized TEXT NOT NULL UNIQUE,
            sample TEXT NOT NULL,
            routed_intent TEXT,
            ask_count INTEGER NOT NULL DEFAULT 1,
            observed_days_json TEXT NOT NULL DEFAULT '[]',
            first_asked_at TEXT NOT NULL,
            last_asked_at TEXT NOT NULL
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_offline_asks_count ON offline_asks(ask_count)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_asks_routed ON offline_asks(routed_intent)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_promotions (
            id TEXT PRIMARY KEY,
            normalized TEXT NOT NULL UNIQUE,
            form TEXT NOT NULL CHECK (form IN ('skill', 'tool')),
            name TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            draft TEXT NOT NULL DEFAULT '',
            evidence_asks INTEGER NOT NULL DEFAULT 0,
            evidence_days INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL
                CHECK (state IN ('proposed', 'accepted', 'rejected')),
            proposed_at TEXT NOT NULL,
            decided_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_promotions_state ON offline_promotions(state)"
    )


async def _migration_006_turn_source(db: Database) -> None:
    """Where each turn came from (chunk P).

    A column and not a metadata key, because provenance is the thing a
    transcript is filtered and audited on: "show me every turn nobody asked
    for" has to be a `WHERE`, not a JSON scan the day the table is large.

    `DEFAULT 'user'` is what migrates the existing rows, and it is honest —
    until this migration ran there was exactly one way for a turn to begin,
    and it was somebody typing. SQLite backfills the default into every
    existing row as part of `ADD COLUMN`, so there is no separate UPDATE to
    get wrong.

    The `CHECK` matches migration 001's treatment of `turns.status` and exists
    for the same reason that column's comment in `agent/session.py` records:
    an unconstrained status column let a mis-spelled value through until every
    finished turn failed. A vocabulary written to disk should be enforced by
    the disk.
    """
    columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(turns)")}
    if "source" in columns:
        # `ADD COLUMN` is not `IF NOT EXISTS`-able, so idempotence is checked
        # rather than declared — the second line of defence this module's
        # docstring asks every migration for.
        return
    await db.execute(
        "ALTER TABLE turns ADD COLUMN source TEXT NOT NULL DEFAULT 'user' "
        "CHECK (source IN ('user', 'timer', 'sensor', 'self'))"
    )


MIGRATIONS: list[Migration] = [
    _migration_001_initial_schema,
    _migration_002_memories,
    _migration_003_notifications,
    _migration_004_utilities,
    _migration_005_offline,
    _migration_006_turn_source,
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
