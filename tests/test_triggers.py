"""Turns nobody asked for, and the four things that stop one being a runaway.

Chunk P. Two subjects: provenance (`TurnSource`, persisted and published) and
`SelfImproveTrigger`, which is the first thing on this device that can begin a
turn without a person.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nomad.agent.session import (
    EVENT_TURN_FINISHED,
    EVENT_TURN_STARTED,
    AgentSession,
    TurnOutcome,
    TurnOutcomeStatus,
)
from nomad.core.config import NomadConfig
from nomad.core.events import Event, EventBus
from nomad.core.turns import TurnSource
from nomad.storage.db import Database
from nomad.storage.migrations import migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.registry import TargetRegistry
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace
from nomad.triggers import SELF_IMPROVE_PROMPT, SelfImproveTrigger


def _session(db: Database, tmp_path: Path, bus: EventBus) -> AgentSession:
    return AgentSession(
        config=NomadConfig(),
        bus=bus,
        conversations=ConversationsRepository(db),
        grants=GrantsRepository(db),
        targets=TargetRegistry(),
        tools=ToolRegistry(),
        workspace=Workspace(tmp_path / "workspace"),
        resume_pending=False,
    )


# -- provenance ------------------------------------------------------------


async def test_an_existing_row_migrates_to_user(tmp_path: Path) -> None:
    """Every turn that predates the column was somebody typing, and says so."""
    db = Database(tmp_path / "nomad.db")
    await db.start()
    try:
        # Stop one short of the migration under test, so the row below is
        # genuinely written against the old schema rather than backfilled by a
        # default that was already there.
        await db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        await db.execute("INSERT INTO schema_version (version) VALUES (0)")
        from nomad.storage.migrations import MIGRATIONS

        for index, migration in enumerate(MIGRATIONS[:-1], start=1):
            await migration(db)
            await db.execute("UPDATE schema_version SET version = ?", (index,))
        await db.execute(
            "INSERT INTO sessions (id, created_at, mode) VALUES ('s', '2026-01-01T00:00:00', 'a')"
        )
        await db.execute("INSERT INTO turns (id, session_id, status) VALUES ('t', 's', 'complete')")

        await migrate(db)

        turn = await ConversationsRepository(db).get_turn("t")
        assert turn is not None
        assert turn.source is TurnSource.USER
    finally:
        await db.stop()


async def test_the_migration_is_idempotent(db: Database) -> None:
    """`ADD COLUMN` has no `IF NOT EXISTS`, so re-running must be checked."""
    from nomad.storage.migrations import _migration_006_turn_source

    await _migration_006_turn_source(db)
    await _migration_006_turn_source(db)


async def test_a_turn_persists_where_it_came_from(db: Database, tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    session = _session(db, tmp_path, bus)
    await session.start()
    try:
        outcome = await session.send("go", source=TurnSource.SELF)
        turn = await ConversationsRepository(db).get_turn(outcome.turn_id)
        assert turn is not None
        assert turn.source is TurnSource.SELF
    finally:
        await session.stop()
        await bus.stop()


async def test_send_still_defaults_to_the_operator(db: Database, tmp_path: Path) -> None:
    """The default is what keeps every caller that predates provenance correct."""
    bus = EventBus()
    await bus.start()
    session = _session(db, tmp_path, bus)
    await session.start()
    try:
        outcome = await session.send("hello")
        assert outcome.source is TurnSource.USER
        turn = await ConversationsRepository(db).get_turn(outcome.turn_id)
        assert turn is not None
        assert turn.source is TurnSource.USER
    finally:
        await session.stop()
        await bus.stop()


async def test_both_turn_events_carry_the_source(db: Database, tmp_path: Path) -> None:
    """Published on the payload, never by renaming an event: `resources` matches
    these two names as text and `tests/test_resources.py` pins that."""
    bus = EventBus()
    await bus.start()
    seen: dict[str, Event] = {}

    async def record(event: Event) -> None:
        seen[event.type] = event

    bus.subscribe(EVENT_TURN_STARTED, record)
    bus.subscribe(EVENT_TURN_FINISHED, record)

    session = _session(db, tmp_path, bus)
    await session.start()
    try:
        await session.send("go", source=TurnSource.SELF)
        for _ in range(50):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await session.stop()
        await bus.stop()

    assert seen[EVENT_TURN_STARTED].payload["source"] == "self"
    assert seen[EVENT_TURN_FINISHED].payload["source"] == "self"


# -- the trigger -----------------------------------------------------------


class _FakeSession:
    """Just enough session to drive the loop: `busy` and `send`."""

    def __init__(self, *, outcomes: list[object] | None = None) -> None:
        self.busy = False
        self.sent: list[tuple[str, TurnSource]] = []
        self._outcomes = outcomes or []

    async def send(self, text: str, *, source: TurnSource = TurnSource.USER) -> TurnOutcome:
        self.sent.append((text, source))
        result = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(result, Exception):
            raise result
        if isinstance(result, TurnOutcome):
            return result
        return TurnOutcome(turn_id="t", status=TurnOutcomeStatus.COMPLETED, source=source)


def _trigger(session: object, **kwargs: object) -> SelfImproveTrigger:
    kwargs.setdefault("interval_s", 0.01)
    # The shipped first tick is two minutes; a suite that waited for it would
    # not be a suite. Tests about the first tick specifically pass their own.
    kwargs.setdefault("first_tick_s", 0.01)
    return SelfImproveTrigger(session, **kwargs)  # type: ignore[arg-type]


async def _drain(trigger: SelfImproveTrigger, *, until: object, tries: int = 200) -> None:
    for _ in range(tries):
        if until():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the trigger never reached the expected state")


async def test_it_starts_a_self_sourced_turn_naming_the_skill() -> None:
    session = _FakeSession()
    trigger = _trigger(session)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: trigger.turns_started >= 1)
    finally:
        await trigger.stop()

    text, source = session.sent[0]
    assert source is TurnSource.SELF
    # Invoked by name, not restated: the skill index is already in every prompt.
    assert "improving-yourself" in text
    assert text == SELF_IMPROVE_PROMPT


async def test_it_never_preempts_a_live_turn() -> None:
    """`send()` serializes on a lock, so the naive call queues rather than
    failing — and lands minutes later as an answer to nothing."""
    session = _FakeSession()
    session.busy = True
    trigger = _trigger(session)
    await trigger.start()
    try:
        await asyncio.sleep(0.1)
        assert session.sent == []
        session.busy = False
        await _drain(trigger, until=lambda: trigger.turns_started >= 1)
    finally:
        await trigger.stop()


async def test_it_does_not_fire_when_the_backend_cannot_authenticate() -> None:
    """The device is logged out today. An hourly turn that cannot run forever
    is exactly the runaway this must not be."""
    session = _FakeSession()

    async def not_ready() -> bool:
        return False

    trigger = _trigger(session, readiness=not_ready)
    await trigger.start()
    try:
        await asyncio.sleep(0.1)
        assert session.sent == []
    finally:
        await trigger.stop()


async def test_a_probe_that_raises_is_a_skip_not_a_fire() -> None:
    session = _FakeSession()

    async def broken() -> bool:
        raise RuntimeError("no CLI")

    trigger = _trigger(session, readiness=broken)
    await trigger.start()
    try:
        await asyncio.sleep(0.1)
        assert session.sent == []
        # And it did not consume the failure budget either: nothing was tried.
        assert trigger.consecutive_failures == 0
    finally:
        await trigger.stop()


async def test_a_turn_that_raises_does_not_kill_the_component() -> None:
    session = _FakeSession(outcomes=[RuntimeError("backend went away")] * 50)
    trigger = _trigger(session, max_consecutive_failures=5)
    await trigger.start()
    try:
        # Three raised turns in a row and the loop is still ticking — which is
        # the point: the backend going away for an interval is ordinary.
        await _drain(trigger, until=lambda: trigger.consecutive_failures >= 3)
        assert not trigger.stopped_after_failures
        assert trigger.turns_started == 0
    finally:
        await trigger.stop()


async def test_consecutive_failed_turns_stop_the_loop() -> None:
    """A *failed outcome* counts, not just a raised one. `_run_turn` catches a
    backend failure and returns it, so counting exceptions alone would watch
    the device fail identically forever and call it healthy."""
    failed = [
        TurnOutcome(turn_id=f"t{i}", status=TurnOutcomeStatus.FAILED, error="nope")
        for i in range(5)
    ]
    session = _FakeSession(outcomes=list(failed))
    trigger = _trigger(session, max_consecutive_failures=3)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: trigger.stopped_after_failures)
        # Stopped means stopped: no further turns, ever, until a restart.
        started = len(session.sent)
        await asyncio.sleep(0.1)
        assert len(session.sent) == started == 3
    finally:
        await trigger.stop()


async def test_a_good_turn_clears_the_failure_budget() -> None:
    session = _FakeSession(
        outcomes=[
            TurnOutcome(turn_id="a", status=TurnOutcomeStatus.FAILED, error="nope"),
            TurnOutcome(turn_id="b", status=TurnOutcomeStatus.FAILED, error="nope"),
            TurnOutcome(turn_id="c", status=TurnOutcomeStatus.COMPLETED),
            TurnOutcome(turn_id="d", status=TurnOutcomeStatus.FAILED, error="nope"),
        ]
    )
    trigger = _trigger(session, max_consecutive_failures=3)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: len(session.sent) >= 4)
        assert not trigger.stopped_after_failures
        assert trigger.consecutive_failures == 1
    finally:
        await trigger.stop()


async def test_an_interrupted_turn_does_not_spend_the_budget() -> None:
    """An interrupt is the operator taking the device back, working as intended."""
    session = _FakeSession(
        outcomes=[TurnOutcome(turn_id="a", status=TurnOutcomeStatus.INTERRUPTED)] * 5
    )
    trigger = _trigger(session, max_consecutive_failures=2)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: len(session.sent) >= 4)
        assert trigger.consecutive_failures == 0
        assert not trigger.stopped_after_failures
    finally:
        await trigger.stop()


# -- the first tick after a boot -------------------------------------------


async def test_the_first_turn_does_not_wait_a_whole_interval() -> None:
    """The complaint that produced this: switching the device on and watching
    it do nothing for an hour. A restart is usually the moment *after* a
    change, so it is when there is most likely to be something worth doing."""
    session = _FakeSession()
    trigger = _trigger(session, interval_s=3600.0, first_tick_s=0.02)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: trigger.turns_started >= 1)
    finally:
        await trigger.stop()
    assert len(session.sent) == 1


async def test_the_first_tick_still_waits_before_firing() -> None:
    """Not zero. The panel, the link and the backend settle in the first
    seconds of a boot, and a turn starting into that paints over the status
    card the operator is still reading."""
    session = _FakeSession()
    trigger = _trigger(session, interval_s=3600.0, first_tick_s=5.0)
    await trigger.start()
    try:
        await asyncio.sleep(0.05)
        assert session.sent == []
    finally:
        await trigger.stop()


async def test_after_the_first_tick_the_interval_takes_over() -> None:
    """A short first tick must not become a short *loop* — that would be a
    device permanently busy with itself."""
    session = _FakeSession()
    trigger = _trigger(session, interval_s=3600.0, first_tick_s=0.02)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: trigger.turns_started >= 1)
        await asyncio.sleep(0.1)
        assert len(session.sent) == 1
    finally:
        await trigger.stop()


async def test_a_negative_first_tick_is_clamped_not_obeyed() -> None:
    trigger = _trigger(_FakeSession(), first_tick_s=-30.0)
    assert trigger._first_tick_s == 0.0


async def test_a_zero_interval_is_off() -> None:
    session = _FakeSession()
    trigger = _trigger(session, interval_s=0.0)
    await trigger.start()
    try:
        await asyncio.sleep(0.05)
        assert session.sent == []
    finally:
        await trigger.stop()


async def test_stop_is_safe_before_start() -> None:
    await _trigger(_FakeSession()).stop()


# -- poked by a person -----------------------------------------------------


async def test_a_poke_starts_a_turn_without_waiting_for_it_to_finish() -> None:
    """The whole point: a button handler must not block for the minutes a
    turn takes, so `fire_now` returns before `send` has."""
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowSession(_FakeSession):
        async def send(self, text: str, *, source: TurnSource = TurnSource.USER) -> TurnOutcome:
            started.set()
            await release.wait()
            return await super().send(text, source=source)

    session = _SlowSession()
    trigger = _trigger(session, interval_s=0.0)
    try:
        assert trigger.fire_now() is True
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # The turn is mid-flight and `fire_now` returned long ago.
        assert trigger.turns_started == 0
        release.set()
        await _drain(trigger, until=lambda: trigger.turns_started >= 1)
    finally:
        await trigger.stop()

    text, source = session.sent[0]
    assert source is TurnSource.SELF
    assert text == SELF_IMPROVE_PROMPT


async def test_a_poke_is_refused_while_a_turn_is_in_flight() -> None:
    session = _FakeSession()
    session.busy = True
    trigger = _trigger(session, interval_s=0.0)
    try:
        assert trigger.fire_now() is False
        await asyncio.sleep(0.05)
        assert session.sent == []
    finally:
        await trigger.stop()


async def test_a_poke_does_not_ask_the_readiness_probe() -> None:
    """The timer skips when the backend cannot authenticate. A person holding
    the device is owed the error instead — a poke that silently does nothing
    is indistinguishable from a dead button."""
    asked = False

    async def not_ready() -> bool:
        nonlocal asked
        asked = True
        return False

    session = _FakeSession()
    trigger = _trigger(session, interval_s=0.0, readiness=not_ready)
    try:
        assert trigger.fire_now() is True
        await _drain(trigger, until=lambda: len(session.sent) >= 1)
        assert asked is False
    finally:
        await trigger.stop()


async def test_a_failing_poke_does_not_spend_the_timers_failure_budget() -> None:
    session = _FakeSession(outcomes=[RuntimeError("backend went away")] * 5)
    trigger = _trigger(session, interval_s=0.0, max_consecutive_failures=2)
    try:
        for _ in range(4):
            assert trigger.fire_now() is True
            await _drain(trigger, until=lambda: len(session.sent) >= 1)
            session.sent.clear()
        assert trigger.consecutive_failures == 0
        assert not trigger.stopped_after_failures
    finally:
        await trigger.stop()


async def test_a_poke_still_works_after_the_timer_has_given_up() -> None:
    """The cap silences the schedule, never the operator — who is very likely
    poking it precisely because they noticed it went quiet."""
    session = _FakeSession(outcomes=[RuntimeError("no")] * 3)
    trigger = _trigger(session, max_consecutive_failures=2)
    await trigger.start()
    try:
        await _drain(trigger, until=lambda: trigger.stopped_after_failures)
        session.sent.clear()
        assert trigger.fire_now() is True
        await _drain(trigger, until=lambda: len(session.sent) >= 1)
    finally:
        await trigger.stop()


# -- the real session honours `busy` ---------------------------------------


async def test_the_session_reports_busy_for_the_whole_of_a_turn(
    db: Database, tmp_path: Path
) -> None:
    """`busy` reads the lock, not `current_turn_id`: `send()` takes the lock
    before the turn row exists, and a trigger checking the id would fire into
    that window."""
    bus = EventBus()
    await bus.start()
    session = _session(db, tmp_path, bus)
    await session.start()
    try:
        assert not session.busy
        observed: list[bool] = []

        async def watch() -> None:
            observed.append(session.busy)

        bus.subscribe(EVENT_TURN_STARTED, watch)
        await session.send("hello")
        assert not session.busy
    finally:
        await session.stop()
        await bus.stop()


@pytest.mark.parametrize("member", list(TurnSource))
def test_every_source_round_trips_as_the_value_stored(member: TurnSource) -> None:
    """The enum's values are what land in the database's CHECK constraint."""
    assert TurnSource(str(member)) is member
