"""Local compute yields to Claude; the interface never yields to anything (D38).

The Pi has 4 GB and four cores and wants to run things that do not fit
together. When the operator is talking to Claude, Claude gets the machine — so
opportunistic workloads are parked for the length of a turn and let go again
afterwards, and interactive ones are never touched at all.

Three things about how that is done here are deliberate and would look like
accidents otherwise.

**The governor learns about turns from event *names*, not from the agent.**
`resources` imports `core` and nothing else, so the two event names are
duplicated here rather than imported. That is not laziness: a resource policy
that must keep working while the session is wedged should not hold a reference
to the session. `tests/test_resources.py` reads `agent/session.py` as text and
fails if the names drift, which is the same trick `test_layering.py` uses.

**Ordering is decided by the event's timestamp, not by arrival.** Those two
names are two subscriptions, which means two consumer tasks and no guaranteed
delivery order between them (D6). A `turn_finished` that was published before a
`turn_started` but delivered after it would otherwise resume an indexer into a
live turn. So every event carries `ts`, the governor applies only events at
least as new as the last one it applied, and **a tie resolves toward LIVE** —
when two frames claim the same instant, the safe reading is that the machine is
busy.

**Suspension is cooperative first and fatal second, and the deadline is a
backstop rather than the mechanism.** A well-behaved workload parks at its next
`ctx.checkpoint()` without a single tick of the deadline being spent. One that
will not park is cancelled; one that swallows cancellation is abandoned after a
grace window, because there is no SIGKILL for a coroutine and a turn must never
be held up by code that refuses to die. Cooperation cannot be assumed — a
promoted offline handler is model-authored code (D25, D29).

Note what this module does *not* do: suspending a workload never touches the
tool registry. The model must be able to call any tool at any time and get an
answer computed on demand. "We paused the indexer" must never become "search
disappeared".
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nomad.core.config import ResourcesConfig
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.resources.clock import Clock, SystemClock
from nomad.resources.errors import WorkloadError
from nomad.resources.workload import (
    InteractiveWorkload,
    OpportunisticWorkload,
    Tier,
    Workload,
    WorkloadState,
    YieldContext,
)

logger = get_logger(__name__)

#: Duplicated from `nomad.agent.session` because `resources` may import only
#: `core`. Pinned as text by `tests/test_resources.py`, so a rename in `agent`
#: fails loudly here instead of leaving the governor believing no turn is ever
#: live — which would be silent, and would look like good performance.
TURN_STARTED_EVENT = "agent.turn_started"
TURN_FINISHED_EVENT = "agent.turn_finished"

EVENT_WORKLOAD_SUSPENDED = "resources.workload_suspended"
EVENT_WORKLOAD_RESUMED = "resources.workload_resumed"
EVENT_WORKLOAD_TERMINATED = "resources.workload_terminated"


class TurnState(StrEnum):
    """What the governor believes the session is doing.

    `UNKNOWN` is a real state and not an error one. At boot nobody has said a
    turn is *not* running, and after a backend restart the last thing observed
    may be arbitrarily stale. D38 resolves an unknown state toward
    responsiveness, so `UNKNOWN` suspends exactly like `LIVE` — the cost of
    being wrong is an index that finishes late, against a device that lags
    while its owner is talking to it.
    """

    LIVE = "live"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass
class WorkloadStatus:
    """One row of `inventory()`. Reporting only; nothing reads it to decide."""

    name: str
    tier: Tier
    state: WorkloadState


@dataclass
class _Entry:
    workload: Workload
    tier: Tier
    state: WorkloadState = WorkloadState.REGISTERED
    ctx: YieldContext | None = None
    task: asyncio.Task[None] | None = None


class ResourceGovernor:
    """Parks background work while a turn is live. A `Component`."""

    name = "resource_governor"

    def __init__(
        self,
        bus: EventBus,
        *,
        config: ResourcesConfig | None = None,
        clock: Clock | None = None,
        initial_turn_state: TurnState = TurnState.UNKNOWN,
    ) -> None:
        self._bus = bus
        self._config = config if config is not None else ResourcesConfig()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._turn_state = initial_turn_state
        self._entries: dict[str, _Entry] = {}
        self._applied_ts: datetime | None = None
        self._resume_task: asyncio.Task[None] | None = None
        self._unsubscribes: list[Callable[[], None]] = []
        self._state = ComponentState.NEW

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def turn_state(self) -> TurnState:
        return self._turn_state

    # -- registration --------------------------------------------------------

    def register(self, workload: Workload) -> None:
        """Register a workload in the tier its *class* declares.

        Synchronous, and it does not need to be otherwise: registering during a
        live turn simply does not launch the workload, so there is no window in
        which new background work runs at the worst possible moment.
        """
        if not isinstance(workload, InteractiveWorkload | OpportunisticWorkload):
            raise WorkloadError(
                f"Workload '{workload.name}' is in neither tier; subclass "
                "InteractiveWorkload or OpportunisticWorkload",
                {"workload": workload.name},
            )
        if workload.name in self._entries:
            raise WorkloadError(
                f"A workload named '{workload.name}' is already registered",
                {"workload": workload.name},
            )
        entry = _Entry(workload=workload, tier=type(workload).tier)
        self._entries[workload.name] = entry
        if entry.tier is Tier.INTERACTIVE:
            # Nothing to drive and nothing to call. Registration records that
            # it exists and is not preemptible; it does not hand over a handle.
            return
        if self._state is ComponentState.STARTED:
            if self._should_suspend():
                entry.state = WorkloadState.SUSPENDED
            else:
                self._launch(entry)

    def inventory(self) -> list[WorkloadStatus]:
        return [
            WorkloadStatus(name=e.workload.name, tier=e.tier, state=e.state)
            for e in self._entries.values()
        ]

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        # Two names, therefore two subscriptions and two consumer tasks. See
        # the module docstring on why ordering is settled by `ts`, not arrival.
        self._unsubscribes = [
            self._bus.subscribe(TURN_STARTED_EVENT, self._on_event),
            self._bus.subscribe(TURN_FINISHED_EVENT, self._on_event),
        ]
        self._state = ComponentState.STARTED
        # Applied directly rather than through `_apply_policy`, because boot is
        # the one transition with no hysteresis to serve: there is no previous
        # turn whose follow-up we might be sitting in, so anything registered
        # before `start()` runs now rather than one quiet period from now.
        if self._should_suspend():
            await self._suspend_all()
        else:
            await self._resume_all()

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []
        await self._cancel_pending_resume()
        for entry in self._entries.values():
            if entry.tier is Tier.INTERACTIVE:
                continue
            await self._cancel_task(entry)
            if entry.state not in (WorkloadState.TERMINATED, WorkloadState.FAILED):
                entry.state = WorkloadState.STOPPED
        self._state = ComponentState.STOPPED

    # -- the turn signal -----------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        """Bus entry point. Anything unexpected resolves toward suspension."""
        try:
            await self.observe(event)
        except Exception as exc:  # noqa: BLE001 - an unclassifiable event must not run free
            logger.error(
                "Could not classify a turn event; assuming a turn is live",
                extra={"event_type": event.type, "error": str(exc)},
            )
            await self.mark_indeterminate(f"unclassifiable event: {exc}")

    async def observe(self, event: Event) -> None:
        """Apply a turn event, if it is not older than the last one applied."""
        if event.type == TURN_STARTED_EVENT:
            new_state = TurnState.LIVE
        elif event.type == TURN_FINISHED_EVENT:
            new_state = TurnState.IDLE
        else:
            return
        if not self._is_fresh(event.ts, new_state):
            logger.debug(
                "Ignoring an out-of-order turn event",
                extra={"event_type": event.type, "ts": event.ts.isoformat()},
            )
            return
        self._applied_ts = event.ts
        await self._transition(new_state)

    def _is_fresh(self, ts: datetime, observed: TurnState) -> bool:
        """Is this event new enough to act on?

        Strictly newer always wins. On an exact tie, `LIVE` wins and `IDLE`
        loses: two frames stamped the same instant carry no ordering
        information, and the reading that keeps the device responsive is the
        one that assumes it is busy.
        """
        if self._applied_ts is None:
            return True
        if ts > self._applied_ts:
            return True
        if ts == self._applied_ts:
            return observed is TurnState.LIVE
        return False

    async def mark_indeterminate(self, reason: str) -> None:
        """Declare the turn state unknown — and therefore suspend.

        For whoever notices that the ground truth has moved out from under the
        governor: a backend restart, a resubscribe, a gap in sequence numbers.
        """
        logger.warning("Turn state is indeterminate", extra={"reason": reason})
        await self._transition(TurnState.UNKNOWN)

    async def _transition(self, new_state: TurnState) -> None:
        self._turn_state = new_state
        await self._apply_policy()

    def _should_suspend(self) -> bool:
        return self._turn_state is not TurnState.IDLE

    async def _apply_policy(self) -> None:
        if self._should_suspend():
            await self._cancel_pending_resume()
            await self._suspend_all()
            return
        # Hysteresis: turns arrive in bursts, and resuming an indexer in the
        # two seconds between a question and its follow-up pays a full
        # suspend/resume cycle per turn for no work done.
        await self._schedule_resume()

    # -- suspension ----------------------------------------------------------

    async def _suspend_all(self) -> None:
        for entry in list(self._entries.values()):
            if entry.tier is Tier.INTERACTIVE:
                continue
            await self._suspend_one(entry)

    async def _suspend_one(self, entry: _Entry) -> None:
        if entry.state not in (WorkloadState.RUNNING, WorkloadState.REGISTERED):
            return
        if entry.task is None or entry.ctx is None:
            entry.state = WorkloadState.SUSPENDED
            return
        if entry.task.done():
            entry.state = WorkloadState.COMPLETED
            return

        entry.state = WorkloadState.SUSPENDING
        entry.ctx.request_suspend()

        parked = asyncio.ensure_future(entry.ctx.wait_parked())
        deadline = asyncio.ensure_future(self._clock.sleep(self._config.suspend_deadline_seconds))
        finished = asyncio.ensure_future(asyncio.shield(entry.task))
        try:
            done, _ = await asyncio.wait(
                {parked, deadline, finished}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for pending in (parked, deadline, finished):
                if not pending.done():
                    pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await finished

        if parked in done:
            entry.state = WorkloadState.SUSPENDED
            await self._publish(EVENT_WORKLOAD_SUSPENDED, entry.workload.name)
            return
        if finished in done and deadline not in done:
            entry.state = WorkloadState.COMPLETED
            return
        logger.warning(
            "Workload missed its yield deadline; terminating",
            extra={
                "workload": entry.workload.name,
                "deadline_s": self._config.suspend_deadline_seconds,
            },
        )
        await self._kill(entry)

    async def _kill(self, entry: _Entry) -> None:
        """Cancel, wait briefly, then abandon. Always calls `terminate()`.

        There is no SIGKILL in-process. A coroutine that catches
        `CancelledError` and carries on cannot be stopped, so after the grace
        window the governor stops waiting for it, calls the workload's own
        last-resort hook — where something holding a subprocess or a thread
        kills it — and drops the task on the floor.
        """
        task = entry.task
        if task is not None and not task.done():
            task.cancel()
            grace = asyncio.ensure_future(self._clock.sleep(self._config.terminate_grace_seconds))
            waiter = asyncio.ensure_future(asyncio.shield(task))
            try:
                await asyncio.wait({waiter, grace}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pending in (waiter, grace):
                    if not pending.done():
                        pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiter
        await self._announce_termination(entry)

    async def _announce_termination(self, entry: _Entry) -> None:
        try:
            await entry.workload.terminate()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - already killing it; record and move on
            logger.error(
                "Workload raised from terminate()",
                extra={"workload": entry.workload.name, "error": str(exc)},
            )
        entry.state = WorkloadState.TERMINATED
        entry.task = None
        entry.ctx = None
        await self._publish(EVENT_WORKLOAD_TERMINATED, entry.workload.name)

    # -- resumption ----------------------------------------------------------

    async def _schedule_resume(self) -> None:
        """Start the quiet-period timer, and do not return until it is armed.

        The `armed` handshake is load-bearing rather than tidy. Without it this
        was just `ensure_future`, which returns before the new task has run a
        single line — so a caller that observed `turn_finished` and then moved
        the clock forward by the resume delay moved it *past* a timer that had
        not been created yet, and the resume never fired. On the device that is
        a background workload that stays parked until the next turn ends; in a
        test it is a hang.

        This relies on `Clock.sleep` registering its wakeup before its first
        suspension point, which both implementations do and which is part of
        the protocol's contract.
        """
        await self._cancel_pending_resume()
        armed = asyncio.Event()
        self._resume_task = asyncio.ensure_future(self._resume_after_delay(armed))
        await armed.wait()

    async def _resume_after_delay(self, armed: asyncio.Event) -> None:
        armed.set()
        await self._clock.sleep(self._config.resume_delay_seconds)
        if self._should_suspend():
            # A turn started again inside the quiet period.
            return
        await self._resume_all()

    async def _cancel_pending_resume(self) -> None:
        task = self._resume_task
        self._resume_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _resume_all(self) -> None:
        for entry in self._entries.values():
            if entry.tier is Tier.INTERACTIVE:
                continue
            # TERMINATED is terminal on purpose: a workload that had to be
            # killed does not get the machine back at the next quiet moment
            # just because time passed. REGISTERED is here because a workload
            # registered before `start()` has never been launched at all.
            if entry.state not in (WorkloadState.SUSPENDED, WorkloadState.REGISTERED):
                continue
            if entry.task is None or entry.task.done():
                self._launch(entry)
                continue
            entry.ctx.release()  # type: ignore[union-attr]
            entry.state = WorkloadState.RUNNING
            await self._publish(EVENT_WORKLOAD_RESUMED, entry.workload.name)

    async def wait_settled(self) -> None:
        """Wait for any in-flight resume to finish. For tests and shutdown."""
        task = self._resume_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    # -- driving -------------------------------------------------------------

    def _launch(self, entry: _Entry) -> None:
        workload = entry.workload
        assert isinstance(workload, OpportunisticWorkload)  # noqa: S101 - tier invariant
        entry.ctx = YieldContext(workload.name, self._clock)
        entry.task = asyncio.ensure_future(self._drive(entry, workload, entry.ctx))
        entry.state = WorkloadState.RUNNING

    async def _drive(
        self, entry: _Entry, workload: OpportunisticWorkload, ctx: YieldContext
    ) -> None:
        try:
            await workload.run(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad workload must not take the loop down
            logger.error(
                "Workload raised",
                extra={"workload": workload.name, "error": str(exc)},
            )
            entry.state = WorkloadState.FAILED
            return
        if entry.state in (WorkloadState.RUNNING, WorkloadState.SUSPENDING):
            entry.state = WorkloadState.COMPLETED

    async def _cancel_task(self, entry: _Entry) -> None:
        task = entry.task
        entry.task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _publish(self, event_type: str, workload: str) -> None:
        try:
            await self._bus.publish(
                Event(type=event_type, source=self.name, payload={"workload": workload})
            )
        except Exception as exc:  # noqa: BLE001 - observability must not break governance
            logger.warning(
                "Could not publish governor event",
                extra={"event_type": event_type, "error": str(exc)},
            )
