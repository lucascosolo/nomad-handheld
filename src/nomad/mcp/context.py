"""Ambient context: what the device knows about the moment it is in.

`get_context` used to live next to the driver facades in `mcp/hardware.py`, and
it has been moved here because it is not one of them. Every other tool in that
module is a thin call onto a single piece of silicon; this one is a *composite
read* — clock, battery, radio, motion — whose value is entirely in the
combination. Leaving it there meant every widening of it widened the facade
module, and the facades are supposed to stay boring.

The combination is what makes "understands my needs" mean anything. A trigger
that fires at 03:00 while the device is face-down on a nightstand and one that
fires at 14:00 while it is being carried are the same trigger with different
answers, and the only thing that separates them is this read. Which is also why
`time_of_day` is computed here rather than left to the model: it is a policy
input other code will branch on, and a threshold each caller re-derives from an
ISO string is a threshold four callers will disagree about.

Everything is injectable — clock, network probe, drivers — because the whole
surface has to be exercisable on a laptop with no hardware and no network (D9),
and because a test that depends on the real time of day is a test that fails at
midnight.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from nomad.mcp.hardware import BatteryDriver
from nomad.tools.base import Risk, ToolContext, ToolResult, ToolSpec


class TimeOfDay(StrEnum):
    """The coarse bucket a trigger actually branches on.

    Four buckets, not twenty-four hours. The distinction that matters to a
    notification policy is "is the operator plausibly asleep", and a policy
    written against an hour number ends up re-deriving these boundaries in
    every call site with a different answer.
    """

    NIGHT = "night"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


#: Boundaries as local hours, low-inclusive. Deliberately constants rather than
#: config: this is a shared vocabulary, and a device where "night" means
#: something different from one install to the next cannot have a policy
#: written against it.
NIGHT_ENDS_HOUR = 6
MORNING_ENDS_HOUR = 12
AFTERNOON_ENDS_HOUR = 18
EVENING_ENDS_HOUR = 22


def classify_time_of_day(moment: datetime) -> TimeOfDay:
    """Bucket a local time. Anything from 22:00 to 05:59 is night."""
    hour = moment.hour
    if hour < NIGHT_ENDS_HOUR or hour >= EVENING_ENDS_HOUR:
        return TimeOfDay.NIGHT
    if hour < MORNING_ENDS_HOUR:
        return TimeOfDay.MORNING
    if hour < AFTERNOON_ENDS_HOUR:
        return TimeOfDay.AFTERNOON
    return TimeOfDay.EVENING


class MotionReading(BaseModel):
    """Whether the device is being carried, as much as a tool needs to know.

    A boolean and a magnitude, not an accelerometer vector. The question this
    answers is "is anyone holding it", and a tool handed three axes would start
    inventing gesture recognition in the model's context window.
    """

    moving: bool = False
    #: Unitless agitation, 0.0 at rest. Comparable across readings from the
    #: same driver and across nothing else.
    magnitude: float = 0.0
    #: False when no motion sensor is fitted, so a consumer can tell "still"
    #: from "unknown" — the distinction a 3am trigger depends on.
    available: bool = True


@runtime_checkable
class MotionDriver(Protocol):
    """The motion sensor, if one is fitted. Structural, like every other facade."""

    async def read(self) -> MotionReading: ...


class MockMotion:
    """The default (D9). Still, present, and settable by a test."""

    def __init__(self, reading: MotionReading | None = None) -> None:
        self.reading = reading or MotionReading(moving=False, magnitude=0.0, available=True)

    async def read(self) -> MotionReading:
        return self.reading


class AbsentMotion:
    """What a device with no accelerometer reports.

    Distinct from `MockMotion` on purpose: "not moving" and "cannot tell" lead
    to different decisions at 3am, and a facade that collapses them would make
    the honest answer unreachable.
    """

    async def read(self) -> MotionReading:
        return MotionReading(moving=False, magnitude=0.0, available=False)


NetworkCheck = Callable[[], Awaitable[bool]]


async def _default_network_check() -> bool:
    """A quick, best-effort reachability probe. Never raises — offline is a
    normal answer, not a failure of this tool."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("1.1.1.1", 53), timeout=1.5)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


class AmbientContext(BaseModel):
    """One read of the moment, as the model and any trigger policy see it."""

    local_time: str
    timezone: str
    time_of_day: TimeOfDay
    is_night: bool
    battery_percent: float
    charging: bool
    network_reachable: bool
    motion: MotionReading = Field(default_factory=MotionReading)
    uptime_seconds: float

    def summary(self) -> str:
        """One line, small-screen first: lead with the fact, no preamble."""
        state = "charging" if self.charging else "discharging"
        motion = (
            "moving"
            if self.motion.moving
            else ("still" if self.motion.available else "motion unknown")
        )
        return (
            f"{self.local_time[:16].replace('T', ' ')} {self.timezone} "
            f"({self.time_of_day}), battery {self.battery_percent:.0f}% ({state}), "
            f"network {'reachable' if self.network_reachable else 'unreachable'}, "
            f"{motion}, up {self.uptime_seconds:.0f}s"
        )


class GetContextParams(BaseModel):
    """No parameters."""


class GetContextTool:
    """Ambient context: local time, battery, network, motion, uptime.

    Read-only, cheap, no prompts — a trigger layer polls this to decide *when*
    to speak rather than firing at random. `started_at` anchors uptime to when
    this tool was constructed: a process-uptime proxy, not a system call, since
    there is no hardware-backed uptime source yet.
    """

    spec = ToolSpec(
        name="get_context",
        description=(
            "Report local time and time of day, battery state, network "
            "reachability, whether the device is being moved, and uptime — the "
            "operator's ambient context."
        ),
        params_model=GetContextParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(
        self,
        battery: BatteryDriver,
        *,
        motion: MotionDriver | None = None,
        network_check: NetworkCheck | None = None,
        clock: Callable[[], datetime] | None = None,
        started_at: float | None = None,
    ) -> None:
        self._battery = battery
        # No sensor is the honest default on hardware that does not have one;
        # `MockMotion` is what a test opts into, not what a bare device claims.
        self._motion: MotionDriver = motion or AbsentMotion()
        self._network_check = network_check or _default_network_check
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._started_at = time.monotonic() if started_at is None else started_at

    async def read(self) -> AmbientContext:
        """The context, without the tool wrapper. What a trigger policy calls."""
        status = await self._battery.read()
        moment = self._clock()
        reachable = await self._network_check()
        motion = await self._motion.read()
        bucket = classify_time_of_day(moment)
        return AmbientContext(
            local_time=moment.isoformat(),
            timezone=moment.tzname() or "UTC",
            time_of_day=bucket,
            is_night=bucket is TimeOfDay.NIGHT,
            battery_percent=status.percent,
            charging=status.charging,
            network_reachable=reachable,
            motion=motion,
            uptime_seconds=max(0.0, time.monotonic() - self._started_at),
        )

    async def execute(self, params: GetContextParams, ctx: ToolContext) -> ToolResult:
        context = await self.read()
        return ToolResult.success(
            context.summary(),
            local_time=context.local_time,
            timezone=context.timezone,
            time_of_day=str(context.time_of_day),
            is_night=context.is_night,
            battery_percent=context.battery_percent,
            charging=context.charging,
            network_reachable=context.network_reachable,
            moving=context.motion.moving,
            motion_available=context.motion.available,
            uptime_seconds=context.uptime_seconds,
        )
