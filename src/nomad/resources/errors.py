"""Errors raised while governing workloads."""

from __future__ import annotations

from nomad.core.errors import NomadError


class ResourceError(NomadError):
    """Base for everything this package raises."""


class WorkloadError(ResourceError):
    """A workload could not be registered.

    Raised at *registration*, never at suspension time, and that is the point:
    a workload in neither tier, or one whose name collides with another, is a
    wiring mistake. Finding it at boot is cheap; finding it when the governor
    first tries to park something is a device that has already been laggy for a
    while.
    """
