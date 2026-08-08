"""The daily utilities, as tools the model can call.

Same shape as `mcp/hardware.py` and `mcp/memory.py`: a `ToolSpec`, a pydantic
params model, `execute(params, ctx) -> ToolResult`. Every one of these passes
the broker and the `GrantVault` exactly like `display_card` does — being small
and obviously harmless changes how a tool is *decided*, never whether it is
*decided at all* (D4). They are `device_local`, so `decide()` auto-runs them in
every mode (D35), which is the only reason a pocket timer is usable at all;
the grant is still minted, persisted and auditable.

**Two facts shape this file.** The first is that these are the seed corpus for
the offline tier, so nothing here reaches the network, imports a model, or
needs a timezone database beyond `zoneinfo` — a device with the radio off must
be able to answer every one of them. The second is that a small screen and a
context window punish tool sprawl the same way, so `stopwatch` and `note` are
each *one* tool with an `action`, rather than five verbs the model has to hold
in its head and the operator has to scroll past.

Tools whose state is durable are built only when a store exists, matching the
rule `build_hardware_tools` already applies to memory: a device wired without a
database should look like a device with no timers, not like one whose timers
are broken.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from nomad.core.config import UtilitiesConfig
from nomad.notifications.errors import NotificationRefused
from nomad.notifications.models import Notification, NotificationKind, NotificationState
from nomad.notifications.queue import NotificationQueue
from nomad.tools.base import Risk, Tool, ToolContext, ToolResult, ToolSpec
from nomad.utilities.errors import UtilityError
from nomad.utilities.notes import NoteStore
from nomad.utilities.stopwatch import StopwatchStore, format_duration
from nomad.utilities.timers import set_alarm, start_timer
from nomad.utilities.units import convert
from nomad.utilities.worldclock import time_in, zone_difference_minutes


def _now() -> datetime:
    return datetime.now(UTC)


def _line(notification: Notification) -> str:
    when = notification.due_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    repeat = f" [{notification.repeat_rule}]" if notification.repeat_rule else ""
    return f"- {notification.title} — {when}Z ({notification.kind}){repeat} {notification.id}"


# ---------------------------------------------------------------------------
# timers, alarms and the queue they live in
# ---------------------------------------------------------------------------


class SetTimerParams(BaseModel):
    label: str = Field(description="What the timer is for, e.g. 'tea'.")
    minutes: float = Field(
        default=0.0, ge=0.0, le=2880.0, description="Minutes to wait. Combined with seconds."
    )
    seconds: float = Field(
        default=0.0, ge=0.0, le=86400.0, description="Additional seconds to wait."
    )
    note: str = Field(default="", description="Optional detail shown when it fires.")


class SetTimerTool:
    """Start a durable countdown that survives the screen going off."""

    spec = ToolSpec(
        name="set_timer",
        device_local=True,
        description=(
            "Start a timer that survives the screen turning off and the device "
            "rebooting. Fires once, and is delivered late rather than lost."
        ),
        params_model=SetTimerParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(
        self, queue: NotificationQueue, *, config: UtilitiesConfig | None = None
    ) -> None:
        self._queue = queue
        self._config = config or UtilitiesConfig()

    async def execute(self, params: SetTimerParams, ctx: ToolContext) -> ToolResult:
        total = params.minutes * 60 + params.seconds
        try:
            notification = await start_timer(
                self._queue,
                label=params.label,
                seconds=total,
                now=self._queue.now(),
                config=self._config,
                body=params.note,
            )
        except (UtilityError, NotificationRefused) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            f"timer '{notification.title}' set for {format_duration(total)}",
            notification_id=notification.id,
            due_at=notification.due_at.isoformat(),
        )


class SetAlarmParams(BaseModel):
    label: str = Field(description="What the alarm is for, e.g. 'wake up'.")
    hour: int = Field(ge=0, le=23, description="Hour of the day, 24-hour clock.")
    minute: int = Field(default=0, ge=0, le=59, description="Minute of the hour.")
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA zone the time is read in, e.g. 'Europe/London'. Omit for UTC. "
            "A repeating alarm keeps this wall-clock time across DST."
        ),
    )
    repeat: str | None = Field(
        default=None, description="'daily', 'weekly', or omit for a one-off."
    )
    note: str = Field(default="", description="Optional detail shown when it fires.")


class SetAlarmTool:
    """Set a wall-clock alarm, optionally repeating on a calendar rule."""

    spec = ToolSpec(
        name="set_alarm",
        device_local=True,
        description=(
            "Set an alarm for a wall-clock time, optionally repeating daily or "
            "weekly. Keeps its local time across daylight-saving changes."
        ),
        params_model=SetAlarmParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, queue: NotificationQueue) -> None:
        self._queue = queue

    async def execute(self, params: SetAlarmParams, ctx: ToolContext) -> ToolResult:
        try:
            notification = await set_alarm(
                self._queue,
                label=params.label,
                hour=params.hour,
                minute=params.minute,
                now=self._queue.now(),
                tz_name=params.timezone,
                repeat=params.repeat,
                body=params.note,
            )
        except (UtilityError, NotificationRefused) as exc:
            return ToolResult.failure(str(exc))
        repeat = f", repeating {params.repeat}" if params.repeat else ""
        return ToolResult.success(
            f"alarm '{notification.title}' set for "
            f"{notification.due_at.isoformat(timespec='minutes')}{repeat}",
            notification_id=notification.id,
            due_at=notification.due_at.isoformat(),
        )


class NotificationAction(StrEnum):
    PENDING = "pending"
    RECENT = "recent"
    ACKNOWLEDGE = "acknowledge"
    CANCEL = "cancel"


class NotificationsParams(BaseModel):
    action: NotificationAction = Field(
        default=NotificationAction.PENDING,
        description=(
            "pending: what is still waiting. recent: what has already fired. "
            "acknowledge / cancel: resolve one by id."
        ),
    )
    notification_id: str | None = Field(
        default=None, description="Required for acknowledge and cancel."
    )
    limit: int | None = Field(default=None, ge=1, le=50, description="Rows to return.")


class NotificationsTool:
    """Read and resolve the durable queue: timers, alarms, reminders."""

    spec = ToolSpec(
        name="notifications",
        device_local=True,
        description=(
            "List what Nomad is still waiting to tell the operator (timers, "
            "alarms, reminders), what has already fired, or acknowledge and "
            "cancel one by id."
        ),
        params_model=NotificationsParams,
        # MUTATING rather than READ_ONLY: two of the four actions resolve a
        # row. Declaring the whole tool by its most dangerous action is the
        # honest label, and `device_local` is what keeps it usable anyway.
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, queue: NotificationQueue) -> None:
        self._queue = queue

    async def execute(self, params: NotificationsParams, ctx: ToolContext) -> ToolResult:
        if params.action in (NotificationAction.ACKNOWLEDGE, NotificationAction.CANCEL):
            if not params.notification_id:
                return ToolResult.failure(f"{params.action} needs a notification_id")
            if params.action is NotificationAction.ACKNOWLEDGE:
                changed = await self._queue.acknowledge(params.notification_id)
            else:
                changed = await self._queue.cancel(params.notification_id)
            if not changed:
                return ToolResult.failure(
                    f"notification {params.notification_id} is unknown or already resolved"
                )
            return ToolResult.success(f"{params.action}d {params.notification_id}")

        if params.action is NotificationAction.PENDING:
            rows = await self._queue.undelivered(limit=params.limit)
            if not rows:
                return ToolResult.success("nothing pending", count=0)
            listing = "\n".join(_line(row) for row in rows)
            return ToolResult.success(f"{len(rows)} pending:\n{listing}", count=len(rows))

        rows = await self._queue.history(
            states=[
                NotificationState.DELIVERED,
                NotificationState.ACKNOWLEDGED,
                NotificationState.EXPIRED,
                NotificationState.CANCELLED,
            ],
            limit=params.limit,
        )
        if not rows:
            return ToolResult.success("nothing has fired yet", count=0)
        listing = "\n".join(f"{_line(row)} [{row.state}]" for row in rows)
        return ToolResult.success(f"{len(rows)} recent:\n{listing}", count=len(rows))


class SetReminderParams(BaseModel):
    label: str = Field(description="What to be reminded of.")
    minutes: float = Field(
        gt=0.0, le=100000.0, description="How long from now, in minutes."
    )
    repeat_minutes: float | None = Field(
        default=None, gt=0.0, description="Repeat every this many minutes. Omit for one-off."
    )
    note: str = Field(default="", description="Optional detail.")


class SetReminderTool:
    """A durable reminder, optionally on a fixed interval."""

    spec = ToolSpec(
        name="set_reminder",
        device_local=True,
        description=(
            "Remind the operator of something after a delay, optionally "
            "repeating on a fixed interval. Survives reboots."
        ),
        params_model=SetReminderParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, queue: NotificationQueue) -> None:
        self._queue = queue

    async def execute(self, params: SetReminderParams, ctx: ToolContext) -> ToolResult:
        now = self._queue.now()
        rule = (
            f"interval:{int(params.repeat_minutes * 60)}"
            if params.repeat_minutes is not None
            else None
        )
        try:
            notification = await self._queue.raise_notification(
                params.label,
                body=params.note,
                kind=NotificationKind.REMINDER,
                due_at=now + timedelta(minutes=params.minutes),
                dedup_key=f"reminder:{params.label.casefold().strip()}",
                repeat_rule=rule,
                source="utilities",
            )
        except NotificationRefused as exc:
            return ToolResult.failure(str(exc))
        when = notification.due_at.isoformat(timespec="minutes")
        return ToolResult.success(
            f"reminder '{notification.title}' due {when}",
            notification_id=notification.id,
            due_at=notification.due_at.isoformat(),
        )


# ---------------------------------------------------------------------------
# stopwatch
# ---------------------------------------------------------------------------


class StopwatchAction(StrEnum):
    START = "start"
    READ = "read"
    LAP = "lap"
    STOP = "stop"
    RESUME = "resume"
    RESET = "reset"
    LIST = "list"


class StopwatchParams(BaseModel):
    action: StopwatchAction = Field(default=StopwatchAction.READ, description="What to do.")
    stopwatch_id: str | None = Field(
        default=None, description="Required for everything except start and list."
    )
    label: str = Field(default="stopwatch", description="Name, used by start.")


class StopwatchTool:
    """One tool, seven verbs — a stopwatch that nothing has to tick."""

    spec = ToolSpec(
        name="stopwatch",
        device_local=True,
        description=(
            "Start, read, lap, stop, resume or reset a stopwatch. Elapsed time "
            "is computed from stored timestamps, so it survives a reboot."
        ),
        params_model=StopwatchParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, store: StopwatchStore) -> None:
        self._store = store

    async def execute(self, params: StopwatchParams, ctx: ToolContext) -> ToolResult:
        now = self._store.now()
        try:
            if params.action is StopwatchAction.START:
                watch = await self._store.start(params.label)
                return ToolResult.success(
                    f"started '{watch.label}'", stopwatch_id=watch.id
                )
            if params.action is StopwatchAction.LIST:
                watches = await self._store.list_all()
                if not watches:
                    return ToolResult.success("no stopwatches", count=0)
                listing = "\n".join(
                    f"- {w.label}: {format_duration(w.elapsed_seconds(now))} "
                    f"({'running' if w.running else 'stopped'}) {w.id}"
                    for w in watches
                )
                return ToolResult.success(
                    f"{len(watches)} stopwatch(es):\n{listing}", count=len(watches)
                )
            if not params.stopwatch_id:
                return ToolResult.failure(f"{params.action} needs a stopwatch_id")
            if params.action is StopwatchAction.READ:
                watch = await self._store.get(params.stopwatch_id)
                if watch is None:
                    return ToolResult.failure(f"no stopwatch with id {params.stopwatch_id}")
                elapsed = watch.elapsed_seconds(now)
                return ToolResult.success(
                    f"{watch.label}: {format_duration(elapsed)} "
                    f"({'running' if watch.running else 'stopped'})",
                    elapsed_seconds=elapsed,
                    running=watch.running,
                )
            if params.action is StopwatchAction.LAP:
                watch, split = await self._store.lap(params.stopwatch_id)
                return ToolResult.success(
                    f"lap {len(watch.laps)}: {format_duration(split)} "
                    f"(total {format_duration(watch.elapsed_seconds(now))})",
                    lap=len(watch.laps),
                    split_seconds=split,
                )
            if params.action is StopwatchAction.STOP:
                watch = await self._store.stop(params.stopwatch_id)
                return ToolResult.success(
                    f"stopped '{watch.label}' at "
                    f"{format_duration(watch.accumulated_seconds)}",
                    elapsed_seconds=watch.accumulated_seconds,
                )
            if params.action is StopwatchAction.RESUME:
                watch = await self._store.resume(params.stopwatch_id)
                return ToolResult.success(f"resumed '{watch.label}'", running=True)
            cleared = await self._store.reset(params.stopwatch_id)
            if not cleared:
                return ToolResult.failure(f"no stopwatch with id {params.stopwatch_id}")
            return ToolResult.success(f"reset {params.stopwatch_id}")
        except UtilityError as exc:
            return ToolResult.failure(str(exc))


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


class NoteAction(StrEnum):
    ADD = "add"
    GET = "get"
    APPEND = "append"
    EDIT = "edit"
    DELETE = "delete"
    SEARCH = "search"


class NoteParams(BaseModel):
    action: NoteAction = Field(default=NoteAction.SEARCH, description="What to do.")
    note_id: str | None = Field(default=None, description="Required except for add/search.")
    title: str = Field(default="", description="Title, for add and edit.")
    body: str = Field(default="", description="Body, for add and edit.")
    text: str = Field(default="", description="Line to append, for append.")
    tags: list[str] = Field(default_factory=list, max_length=8, description="Optional tags.")
    query: str = Field(default="", description="Search text. Empty returns the most recent.")
    limit: int | None = Field(default=None, ge=1, le=25, description="Rows for search.")


class NoteTool:
    """Notes the operator wrote, kept separate from what Nomad remembers."""

    spec = ToolSpec(
        name="note",
        device_local=True,
        description=(
            "Write, read, append to, edit, delete or search the operator's "
            "notes. Distinct from `remember`: notes are theirs and are never "
            "injected into a prompt."
        ),
        params_model=NoteParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, store: NoteStore) -> None:
        self._store = store

    async def execute(self, params: NoteParams, ctx: ToolContext) -> ToolResult:
        preview = self._store.preview_chars
        try:
            if params.action is NoteAction.ADD:
                note = await self._store.add(
                    params.title, body=params.body, tags=params.tags
                )
                return ToolResult.success(f"noted: {note.title}", note_id=note.id)
            if params.action is NoteAction.SEARCH:
                notes = await self._store.search(params.query, limit=params.limit)
                if not notes:
                    subject = f" matching '{params.query}'" if params.query else ""
                    return ToolResult.success(f"no notes{subject}", count=0)
                listing = "\n".join(
                    f"- {n.title}: {n.body[:preview]}{'…' if len(n.body) > preview else ''}"
                    f" ({n.id})"
                    for n in notes
                )
                return ToolResult.success(f"{len(notes)} note(s):\n{listing}", count=len(notes))
            if not params.note_id:
                return ToolResult.failure(f"{params.action} needs a note_id")
            if params.action is NoteAction.GET:
                note = await self._store.get(params.note_id)
                if note is None or note.deleted_at is not None:
                    return ToolResult.failure(f"no note with id {params.note_id}")
                return ToolResult.success(f"{note.title}\n{note.body}", note_id=note.id)
            if params.action is NoteAction.DELETE:
                if not await self._store.delete(params.note_id):
                    return ToolResult.failure(f"no note with id {params.note_id}")
                return ToolResult.success(f"deleted {params.note_id}")
            note = await self._store.update(
                params.note_id,
                title=params.title or None,
                body=params.body if params.action is NoteAction.EDIT and params.body else None,
                append=params.text if params.action is NoteAction.APPEND else None,
                tags=params.tags or None,
            )
            return ToolResult.success(f"updated: {note.title}", note_id=note.id)
        except UtilityError as exc:
            return ToolResult.failure(str(exc))


# ---------------------------------------------------------------------------
# pure conversions — no store, no clock, no network
# ---------------------------------------------------------------------------


class ConvertUnitsParams(BaseModel):
    value: float = Field(description="The number to convert.")
    from_unit: str = Field(description="Source unit, e.g. 'km' or 'celsius'.")
    to_unit: str = Field(description="Target unit, e.g. 'mi' or 'f'.")


class ConvertUnitsTool:
    """Offline unit conversion across length, mass, temperature and more."""

    spec = ToolSpec(
        name="convert_units",
        device_local=True,
        description=(
            "Convert between units of length, mass, temperature, volume, speed, "
            "data or duration. Works with no network."
        ),
        params_model=ConvertUnitsParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    async def execute(self, params: ConvertUnitsParams, ctx: ToolContext) -> ToolResult:
        try:
            result = convert(params.value, params.from_unit, params.to_unit)
        except UtilityError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            f"{params.value:g} {result.source_symbol} = {result.value:g} {result.symbol}",
            value=result.value,
            unit=result.unit,
            symbol=result.symbol,
            dimension=result.dimension,
        )


class WorldClockParams(BaseModel):
    zone: str = Field(description="Place or IANA zone, e.g. 'tokyo' or 'Asia/Tokyo'.")
    compare_to: str | None = Field(
        default=None, description="Optional second zone to compare against."
    )


class WorldClockTool:
    """What time it is somewhere else, and how far away that is."""

    spec = ToolSpec(
        name="world_clock",
        device_local=True,
        description=(
            "Report the current time in a city or IANA timezone, with its UTC "
            "offset and DST state, optionally compared to a second zone."
        ),
        params_model=WorldClockParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        # Injectable so a test never depends on wall time, and so the device
        # can later hand this a clock corrected by something other than NTP.
        self._clock = clock or _now

    async def execute(self, params: WorldClockParams, ctx: ToolContext) -> ToolResult:
        now = self._clock()
        try:
            here = time_in(params.zone, now)
            if params.compare_to is None:
                return ToolResult.success(
                    f"{here.local} {here.abbreviation} ({here.zone}), "
                    f"{here.day_of_week}, UTC{here.utc_offset_minutes // 60:+d}",
                    **here.model_dump(),
                )
            there = time_in(params.compare_to, now)
            delta = zone_difference_minutes(params.zone, params.compare_to, now)
        except UtilityError as exc:
            return ToolResult.failure(str(exc))
        direction = "ahead of" if delta >= 0 else "behind"
        return ToolResult.success(
            f"{here.zone}: {here.local} {here.abbreviation}\n"
            f"{there.zone}: {there.local} {there.abbreviation}\n"
            f"{there.zone} is {abs(delta) // 60}h{abs(delta) % 60:02d} {direction} {here.zone}",
            difference_minutes=delta,
        )


def build_utility_tools(
    *,
    notifications: NotificationQueue | None = None,
    notes: NoteStore | None = None,
    stopwatches: StopwatchStore | None = None,
    config: UtilitiesConfig | None = None,
) -> list[Tool]:
    """The utility suite. Pure tools always; durable tools when a store exists.

    The asymmetry is deliberate and matches `build_hardware_tools`: converting
    kilometres needs nothing, so refusing to offer it on a device with no
    database would be inventing a dependency. A timer genuinely needs somewhere
    durable to live, and a timer that silently forgets is worse than a device
    that plainly has none.
    """
    config = config or UtilitiesConfig()
    tools: list[Tool] = [ConvertUnitsTool(), WorldClockTool()]
    if notifications is not None:
        tools.extend(
            [
                SetTimerTool(notifications, config=config),
                SetAlarmTool(notifications),
                SetReminderTool(notifications),
                NotificationsTool(notifications),
            ]
        )
    if notes is not None:
        tools.append(NoteTool(notes))
    if stopwatches is not None:
        tools.append(StopwatchTool(stopwatches))
    return tools
