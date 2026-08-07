"""Process lifecycle: the `Component` protocol and `ComponentRegistry`.

Components start in registration order and stop in reverse order. A start
failure aborts the sequence and rolls back by stopping whatever already
started. Stop failures are logged and tolerated — one bad component must
never block shutdown of the rest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from nomad.core.errors import LifecycleError
from nomad.core.logging import get_logger

logger = get_logger(__name__)


class ComponentState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@runtime_checkable
class Component(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ComponentRegistry:
    """Starts components in order, stops them in reverse, tolerating stop failures."""

    def __init__(self) -> None:
        self._components: list[Component] = []
        self._states: dict[str, ComponentState] = {}
        self._started: list[Component] = []

    def register(self, component: Component) -> None:
        self._components.append(component)
        self._states[component.name] = ComponentState.NEW

    def state_of(self, name: str) -> ComponentState:
        return self._states.get(name, ComponentState.NEW)

    def states(self) -> dict[str, ComponentState]:
        return dict(self._states)

    async def start_all(self) -> None:
        """Start every registered component in order.

        On failure, stop everything already started (in reverse order) and
        raise `LifecycleError` naming the component that failed.
        """
        for component in self._components:
            self._states[component.name] = ComponentState.STARTING
            try:
                await component.start()
            except Exception as exc:  # noqa: BLE001 - must roll back regardless of cause
                self._states[component.name] = ComponentState.FAILED
                logger.error(
                    "Component failed to start; rolling back",
                    extra={"component": component.name, "error": str(exc)},
                )
                await self._rollback()
                raise LifecycleError(
                    f"Component '{component.name}' failed to start: {exc}",
                    {"component": component.name},
                ) from exc
            self._states[component.name] = ComponentState.STARTED
            self._started.append(component)

    async def _rollback(self) -> None:
        for component in reversed(self._started):
            await self._stop_one(component)
        self._started.clear()

    async def stop_all(self) -> None:
        """Stop every started component in reverse order.

        Failures are logged and swallowed so one bad component cannot block
        the rest of shutdown.
        """
        for component in reversed(self._started):
            await self._stop_one(component)
        self._started.clear()

    async def _stop_one(self, component: Component) -> None:
        self._states[component.name] = ComponentState.STOPPING
        try:
            await component.stop()
            self._states[component.name] = ComponentState.STOPPED
        except Exception as exc:  # noqa: BLE001 - one bad component must not block shutdown
            self._states[component.name] = ComponentState.FAILED
            logger.error(
                "Component failed to stop; continuing shutdown",
                extra={"component": component.name, "error": str(exc)},
            )
