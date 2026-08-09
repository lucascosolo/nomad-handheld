"""D38: local compute yields to Claude; the interface never yields to anything.

Every delay in these tests is driven by a `ManualClock`. Nothing here sleeps
for real time, so a loaded two-core laptop cannot make the suite flaky by
missing a deadline it was only ever going to miss on the test machine. The one
exception is `test_the_governor_is_wired_to_the_real_turn_events`, which goes
through the bus and therefore waits for delivery like the rest of the suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nomad.core.config import ResourcesConfig
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.resources import (
    EVENT_WORKLOAD_TERMINATED,
    TURN_FINISHED_EVENT,
    TURN_STARTED_EVENT,
    InteractiveWorkload,
    ManualClock,
    OpportunisticWorkload,
    ResourceGovernor,
    Tier,
    TurnState,
    Workload,
    WorkloadError,
    WorkloadState,
    YieldContext,
)

#: How long one unit of fake background work "takes" on the manual clock.
UNIT = 1.0

CONFIG = ResourcesConfig(
    suspend_deadline_seconds=5.0,
    terminate_grace_seconds=1.0,
    resume_delay_seconds=3.0,
)

BASE_TS = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def turn_started(offset_ms: int = 0) -> Event:
    return Event(
        type=TURN_STARTED_EVENT,
        source="agent_session",
        payload={"turn_id": "t1"},
        ts=BASE_TS + timedelta(milliseconds=offset_ms),
    )


def turn_finished(offset_ms: int = 0) -> Event:
    return Event(
        type=TURN_FINISHED_EVENT,
        source="agent_session",
        payload={"turn_id": "t1", "status": "completed"},
        ts=BASE_TS + timedelta(milliseconds=offset_ms),
    )


# --- fake workloads ---------------------------------------------------------


class Indexer(OpportunisticWorkload):
    """A well-behaved opportunistic workload: checkpoints between units."""

    def __init__(self, name: str = "indexer") -> None:
        super().__init__(name)
        self.units = 0
        self.cancelled = False
        self.terminated = False

    async def run(self, ctx: YieldContext) -> None:
        try:
            while True:
                await ctx.checkpoint()
                self.units += 1
                await ctx.sleep(UNIT)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def terminate(self) -> None:
        self.terminated = True


class SearchIndexer(Indexer):
    """An indexer that also backs a tool endpoint.

    D38: suspending a *workload* never deregisters a *tool*. The background
    half is preemptible; the answer-on-demand half is not the same thing.
    """

    async def answer(self, query: str) -> str:
        return f"{query}:{self.units}"


class Uncooperative(OpportunisticWorkload):
    """Never checkpoints. Model-authored code that ignored the contract (D25)."""

    def __init__(self, name: str = "greedy") -> None:
        super().__init__(name)
        self.cancelled = False
        self.terminated = False

    async def run(self, ctx: YieldContext) -> None:
        try:
            while True:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def terminate(self) -> None:
        self.terminated = True


class Unkillable(OpportunisticWorkload):
    """Worse: it swallows cancellation. In-process there is no SIGKILL."""

    def __init__(self, name: str = "unkillable") -> None:
        super().__init__(name)
        self.cancellations = 0
        self.terminated = False
        self.released = False

    async def run(self, ctx: YieldContext) -> None:
        while not self.released:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                self.cancellations += 1

    async def terminate(self) -> None:
        self.terminated = True


class Saboteur(InteractiveWorkload):
    """An interactive workload that *looks* preemptible.

    It has `run`, `suspend` and `terminate` — the whole vocabulary. If the
    governor decided tiers by duck-typing rather than by type, this is what
    would take the screen down.
    """

    def __init__(self, name: str = "screen") -> None:
        super().__init__(name)
        self.touched: list[str] = []

    async def run(self, ctx: object) -> None:
        self.touched.append("run")

    async def suspend(self) -> None:
        self.touched.append("suspend")

    async def terminate(self) -> None:
        self.touched.append("terminate")


class Rogue(Workload):
    """Claims a tier by attribute without implementing either contract."""

    tier = Tier.OPPORTUNISTIC


# --- helpers ----------------------------------------------------------------


async def make_governor(
    bus: EventBus,
    clock: ManualClock,
    *,
    initial: TurnState = TurnState.IDLE,
    workloads: list[Workload] | None = None,
) -> ResourceGovernor:
    governor = ResourceGovernor(bus, config=CONFIG, clock=clock, initial_turn_state=initial)
    for workload in workloads or []:
        governor.register(workload)
    await governor.start()
    await clock.settle()
    return governor


def state_of(governor: ResourceGovernor, name: str) -> WorkloadState:
    return next(row.state for row in governor.inventory() if row.name == name)


# --- tiers ------------------------------------------------------------------


async def test_the_tier_comes_from_the_class_not_from_the_call_site(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    indexer, screen = Indexer(), Saboteur()
    governor = await make_governor(event_bus, clock, workloads=[indexer, screen])
    try:
        tiers = {row.name: row.tier for row in governor.inventory()}
        assert tiers == {"indexer": Tier.OPPORTUNISTIC, "screen": Tier.INTERACTIVE}
    finally:
        await governor.stop()


async def test_an_interactive_workload_has_no_suspension_surface() -> None:
    """The structural half of D38: there is nothing on it to call.

    Not "the governor checks the tier before suspending" — the base class has
    no `run`, no gate and no `suspend`, so a governor bug cannot reach one.
    """
    for attribute in ("run", "suspend", "resume", "terminate"):
        assert not hasattr(InteractiveWorkload, attribute)
    # Only `tier`, and nothing else anyone could call. Dunders are excluded
    # rather than enumerated: an ABC subclass carries `__abstractmethods__`,
    # `_abc_impl`, `__firstlineno__` and `__static_attributes__` on 3.13, none
    # of which is a suspension surface, and pinning that exact set would make
    # this fail on the next Python rather than on the next real regression.
    assert {name for name in vars(InteractiveWorkload) if not name.startswith("_")} == {"tier"}


async def test_a_workload_in_neither_tier_is_refused(event_bus: EventBus) -> None:
    governor = ResourceGovernor(event_bus, config=CONFIG, clock=ManualClock())
    with pytest.raises(WorkloadError):
        governor.register(Rogue("rogue"))


async def test_a_duplicate_name_is_refused(event_bus: EventBus) -> None:
    governor = ResourceGovernor(event_bus, config=CONFIG, clock=ManualClock())
    governor.register(Indexer("indexer"))
    with pytest.raises(WorkloadError):
        governor.register(Indexer("indexer"))


# --- the interactive tier is never preempted --------------------------------


async def test_a_live_turn_never_touches_an_interactive_workload(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    screen, indexer = Saboteur(), Indexer()
    governor = await make_governor(event_bus, clock, workloads=[screen, indexer])
    try:
        await governor.observe(turn_started())
        await governor.mark_indeterminate("belt and braces")
        await governor.observe(turn_finished(offset_ms=10))
        await clock.advance(CONFIG.resume_delay_seconds)

        assert screen.touched == []
        assert state_of(governor, "screen") is WorkloadState.REGISTERED
    finally:
        await governor.stop()
        assert screen.touched == []


# --- suspend and resume around a turn ---------------------------------------


async def test_opportunistic_work_stops_for_a_turn_and_returns_after(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await clock.advance(UNIT)
        assert indexer.units >= 2
        assert state_of(governor, "indexer") is WorkloadState.RUNNING

        await governor.observe(turn_started())
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
        frozen = indexer.units
        await clock.advance(UNIT * 10)
        assert indexer.units == frozen, "a suspended workload got the machine anyway"

        await governor.observe(turn_finished(offset_ms=10))
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
        await clock.advance(CONFIG.resume_delay_seconds)
        await governor.wait_settled()
        await clock.settle()
        assert state_of(governor, "indexer") is WorkloadState.RUNNING
        assert indexer.units > frozen
    finally:
        await governor.stop()


async def test_a_cooperative_workload_is_parked_not_cancelled(event_bus: EventBus) -> None:
    """The deadline is a backstop, not the mechanism. It must not be spent."""
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await governor.observe(turn_started())
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
        assert indexer.cancelled is False
        assert indexer.terminated is False
        assert clock.monotonic() == 0.0, "parking should not have consumed the deadline"
    finally:
        await governor.stop()


# --- cooperative first, fatal second ----------------------------------------


async def test_a_workload_that_will_not_yield_is_killed(event_bus: EventBus) -> None:
    clock = ManualClock()
    greedy = Uncooperative()
    terminations: list[str] = []

    async def watch(event: Event) -> None:
        terminations.append(event.payload["workload"])

    event_bus.subscribe(EVENT_WORKLOAD_TERMINATED, watch)
    governor = await make_governor(event_bus, clock, workloads=[greedy])
    try:
        suspending = asyncio.ensure_future(governor.observe(turn_started()))
        await clock.wait_for_sleepers(1)  # the governor is holding the deadline open
        assert greedy.cancelled is False, "killed before its deadline elapsed"
        await clock.advance(CONFIG.suspend_deadline_seconds)
        await suspending

        assert greedy.cancelled is True
        assert greedy.terminated is True
        assert state_of(governor, "greedy") is WorkloadState.TERMINATED
        await clock.settle()
        assert terminations == ["greedy"]
    finally:
        await governor.stop()


async def test_a_terminated_workload_is_not_resumed_by_the_next_idle_period(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    greedy = Uncooperative()
    governor = await make_governor(event_bus, clock, workloads=[greedy])
    try:
        suspending = asyncio.ensure_future(governor.observe(turn_started()))
        await clock.wait_for_sleepers(1)
        await clock.advance(CONFIG.suspend_deadline_seconds)
        await suspending

        await governor.observe(turn_finished(offset_ms=10))
        await clock.advance(CONFIG.resume_delay_seconds)
        await governor.wait_settled()
        assert state_of(governor, "greedy") is WorkloadState.TERMINATED
    finally:
        await governor.stop()


async def test_a_workload_that_swallows_cancellation_is_abandoned(
    event_bus: EventBus,
) -> None:
    """There is no SIGKILL in-process, so the governor stops waiting instead.

    It calls the workload's own last-resort hook — where something holding a
    subprocess kills it — drops the task, and returns. A turn must never be
    held up by code that refuses to die.
    """
    clock = ManualClock()
    zombie = Unkillable()
    governor = await make_governor(event_bus, clock, workloads=[zombie])
    try:
        suspending = asyncio.ensure_future(governor.observe(turn_started()))
        await clock.wait_for_sleepers(1)
        await clock.advance(CONFIG.suspend_deadline_seconds)
        await clock.wait_for_sleepers(1)  # now holding the termination grace open
        await clock.advance(CONFIG.terminate_grace_seconds)
        await suspending

        assert zombie.cancellations == 1
        assert zombie.terminated is True
        assert state_of(governor, "unkillable") is WorkloadState.TERMINATED
    finally:
        zombie.released = True
        await clock.settle()
        await governor.stop()


# --- fails toward responsiveness --------------------------------------------


async def test_an_unknown_turn_state_suspends_before_the_first_unit_of_work(
    event_bus: EventBus,
) -> None:
    """Boot is indeterminate: nobody has told the governor a turn is not live."""
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, initial=TurnState.UNKNOWN, workloads=[indexer])
    try:
        assert governor.turn_state is TurnState.UNKNOWN
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
        await clock.advance(UNIT * 5)
        assert indexer.units == 0, "background work ran before the state was known"

        await governor.observe(turn_finished())
        await clock.advance(CONFIG.resume_delay_seconds)
        await governor.wait_settled()
        await clock.settle()
        assert state_of(governor, "indexer") is WorkloadState.RUNNING
    finally:
        await governor.stop()


async def test_becoming_indeterminate_suspends_running_work(event_bus: EventBus) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        assert state_of(governor, "indexer") is WorkloadState.RUNNING
        await governor.mark_indeterminate("backend restarted")
        assert governor.turn_state is TurnState.UNKNOWN
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
    finally:
        await governor.stop()


async def test_a_governor_error_leaves_opportunistic_work_suspended(
    event_bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])

    async def boom(event: Event) -> None:
        raise RuntimeError("cannot classify")

    try:
        monkeypatch.setattr(governor, "observe", boom)
        await governor._on_event(turn_started())
        assert governor.turn_state is TurnState.UNKNOWN
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
    finally:
        await governor.stop()


# --- ordering and hysteresis ------------------------------------------------


async def test_a_stale_turn_finished_cannot_resume_a_live_turn(
    event_bus: EventBus,
) -> None:
    """Two subscriptions, two consumer tasks, no guaranteed delivery order.

    `turn_finished` published *before* a `turn_started` but delivered after it
    must not be applied, or an indexer resumes into a live turn.
    """
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await governor.observe(turn_started(offset_ms=20))
        await governor.observe(turn_finished(offset_ms=10))
        assert governor.turn_state is TurnState.LIVE
        await clock.advance(CONFIG.resume_delay_seconds)
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
    finally:
        await governor.stop()


async def test_a_tie_on_the_timestamp_resolves_toward_live(event_bus: EventBus) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await governor.observe(turn_started(offset_ms=10))
        await governor.observe(turn_finished(offset_ms=10))
        assert governor.turn_state is TurnState.LIVE
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED
    finally:
        await governor.stop()


async def test_a_burst_of_turns_does_not_resume_work_in_the_gap(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await governor.observe(turn_started(offset_ms=0))
        await governor.observe(turn_finished(offset_ms=10))
        await clock.advance(CONFIG.resume_delay_seconds / 2)
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED

        await governor.observe(turn_started(offset_ms=20))
        await clock.advance(CONFIG.resume_delay_seconds)
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED, (
            "the second turn's start did not cancel the first turn's resume"
        )

        await governor.observe(turn_finished(offset_ms=30))
        await clock.advance(CONFIG.resume_delay_seconds)
        await governor.wait_settled()
        await clock.settle()
        assert state_of(governor, "indexer") is WorkloadState.RUNNING
    finally:
        await governor.stop()


# --- availability is not background processing ------------------------------


async def test_suspending_a_workload_does_not_take_its_endpoint_down(
    event_bus: EventBus,
) -> None:
    """D38: "we paused the indexer" must never become "search disappeared".

    There is no tool registry involved yet — wiring one is a later chunk — but
    the property is expressible now: parking `run` parks the background half
    and nothing else. The on-demand half still answers, mid-turn.
    """
    clock = ManualClock()
    search = SearchIndexer("search")
    governor = await make_governor(event_bus, clock, workloads=[search])
    try:
        await governor.observe(turn_started())
        assert state_of(governor, "search") is WorkloadState.SUSPENDED
        assert await search.answer("q") == f"q:{search.units}"
    finally:
        await governor.stop()


# --- wiring -----------------------------------------------------------------


async def test_the_governor_is_wired_to_the_real_turn_events(event_bus: EventBus) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    try:
        await event_bus.publish(turn_started())
        await asyncio.sleep(0.05)
        assert governor.turn_state is TurnState.LIVE
        assert state_of(governor, "indexer") is WorkloadState.SUSPENDED

        await event_bus.publish(turn_finished(offset_ms=10))
        await asyncio.sleep(0.05)
        assert governor.turn_state is TurnState.IDLE
    finally:
        await governor.stop()


def test_the_turn_event_names_still_match_the_agent_module() -> None:
    """Pins the coupling that layering forbids expressing as an import.

    `resources` may not import `agent` (see `test_layering.py`), so the two
    event names are duplicated. Read the agent module as text — the same trick
    the layering test uses — so a rename fails here loudly instead of leaving
    the governor believing no turn is ever live.
    """
    source = (
        Path(__file__).resolve().parent.parent / "src" / "nomad" / "agent" / "session.py"
    ).read_text()
    assert f'EVENT_TURN_STARTED = "{TURN_STARTED_EVENT}"' in source
    assert f'EVENT_TURN_FINISHED = "{TURN_FINISHED_EVENT}"' in source


async def test_stop_cancels_workloads_and_reports_stopped(event_bus: EventBus) -> None:
    clock = ManualClock()
    indexer = Indexer()
    governor = await make_governor(event_bus, clock, workloads=[indexer])
    await governor.stop()
    assert governor.state is ComponentState.STOPPED
    assert state_of(governor, "indexer") is WorkloadState.STOPPED
    frozen = indexer.units
    await clock.advance(UNIT * 5)
    assert indexer.units == frozen


async def test_a_workload_registered_after_start_is_governed_immediately(
    event_bus: EventBus,
) -> None:
    clock = ManualClock()
    governor = await make_governor(event_bus, clock)
    indexer = Indexer()
    try:
        await governor.observe(turn_started())
        governor.register(indexer)
        await clock.settle()
        assert indexer.units == 0, "a workload registered mid-turn got the machine"
        assert state_of(governor, "indexer") in (
            WorkloadState.SUSPENDING,
            WorkloadState.SUSPENDED,
        )
    finally:
        await governor.stop()
