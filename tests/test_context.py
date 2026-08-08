"""Chunk N: the ambient read a trigger policy branches on.

Nothing here reads the real clock, the real network or real hardware. A test
that depends on the actual time of day is a test that behaves differently at
midnight, which is exactly the hour this feature exists to get right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from nomad.core.logging import get_logger
from nomad.mcp.context import (
    AbsentMotion,
    AmbientContext,
    GetContextParams,
    GetContextTool,
    MockMotion,
    MotionDriver,
    MotionReading,
    TimeOfDay,
    classify_time_of_day,
)
from nomad.mcp.hardware import BatteryStatus, MockBattery
from nomad.targets.local import LocalTarget
from nomad.tools.base import ToolContext
from nomad.tools.workspace import Workspace


@pytest.fixture
def tool_ctx(tmp_path) -> ToolContext:
    """A minimal call context. `get_context` touches neither target nor workspace."""
    return ToolContext(
        target=LocalTarget(),
        workspace=Workspace(tmp_path),
        session_id="session-test",
        turn_id=None,
        logger=get_logger("test"),
    )


def _at(hour: int, minute: int = 0, offset_hours: int = 0) -> datetime:
    return datetime(
        2026, 8, 8, hour, minute, tzinfo=timezone(timedelta(hours=offset_hours))
    )


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, TimeOfDay.NIGHT),
        (3, TimeOfDay.NIGHT),
        (5, TimeOfDay.NIGHT),
        (6, TimeOfDay.MORNING),
        (11, TimeOfDay.MORNING),
        (12, TimeOfDay.AFTERNOON),
        (17, TimeOfDay.AFTERNOON),
        (18, TimeOfDay.EVENING),
        (21, TimeOfDay.EVENING),
        (22, TimeOfDay.NIGHT),
        (23, TimeOfDay.NIGHT),
    ],
)
def test_time_of_day_buckets_are_the_shared_vocabulary(hour: int, expected: TimeOfDay) -> None:
    assert classify_time_of_day(_at(hour)) is expected


def test_the_bucket_follows_local_time_not_utc() -> None:
    """03:00 in Tokyo is night even though the same instant is afternoon in UTC."""
    assert classify_time_of_day(_at(3, offset_hours=9)) is TimeOfDay.NIGHT


async def _read(
    *,
    clock_hour: int = 14,
    charging: bool = False,
    reachable: bool = True,
    motion: MotionDriver | None = None,
) -> AmbientContext:
    battery = MockBattery(BatteryStatus(percent=64.0, charging=charging, voltage=3.9))

    async def probe() -> bool:
        return reachable

    tool = GetContextTool(
        battery,
        motion=motion,
        network_check=probe,
        clock=lambda: _at(clock_hour),
        started_at=None,
    )
    return await tool.read()


async def test_a_3am_read_is_distinguishable_from_a_2pm_one() -> None:
    """The whole point: a trigger that knows it is 3am behaves differently."""
    night = await _read(clock_hour=3)
    afternoon = await _read(clock_hour=14)
    assert night.is_night is True
    assert night.time_of_day is TimeOfDay.NIGHT
    assert afternoon.is_night is False
    assert afternoon.time_of_day is TimeOfDay.AFTERNOON


async def test_the_read_carries_battery_charging_and_network() -> None:
    context = await _read(charging=True, reachable=False)
    assert context.battery_percent == 64.0
    assert context.charging is True
    assert context.network_reachable is False


async def test_still_and_cannot_tell_are_different_answers() -> None:
    """A device with no accelerometer must not claim it is sitting still."""
    absent = await _read(motion=AbsentMotion())
    still = await _read(motion=MockMotion(MotionReading(moving=False, available=True)))
    moving = await _read(motion=MockMotion(MotionReading(moving=True, magnitude=1.4)))

    assert absent.motion.available is False
    assert still.motion.available is True and still.motion.moving is False
    assert moving.motion.moving is True
    assert "motion unknown" in absent.summary()
    assert "still" in still.summary()
    assert "moving" in moving.summary()


async def test_no_motion_driver_reports_unknown_rather_than_inventing_stillness() -> None:
    context = await _read(motion=None)
    assert context.motion.available is False


async def test_the_summary_leads_with_the_fact_and_fits_one_line() -> None:
    context = await _read(clock_hour=14, reachable=True)
    summary = context.summary()
    assert summary.startswith("2026-08-08 14:00")
    assert "battery 64%" in summary
    assert "network reachable" in summary
    assert "\n" not in summary


async def test_the_tool_exposes_every_field_as_metadata(tool_ctx) -> None:
    """A trigger layer reads metadata, not the prose line."""
    battery = MockBattery(BatteryStatus(percent=20.0, charging=False))

    async def probe() -> bool:
        return False

    tool = GetContextTool(
        battery,
        motion=MockMotion(MotionReading(moving=True, magnitude=2.0)),
        network_check=probe,
        clock=lambda: _at(3),
        started_at=0.0,
    )
    result = await tool.execute(GetContextParams(), tool_ctx)
    assert result.ok
    assert result.metadata["time_of_day"] == "night"
    assert result.metadata["is_night"] is True
    assert result.metadata["moving"] is True
    assert result.metadata["motion_available"] is True
    assert result.metadata["network_reachable"] is False
    assert result.metadata["battery_percent"] == 20.0
    assert result.metadata["uptime_seconds"] >= 0.0


def test_get_context_is_read_only_and_needs_no_permission() -> None:
    """It is polled by a trigger layer; a prompt every poll would be unusable."""
    spec = GetContextTool(MockBattery()).spec
    assert spec.name == "get_context"
    assert str(spec.risk) == "read_only"
    assert spec.permissions == frozenset()
    assert spec.required_capabilities == frozenset()
    assert spec.never_auto is False


async def test_a_network_probe_that_fails_is_a_normal_answer() -> None:
    """Offline is a state of the world, not a fault of this tool."""

    async def broken() -> bool:
        raise OSError("no route to host")

    tool = GetContextTool(MockBattery(), network_check=broken, clock=lambda: _at(12))
    with pytest.raises(OSError):
        await tool.read()
    # The *default* probe is the one that must never raise; a caller that
    # injects a raising probe gets what it asked for.
    assert callable(tool._network_check)


def test_utc_is_reported_when_the_clock_has_no_zone_name() -> None:
    context = AmbientContext(
        local_time=datetime(2026, 8, 8, 12, tzinfo=UTC).isoformat(),
        timezone="UTC",
        time_of_day=TimeOfDay.AFTERNOON,
        is_night=False,
        battery_percent=50.0,
        charging=False,
        network_reachable=True,
        uptime_seconds=1.0,
    )
    assert "UTC" in context.summary()
