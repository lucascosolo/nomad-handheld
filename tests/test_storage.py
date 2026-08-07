from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nomad.core.events import Event
from nomad.storage.db import Database
from nomad.storage.migrations import MIGRATIONS, current_version, migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.events import EventsRepository


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "idempotent.db")
    await database.start()
    try:
        v1 = await migrate(database)
        v2 = await migrate(database)
        assert v1 == v2 == len(MIGRATIONS)
        assert await current_version(database) == v2
    finally:
        await database.stop()


async def test_wal_mode_and_foreign_keys_enabled(db: Database) -> None:
    row = await db.fetch_one("PRAGMA journal_mode")
    assert row is not None
    assert row["journal_mode"].lower() == "wal"

    row = await db.fetch_one("PRAGMA foreign_keys")
    assert row is not None
    assert row["foreign_keys"] == 1


async def test_events_repository_round_trip(db: Database) -> None:
    repo = EventsRepository(db)
    event = Event(type="tool.called", source="test", payload={"name": "get_system_info"})
    await repo.append(event)

    results = await repo.query(type_prefix="tool")
    assert len(results) == 1
    assert results[0].id == event.id
    assert results[0].payload == {"name": "get_system_info"}


async def test_events_repository_filters_by_prefix_and_time(db: Database) -> None:
    repo = EventsRepository(db)
    now = datetime.now(UTC)
    await repo.append(Event(type="tool.called", source="t", payload={}, ts=now))
    await repo.append(Event(type="display.updated", source="t", payload={}, ts=now))
    await repo.append(
        Event(type="tool.called", source="t", payload={}, ts=now - timedelta(hours=2))
    )

    recent_tool_events = await repo.query(type_prefix="tool", since=now - timedelta(minutes=1))
    assert len(recent_tool_events) == 1

    all_tool_events = await repo.query(type_prefix="tool")
    assert len(all_tool_events) == 2

    display_events = await repo.query(type_prefix="display")
    assert len(display_events) == 1


async def test_events_repository_pagination(db: Database) -> None:
    repo = EventsRepository(db)
    for i in range(5):
        await repo.append(Event(type="counted", source="t", payload={"i": i}))

    page1 = await repo.query(type_prefix="counted", limit=2, offset=0)
    page2 = await repo.query(type_prefix="counted", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].id != page2[0].id


async def test_conversation_repository_round_trip(db: Database) -> None:
    repo = ConversationsRepository(db)
    session = await repo.create_session(mode="manual")
    turn = await repo.create_turn(session_id=session.id, status="pending")
    await repo.add_message(
        turn_id=turn.id, session_id=session.id, role="user", content={"text": "hi"}
    )
    await repo.add_message(
        turn_id=turn.id, session_id=session.id, role="assistant", content={"text": "hello"}
    )

    fetched_session = await repo.get_session(session.id)
    assert fetched_session is not None
    assert fetched_session.mode == "manual"

    messages = await repo.get_messages_for_turn(turn.id)
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_turn_persisted_before_execution_and_status_updates(db: Database) -> None:
    repo = ConversationsRepository(db)
    session = await repo.create_session(mode="auto")
    turn = await repo.create_turn(session_id=session.id, status="pending")

    # Simulate: turn marked running before any tool execution happens (D11).
    await repo.update_turn_status(turn.id, "running", set_started=True)
    running_turn = await repo.get_turn(turn.id)
    assert running_turn is not None
    assert running_turn.status == "running"
    assert running_turn.started_at is not None
    assert running_turn.finished_at is None

    await repo.update_turn_status(turn.id, "complete")
    finished_turn = await repo.get_turn(turn.id)
    assert finished_turn is not None
    assert finished_turn.status == "complete"
    assert finished_turn.finished_at is not None


async def test_find_incomplete_turns_recovers_non_terminal_state(db: Database) -> None:
    repo = ConversationsRepository(db)
    session = await repo.create_session(mode="auto")

    running_turn = await repo.create_turn(session_id=session.id, status="running")
    awaiting_turn = await repo.create_turn(session_id=session.id, status="awaiting_grant")
    done_turn = await repo.create_turn(session_id=session.id, status="pending")
    await repo.update_turn_status(done_turn.id, "complete")

    incomplete = await repo.find_incomplete_turns()
    incomplete_ids = {t.id for t in incomplete}

    assert running_turn.id in incomplete_ids
    assert awaiting_turn.id in incomplete_ids
    assert done_turn.id not in incomplete_ids
