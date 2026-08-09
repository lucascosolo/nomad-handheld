"""A clock the governor can be lied to about.

Every delay in this package is a policy number — how long a workload gets to
yield before it is killed, how long the device waits after a turn before it
trusts the quiet — and a policy number has to be *tested*, not slept through.
A suite that proves the hysteresis window by waiting three real seconds is
three seconds slower per case on a two-core laptop, and it is the kind of test
that fails once a month under load and gets deleted rather than fixed.

So nothing in `nomad.resources` calls `asyncio.sleep` for a policy delay.
Waiting goes through a `Clock`: `SystemClock` on the device, `ManualClock` in a
test, where time moves only when the test moves it. The seam is the same one
D9 draws everywhere else — the real driver is the special case, not the default.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

#: Loop iterations `ManualClock` yields after waking a sleeper, so the tasks it
#: woke actually run before `advance()` returns. Small and fixed: this is a
#: scheduling courtesy, not a timeout.
_SETTLE_TICKS = 8

#: Free loop yields `wait_for_sleepers` spends before it starts sleeping for
#: real. Enough that a workload which only awaits other coroutines is caught on
#: the fast path with no wall-clock cost at all.
_MAX_POLL_TICKS = 500

#: The real-time bound, used once the free ticks are spent.
#:
#: This used to be loop iterations *only*, with a comment claiming that made it
#: machine-independent. It made it worse. A workload that reaches `sleep()`
#: after awaiting the database is waiting on a worker thread, and 500 loop
#: iterations of `asyncio.sleep(0)` elapse in well under a millisecond — so the
#: bound expired before SQLite could answer, and whether that was a pass or a
#: failure came down to how fast the disk was. It passed on the laptop it was
#: written on and failed on the device, every single run.
_POLL_TIMEOUT_S = 5.0

#: How long each poll parks once real waiting starts. Short enough to keep a
#: fast case fast, long enough that the loop can service I/O between checks.
_POLL_INTERVAL_S = 0.002


@runtime_checkable
class Clock(Protocol):
    """Monotonic time and the ability to wait for some of it."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real one. The only implementation that ever runs on the device."""

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """Time that advances only when a test says so.

    Shipped in `src` rather than in the suite on purpose: the chunk that wires
    the governor into the composition root needs the same determinism, and a
    fake clock copied into two test files drifts apart.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    @property
    def sleepers(self) -> int:
        """How many callers are currently parked in `sleep`."""
        return len(self._sleepers)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        entry = (self._now + seconds, asyncio.get_running_loop().create_future())
        self._sleepers.append(entry)
        try:
            await entry[1]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    async def advance(self, seconds: float) -> None:
        """Move time forward, wake whoever is due, and let them run."""
        self._now += seconds
        for entry in [e for e in self._sleepers if e[0] <= self._now]:
            if entry in self._sleepers:
                self._sleepers.remove(entry)
            if not entry[1].done():
                entry[1].set_result(None)
        await self.settle()

    async def wait_for_sleepers(self, count: int = 1, *, timeout: float | None = None) -> None:
        """Return once `count` callers are parked in `sleep`.

        This is how a test says "the governor has entered its deadline wait"
        without guessing at a number of `await`s.

        Two phases, and the second one is the fix. Free loop yields first, so a
        workload that only awaits coroutines is caught immediately and costs
        nothing. Then real, short sleeps against a wall-clock deadline, because
        a workload on its way to `sleep()` may be waiting on the database —
        which is a worker thread, not a coroutine, and no number of free yields
        will make a thread finish sooner.
        """
        for _ in range(_MAX_POLL_TICKS):
            if len(self._sleepers) >= count:
                return
            await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (_POLL_TIMEOUT_S if timeout is None else timeout)
        while loop.time() < deadline:
            if len(self._sleepers) >= count:
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise TimeoutError(f"expected {count} sleeper(s), saw {len(self._sleepers)}")

    async def settle(self) -> None:
        """Yield the loop enough times for woken tasks to make progress."""
        for _ in range(_SETTLE_TICKS):
            await asyncio.sleep(0)
