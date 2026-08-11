"""A turn nobody asked for: Nomad's own scheduled time to improve himself.

Everything else on this device begins a turn because a person did something —
typed into the browser view, pressed a button, spoke. That is why
`AgentSession.send()` had exactly one caller in `src/` and it lived inside
crash recovery: there was no such thing as Nomad starting a conversation. A
pocket agent that can only ever answer is a very good terminal, and the whole
point of D22's scratch-worktree path is that this one is supposed to be able to
work on itself.

So this is a `Component` that owns a task for the life of the device and, on a
tick, starts one turn with `TurnSource.SELF` pointing at the
`improving-yourself` skill. It is small, and almost all of it is the four
things that stop a self-directed loop being a runaway:

* **Off by default.** A device that starts spending tokens because it was
  switched on has made a decision that belongs to whoever pays for them.
  `[triggers.self_improve].enabled` ships `false`; the Pi's gitignored
  `nomad.local.toml` is where it becomes true.
* **Never preempt a live turn.** `send()` serializes on a lock, so the naive
  call does not *fail* while the operator is mid-conversation — it silently
  queues and lands minutes later as an answer to nothing they asked. A skipped
  tick is the honest behaviour, and the next one is only an interval away.
* **Never fire when the backend cannot authenticate.** The device is logged
  out today (`auth: expired`), and a loop that starts a failing turn every
  interval forever is exactly the failure this component must not be. The
  readiness probe is injected rather than reached for, because the one that
  already exists lives in `nomad.status` — a composition-root module this
  package may not import, and a second probe would be a second answer to a
  question with one right one.
* **Bounded by consecutive failures.** Note *consecutive*, and note that a
  failed turn does not raise: `_run_turn` catches a backend failure and returns
  a `FAILED` outcome, so counting exceptions alone would have watched the
  device fail identically forever and called it healthy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from nomad.agent.session import AgentSession, TurnOutcomeStatus
from nomad.core.logging import get_logger
from nomad.core.turns import TurnSource

logger = get_logger(__name__)

#: Answers "could a turn happen right now?". Injected so this package does not
#: have to reach `nomad.status` (see the module docstring); `None` means the
#: caller has no way to ask, which is treated as "go ahead" rather than as
#: "never run" — a trigger the operator explicitly enabled must not be silently
#: disabled by a probe nobody wired.
ReadinessProbe = Callable[[], Awaitable[bool]]

#: What the turn actually says. Deliberately an *invocation* rather than a
#: restatement: D39 puts the skill index in every prompt, so naming the skill
#: is enough and copying its body here would create a second copy of the
#: procedure that drifts from the one on disk the first time either is edited.
#:
#: The last sentence is load-bearing. Without an explicit way to decline, a
#: scheduled "improve yourself" turn is a standing instruction to find
#: something to change, which on a codebase that is currently fine means
#: manufacturing churn and running it past the operator.
SELF_IMPROVE_PROMPT = (
    "Nobody asked for this turn. It is your own scheduled time to work on "
    "yourself, so follow the `improving-yourself` skill exactly: a scratch "
    "worktree with its own venv, never the tree you are running from; "
    "`docs/DECISIONS.md` before you change behaviour; the whole suite green as "
    "the gate; and you report and ask rather than merging anything. Pick one "
    "small improvement that is genuinely worth making — the known gaps in "
    "`docs/BUILD_LEDGER.md` are a good place to look. If nothing is worth "
    "changing right now, say so and end the turn without editing anything; "
    "that is a correct outcome, not a failed one."
)


#: How long after start the first unprompted turn may begin. Short, because an
#: operator who just power-cycled the device should not wait an hour to see it
#: do anything — and a restart usually follows a change, which is when there is
#: most likely to be something worth working on. Not zero, because the panel,
#: the serial link and the backend connection settle in the first seconds and a
#: turn starting into that paints over the status card still being read.
DEFAULT_FIRST_TICK_SECONDS = 120.0


class SelfImproveTrigger:
    """Starts one self-directed turn per interval, and stops if they keep failing."""

    name = "self_improve_trigger"

    def __init__(
        self,
        session: AgentSession,
        *,
        interval_s: float,
        first_tick_s: float = DEFAULT_FIRST_TICK_SECONDS,
        readiness: ReadinessProbe | None = None,
        max_consecutive_failures: int = 3,
        prompt: str = SELF_IMPROVE_PROMPT,
    ) -> None:
        self._session = session
        self._interval_s = interval_s
        #: How long after start the *first* tick happens. Separate from the
        #: interval because they answer different questions: the interval is
        #: how often unattended work is worth doing, and this is how long a
        #: device that was just switched on should sit idle first. Sharing one
        #: number made a restart cost a full hour of nothing.
        self._first_tick_s = max(0.0, first_tick_s)
        self._readiness = readiness
        self._max_consecutive_failures = max_consecutive_failures
        self._prompt = prompt
        self._task: asyncio.Task[None] | None = None
        #: Turns started by `fire_now`, held so the loop keeps a strong
        #: reference to them. An `ensure_future` whose only reference is a
        #: local is collectable mid-flight, and the failure mode is a turn that
        #: stops halfway with nothing in the log saying why.
        self._requested: set[asyncio.Task[None]] = set()
        #: Turns this trigger actually started. Read by tests and diagnostics:
        #: a trigger that is running and has fired nothing means every tick is
        #: being skipped, which is a different problem from one that is off.
        self.turns_started = 0
        #: Consecutive failures. Reset by any turn that does not fail, which is
        #: what makes the cap a runaway brake rather than a lifetime quota.
        self.consecutive_failures = 0
        #: Set when the cap is hit. The loop is over at that point; only a
        #: restart brings it back, deliberately, because whatever is wrong
        #: needs a person.
        self.stopped_after_failures = False

    async def start(self) -> None:
        if self._task is None and self._interval_s > 0:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._requested):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._requested.clear()

    # -- asked for by a person ---------------------------------------------

    def fire_now(self) -> bool:
        """Start a self-directed turn right now. `False` if one is in flight.

        **Not a coroutine that finishes when the turn does.** `send()` returns
        at the *end* of a turn, which is minutes; a button handler or an HTTP
        route that awaited it would be a hung UI holding a request open while
        the operator watches the screen it is already updating. So this starts
        the turn and returns immediately, and the answer arrives the way every
        other answer does — on the glass, over the event bus. It is a plain
        method rather than a coroutine for a second reason: a synchronous check of
        `busy` followed by `ensure_future` has no `await` between them, so on
        D1's one loop no second turn can slip in behind the check.

        Three deliberate differences from the scheduled path:

        * **No readiness probe.** The timer skips a tick when the backend
          cannot authenticate, because a loop that fires into a wall every
          interval forever is the failure that component must not be. A person
          holding the device is the opposite: they are owed the error, and a
          poke that silently does nothing is indistinguishable from a dead
          button.
        * **It does not spend the failure budget.**
          `max_consecutive_failures` is a brake on something unattended. A hand
          is attended by definition, and the count it would consume belongs to
          the timer.
        * **It works after the timer has given up.** `stopped_after_failures`
          silences the schedule until a restart; it must not silence the
          operator, who is very likely poking it precisely because they noticed
          it went quiet.
        """
        if self._session.busy:
            logger.info("Poke ignored: a turn is already in flight")
            return False
        task = asyncio.ensure_future(self._run_requested())
        self._requested.add(task)
        task.add_done_callback(self._finished_requested)
        logger.info("Poke accepted: starting a self-directed turn")
        return True

    def _finished_requested(self, task: asyncio.Task[None]) -> None:
        """Drop the reference and *read the exception*, so asyncio does not
        report it at collection time, far away from what caused it."""
        self._requested.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:  # pragma: no cover - `_run_requested` catches
                logger.error(
                    "Poked turn raised out of its own handler",
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )

    async def _run_requested(self) -> None:
        try:
            outcome = await self._session.send(self._prompt, source=TurnSource.SELF)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the operator gets the error, not a crash
            logger.warning("Poked turn failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            return
        self.turns_started += 1
        logger.info(
            "Poked turn finished",
            extra={"turn_id": outcome.turn_id, "status": str(outcome.status)},
        )

    async def _run(self) -> None:
        # The first wait is its own number. Sleeping a full interval before the
        # first tick meant a device that had just been switched on sat there
        # doing nothing for an hour — and a restart is exactly when there is
        # most likely to be something worth doing, because a restart usually
        # follows a change. The short wait is not zero: the panel, the link and
        # the backend connection all settle in the first seconds of a boot, and
        # a turn that starts into that draws over the status card the operator
        # is still reading.
        delay = self._first_tick_s
        while True:
            await asyncio.sleep(delay)
            delay = self._interval_s
            if not await self._should_fire():
                continue
            if not await self._fire():
                return

    async def _should_fire(self) -> bool:
        """Is now a moment to start a turn nobody asked for?

        The order is not arbitrary. `busy` is checked first because it is free
        and settles most ticks on a device in use; the readiness probe is
        checked second because it may spawn a subprocess and take seconds; and
        `busy` is then checked *again*, because those seconds are long enough
        for the operator to have started typing, and after that check there is
        no `await` before `send` for a turn to slip into.
        """
        if self._session.busy:
            logger.info("Skipping self-improvement: a turn is already in flight")
            return False
        if self._readiness is not None and not await self._is_ready():
            return False
        if self._session.busy:
            logger.info("Skipping self-improvement: a turn began while checking readiness")
            return False
        return True

    async def _is_ready(self) -> bool:
        assert self._readiness is not None
        try:
            ready = await self._readiness()
        except Exception as exc:  # noqa: BLE001 - an unanswerable probe is a skip
            # Fail closed, as every other unanswerable question on this device
            # does: a probe that raised tells us nothing about the backend, and
            # firing anyway is how a logged-out device burns an interval on a
            # turn that cannot run.
            logger.warning(
                "Skipping self-improvement: readiness could not be determined",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return False
        if not ready:
            logger.info("Skipping self-improvement: the backend is not ready to run a turn")
        return ready

    async def _fire(self) -> bool:
        """Run one self-directed turn. `False` means the loop must end.

        A turn that raises must not kill the component — the backend going away
        for one interval is ordinary — so everything is caught and counted. The
        only thing that ends the loop is the same failure happening
        `max_consecutive_failures` times in a row, and it ends loudly.
        """
        try:
            outcome = await self._session.send(self._prompt, source=TurnSource.SELF)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad turn is not a dead trigger
            return self._record_failure(f"{type(exc).__name__}: {exc}")

        self.turns_started += 1
        if outcome.status is TurnOutcomeStatus.FAILED:
            # The case that makes counting exceptions insufficient: the session
            # turns a backend failure into a `FAILED` outcome and returns it.
            return self._record_failure(outcome.error or "turn failed")
        # An interrupted turn is the operator taking the device back, which is
        # working as intended and must not spend the failure budget.
        if outcome.status is TurnOutcomeStatus.COMPLETED:
            self.consecutive_failures = 0
            logger.info("Self-improvement turn finished", extra={"turn_id": outcome.turn_id})
        return True

    def _record_failure(self, error: str) -> bool:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self._max_consecutive_failures:
            self.stopped_after_failures = True
            logger.error(
                "Self-improvement stopped: too many consecutive failures. Nomad will not "
                "start another unprompted turn until he is restarted.",
                extra={"failures": self.consecutive_failures, "error": error},
            )
            return False
        logger.warning(
            "Self-improvement turn failed",
            extra={
                "error": error,
                "failures": self.consecutive_failures,
                "limit": self._max_consecutive_failures,
            },
        )
        return True
