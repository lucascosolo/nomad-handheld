"""Who gets the Pi's cores, and who gives them back (D38).

Depends on `core` alone, deliberately: the governor learns that a turn is live
by watching the bus for two event *names*, never by importing the session it is
arbitrating for. A resource policy that must keep working while the session is
wedged should not hold a reference to it.
"""

from __future__ import annotations

from nomad.resources.clock import Clock, ManualClock, SystemClock
from nomad.resources.errors import ResourceError, WorkloadError
from nomad.resources.governor import (
    EVENT_WORKLOAD_RESUMED,
    EVENT_WORKLOAD_SUSPENDED,
    EVENT_WORKLOAD_TERMINATED,
    TURN_FINISHED_EVENT,
    TURN_STARTED_EVENT,
    ResourceGovernor,
    TurnState,
    WorkloadStatus,
)
from nomad.resources.workload import (
    InteractiveWorkload,
    OpportunisticWorkload,
    Tier,
    Workload,
    WorkloadState,
    YieldContext,
)

__all__ = [
    "EVENT_WORKLOAD_RESUMED",
    "EVENT_WORKLOAD_SUSPENDED",
    "EVENT_WORKLOAD_TERMINATED",
    "TURN_FINISHED_EVENT",
    "TURN_STARTED_EVENT",
    "Clock",
    "InteractiveWorkload",
    "ManualClock",
    "OpportunisticWorkload",
    "ResourceError",
    "ResourceGovernor",
    "SystemClock",
    "Tier",
    "TurnState",
    "Workload",
    "WorkloadError",
    "WorkloadState",
    "WorkloadStatus",
    "YieldContext",
]
