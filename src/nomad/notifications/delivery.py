"""The thing that was actually watching (D36, D38).

Chunks N and U built a durable queue, a timer tool, an alarm tool and a
reminder tool, all green. Nothing in `src/` ever called `deliver_due()`. So the
device answered "timer '5 minutes' set for 5m 00s" and then, five minutes
later, did nothing at all — a promise made in writing and broken in silence,
which is a worse failure than refusing to set the timer. This module is the
missing consumer, and it is deliberately the *only* thing in the tree that
knows both that notifications exist and that a screen exists.

**Why the screen is asked, not written to.** `ScreenOwner` arbitrates three
writers already (D36): the turn renderer, the model's `display_*` tools, and
the authorization prompt. A notification is a fourth, and it is the one for
which the existing rule — a suppressed frame is simply dropped — is wrong.
Dropping is safe for the renderer because `agent.turn_finished` redraws the
whole answer authoritatively; nothing ever redraws a fired timer. A dropped
notification is gone, and the row would already have been marked delivered.

So the arbitration is: **the notification never waits for the screen and never
takes it from anyone.** Three cases, and each one falls out of that sentence:

* *An authorization prompt holds the screen.* The sink is refused, raises
  `NotificationDeferred`, and `deliver_due` leaves the row `PENDING` (that is
  the property `queue.py` documents and the reason its sink is a parameter).
  The next poll shows it, a second later, with nothing lost. Waiting for the
  prompt instead would be worse than dropping: the notification would land the
  instant the prompt resolved, on top of the "Approved" / "Denied"
  confirmation the operator is reading.
* *Nobody holds the screen.* The notification takes it — exclusively, for a
  short dwell — because the alternative is being overwritten by the next
  streamed chunk of a live turn, and a reminder that was on the glass for
  120 ms was not delivered in any sense the operator would accept. Suppressing
  renderer frames for those few seconds is safe for exactly the reason D36
  already relies on: the turn redraws itself when it finishes.
* *The screen is claimed while the sink is drawing.* Cannot happen — the claim
  is held across the whole draw, and `claim_if_free` acquires it atomically.

**Speaking is a seam, not a call.** D37 gives the device a `speak` tool and no
`listen` tool at any price, and `audio/selection.py` currently resolves `alsa`
to a `NotImplementedError` stub with nothing plugged into the jack. So this
module takes an optional `Announce` callable and, if there is one, calls it
*after* the screen frame has landed and the claim has been released, with any
failure logged and swallowed. Drawing is what "delivered" means; a speaker that
does not exist, or a synthesizer that raises, must never turn a delivered
reminder into one that retries forever. Nothing here constructs a `Recorder` or
a `Transcriber` — there is no microphone path in this direction, by decision.

**D38 puts this in the `INTERACTIVE` tier**, so it is registered as a workload
that can never be suspended. That is the point of the tier and not a
formality: a timer that stops firing whenever the device is busy is a timer the
operator learns not to trust, and "Nomad is thinking" must never become "Nomad
is not listening to its own clock". The tier is a declaration with no handle
(`InteractiveWorkload`), so the poll loop is an ordinary `Component` started by
`ComponentRegistry` like everything else, and registration happens in the
composition root — an import of `resources` here would put a resource policy
underneath storage.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.notifications.errors import NotificationDeferred
from nomad.notifications.models import Notification, NotificationKind
from nomad.notifications.queue import NotificationQueue

logger = get_logger(__name__)

#: The name this writer draws under, and the name it registers as a workload.
#: One string for both, because "the thing that may not be suspended" and "the
#: thing that owns the screen while a reminder is up" are the same thing.
NOTIFICATION_WRITER = "notifications"

#: How often due rows are looked for. A second is the granularity the operator
#: can perceive on a timer, and two SQLite reads a second on an idle device is
#: not the thing that will cost this Pi its battery.
DEFAULT_POLL_SECONDS = 1.0

#: How long a delivered notification keeps the screen. Long enough to read a
#: line and look up; short enough that it cannot hold an authorization prompt
#: off the glass for a meaningful fraction of that prompt's own timeout.
DEFAULT_DWELL_SECONDS = 4.0

#: Chrome, chosen by Nomad from the row's `kind` — never from its text. The
#: operator has to be able to tell "your timer went off" from a line the model
#: wrote, and the title is the half of the frame the model does not author.
_TITLES: dict[NotificationKind, str] = {
    NotificationKind.TIMER: "Timer",
    NotificationKind.ALARM: "Alarm",
    NotificationKind.REMINDER: "Reminder",
    NotificationKind.SYSTEM: "Nomad",
    NotificationKind.AGENT: "Notification",
}

#: Say it out loud, when there is something to say it with. Text in, nothing
#: out: a sink that could read a result back from the audio path would be the
#: beginning of the capture route D37 forbids.
Announce = Callable[[str], Awaitable[None]]


class TextScreen(Protocol):
    """The one drawing call a notification needs. `ScreenView` satisfies it."""

    async def show_text(self, text: str, *, title: str | None = None) -> None: ...


class ArbitratedScreen(Protocol):
    """The half of `ScreenOwner` this module uses, named structurally.

    A Protocol and not an import: `notifications` may see `core` and `storage`
    and nothing else (`tests/test_layering.py`), because the queue sits below
    the loop and must stay answerable with everything above it wedged. The
    same trick `input/choice.py` uses to draw without importing `hardware`.
    """

    def claim_if_free(self, writer: str) -> contextlib.AbstractAsyncContextManager[
        TextScreen | None
    ]: ...


class ScreenNotificationSink:
    """Draws one due notification, or declines and leaves it pending."""

    def __init__(
        self,
        screen: ArbitratedScreen,
        *,
        announce: Announce | None = None,
        dwell_seconds: float = DEFAULT_DWELL_SECONDS,
    ) -> None:
        self._screen = screen
        self._announce = announce
        self._dwell_seconds = dwell_seconds
        self.shown = 0
        self.deferred = 0

    async def __call__(self, notification: Notification) -> None:
        """The `NotificationSink` signature: return means delivered."""
        async with self._screen.claim_if_free(NOTIFICATION_WRITER) as view:
            if view is None:
                self.deferred += 1
                raise NotificationDeferred(
                    "the screen is held by another writer; this notification stays pending",
                    {"notification_id": notification.id, "title": notification.title},
                )
            await view.show_text(_body(notification), title=_title(notification))
            self.shown += 1
            logger.info(
                "Delivered a notification to the screen",
                extra={
                    "notification_id": notification.id,
                    "kind": str(notification.kind),
                    "due_at": notification.due_at.isoformat(),
                },
            )
            if self._dwell_seconds > 0:
                await asyncio.sleep(self._dwell_seconds)
        # Outside the claim on purpose: speech is slow, best-effort, and must
        # not hold the screen away from an authorization prompt while it runs.
        await self._speak(notification)

    async def _speak(self, notification: Notification) -> None:
        if self._announce is None:
            return
        try:
            await self._announce(_body(notification))
        except asyncio.CancelledError:  # pragma: no cover - propagate shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - a mute device is still delivered
            logger.warning(
                "Could not speak a delivered notification; it is on the screen",
                extra={"notification_id": notification.id, "error": str(exc)},
            )


def _title(notification: Notification) -> str:
    return _TITLES.get(notification.kind, "Notification")


def _body(notification: Notification) -> str:
    if notification.body:
        return f"{notification.title}\n{notification.body}"
    return notification.title


class NotificationDelivery:
    """Polls the queue and hands due rows to a sink. `INTERACTIVE` (D38).

    A poll and not a scheduled wakeup, deliberately. The queue is a table that
    survives reboots and that anything may write to at any moment — a timer
    tool, a repeat re-arming itself, a restated row moving its own `due_at`
    backwards — so a task sleeping until the next known due time would have to
    be woken by every one of those writers, and a missed wakeup is a promise
    silently dropped. One second of polling buys the property that the only
    thing that can lose a notification is the row not being there.
    """

    name = NOTIFICATION_WRITER

    def __init__(
        self,
        queue: NotificationQueue,
        sink: Callable[[Notification], Awaitable[None]],
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._queue = queue
        self._sink = sink
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._state = ComponentState.NEW
        self.delivered = 0
        self.polls = 0

    @property
    def state(self) -> ComponentState:
        return self._state

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._task = asyncio.create_task(self._run(), name="notification-delivery")
        self._state = ComponentState.STARTED

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._state = ComponentState.STOPPED

    async def poll_once(self) -> int:
        """One pass. Returns how many rows were delivered.

        Public so a test — and the loop below — share one code path, and so
        that "did the device deliver anything" is answerable without waiting
        for a tick.
        """
        self.polls += 1
        delivered = await self._queue.deliver_due(self._sink)
        self.delivered += delivered
        return delivered

    async def _run(self) -> None:
        """Poll forever. Nothing a sink or the database does may end this task.

        A delivery loop that dies on the first exception is the same class of
        bug as no delivery loop at all, and harder to notice — so every failure
        is logged and the next tick happens anyway. Cancellation is the only
        way out, which is what `stop()` uses.
        """
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except NotificationDeferred as deferred:
                # The expected case while an authorization prompt is up. The
                # row is still pending; there is nothing to repair.
                logger.debug("Notification deferred", extra={"reason": str(deferred)})
            except Exception as exc:  # noqa: BLE001 - the loop outlives its failures
                logger.warning(
                    "Notification delivery failed; the rows stay pending",
                    extra={"error": str(exc)},
                )
            await asyncio.sleep(self._poll_seconds)
