"""Two tiers, and why one of them has no `suspend` method (D38).

The tier a workload registers in is a promise about *preemption*, not a
priority number somebody tunes later. There are exactly two, and the promise
each makes is absolute:

- `INTERACTIVE` — never preempted, by anything, ever. The screen, the input
  stream, the notification queue, the tool endpoints, the authorization prompt,
  and speech-to-text. That last one is the counterintuitive member: STT is the
  heaviest thing on the device and the obvious thing to pause, and pausing it
  is exactly wrong, because the operator speaks *most* while a turn is running.
  Being on the path from operator to machine picks the tier; weight does not.
- `OPPORTUNISTIC` — suspended whenever a turn is live. Index building,
  promotion analysis, model warmup. These make the device better later, and
  later is always negotiable.

D38 says the governor "must never be able to suspend an `INTERACTIVE` workload
even if asked — that is a structural property, not a policy check, or the first
bug in it takes the UI down." So `InteractiveWorkload` has no `run`, no
`suspend`, no gate, and no task: it is a *declaration* that something exists and
is not preemptible. The governor keeps it in a differently-typed collection
from the preemptible one and there is nothing on it to call. "Suspend the
screen" is not a thing this code can express incorrectly, because it is not a
thing it can express.

Only `OpportunisticWorkload` gets a body the governor drives, and it is driven
through a `YieldContext` — cooperative first, fatal second. Cooperation cannot
be assumed: a promoted offline handler is model-authored code (D25, D29), so
the workload is asked to park, and the task is cancelled and abandoned if it
does not. The context is the only thing that makes cooperating cheap enough
that well-behaved workloads survive; the deadline is what makes the rest safe.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import ClassVar

from nomad.core.logging import get_logger
from nomad.resources.clock import Clock

logger = get_logger(__name__)


class Tier(StrEnum):
    """A promise about preemption. Not a priority, and not orderable."""

    INTERACTIVE = "interactive"
    OPPORTUNISTIC = "opportunistic"


class WorkloadState(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    #: Missed its yield deadline and was killed. Terminal: never resumed.
    TERMINATED = "terminated"
    FAILED = "failed"
    STOPPED = "stopped"


class Workload(ABC):
    """Anything that consumes real CPU or memory in the background (D38)."""

    tier: ClassVar[Tier]

    def __init__(self, name: str) -> None:
        self.name = name


class InteractiveWorkload(Workload):
    """Registered, never driven, and structurally impossible to suspend.

    It has no methods on purpose. Everything in this tier is already a
    `Component` started by the registry — the screen, the input stream, the
    voice path. Registering it here records that it exists and that it is not
    preemptible; it does not hand the governor a handle on it, because a handle
    is the thing that could be misused.
    """

    tier: ClassVar[Tier] = Tier.INTERACTIVE


class YieldContext:
    """The seam an opportunistic workload cooperates through.

    A workload's `run` awaits `checkpoint()` between units of work — that is
    the whole contract. When the governor asks for the machine back, the next
    checkpoint parks the workload where its own invariants hold, which is the
    only kind of suspension that is safe for something holding a half-written
    index. Anything long enough to matter should be waited on with `sleep()`,
    which parks immediately instead of holding the deadline open.
    """

    def __init__(self, name: str, clock: Clock) -> None:
        self._name = name
        self._clock = clock
        self._suspend_requested = False
        self._suspend_signal = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._parked = asyncio.Event()

    @property
    def suspend_requested(self) -> bool:
        """True once the governor has asked for the machine back.

        A workload that wants to unwind its own way — flush a partial index,
        drop a cache — reads this rather than waiting to be cancelled.
        """
        return self._suspend_requested

    async def checkpoint(self) -> None:
        """Park here if the governor has asked; otherwise yield and continue.

        Always yields the loop, even on the fast path. A workload whose only
        `await` is this call still cooperates, which is worth one tick.
        """
        if not self._suspend_requested:
            await asyncio.sleep(0)
            return
        self._parked.set()
        try:
            await self._resume.wait()
        finally:
            self._parked.clear()

    async def sleep(self, seconds: float) -> None:
        """Wait, but wake immediately if the governor wants the machine.

        A poller that slept through its yield deadline would be killed for
        being idle, which is the wrong outcome for the best-behaved workload
        on the device.
        """
        await self.checkpoint()
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        signal = asyncio.ensure_future(self._suspend_signal.wait())
        try:
            await asyncio.wait({timer, signal}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (timer, signal):
                if not pending.done():
                    pending.cancel()
        await self.checkpoint()

    # -- governor side ----------------------------------------------------
    # Not for workloads to call. Synchronous by design: asking is instant,
    # and only the waiting-for-an-answer part is allowed to take time.

    def request_suspend(self) -> None:
        self._suspend_requested = True
        self._resume.clear()
        self._suspend_signal.set()

    def release(self) -> None:
        self._suspend_requested = False
        self._suspend_signal.clear()
        self._resume.set()

    async def wait_parked(self) -> None:
        await self._parked.wait()

    @property
    def parked(self) -> bool:
        return self._parked.is_set()


class OpportunisticWorkload(Workload):
    """Background work that yields the machine whenever a turn is live.

    The governor owns the task running `run`, which is what makes the deadline
    enforceable: a workload that keeps its own task could only ever be *asked*
    to stop.
    """

    tier: ClassVar[Tier] = Tier.OPPORTUNISTIC

    @abstractmethod
    async def run(self, ctx: YieldContext) -> None:
        """Do the work, awaiting `ctx.checkpoint()` between units of it.

        Returning is allowed and final: a workload that finishes is `COMPLETED`
        and is not restarted on the next resume.
        """

    async def terminate(self) -> None:
        """Last resort, called after the yield deadline is missed.

        Cancelling the task is the governor's first move; this hook exists for
        a workload holding something cancellation cannot reach — a subprocess,
        a thread, an mmap. Must be idempotent and must not raise; it is called
        whether or not the cancellation landed.
        """
        return None
