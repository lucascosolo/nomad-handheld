from __future__ import annotations

import pytest

from nomad.core.errors import LifecycleError
from nomad.core.lifecycle import Component, ComponentRegistry, ComponentState


class RecordingComponent:
    def __init__(self, name: str, events: list[str], *, fail_start: bool = False,
                 fail_stop: bool = False) -> None:
        self.name = name
        self._events = events
        self._fail_start = fail_start
        self._fail_stop = fail_stop

    async def start(self) -> None:
        if self._fail_start:
            self._events.append(f"{self.name}:start:fail")
            raise RuntimeError(f"{self.name} refuses to start")
        self._events.append(f"{self.name}:start")

    async def stop(self) -> None:
        if self._fail_stop:
            self._events.append(f"{self.name}:stop:fail")
            raise RuntimeError(f"{self.name} refuses to stop")
        self._events.append(f"{self.name}:stop")


def test_component_protocol_shape() -> None:
    comp = RecordingComponent("x", [])
    assert isinstance(comp, Component)


async def test_start_all_in_order_stop_all_reversed() -> None:
    events: list[str] = []
    registry = ComponentRegistry()
    registry.register(RecordingComponent("a", events))
    registry.register(RecordingComponent("b", events))
    registry.register(RecordingComponent("c", events))

    await registry.start_all()
    assert events == ["a:start", "b:start", "c:start"]

    events.clear()
    await registry.stop_all()
    assert events == ["c:stop", "b:stop", "a:stop"]


async def test_start_failure_rolls_back_already_started() -> None:
    events: list[str] = []
    registry = ComponentRegistry()
    registry.register(RecordingComponent("a", events))
    registry.register(RecordingComponent("b", events, fail_start=True))
    registry.register(RecordingComponent("c", events))

    with pytest.raises(LifecycleError):
        await registry.start_all()

    # c never started (start_all aborted after b failed); a was rolled back.
    assert events == ["a:start", "b:start:fail", "a:stop"]
    assert registry.state_of("b") == ComponentState.FAILED
    assert registry.state_of("a") == ComponentState.STOPPED
    assert registry.state_of("c") == ComponentState.NEW


async def test_stop_failure_does_not_block_other_stops() -> None:
    events: list[str] = []
    registry = ComponentRegistry()
    registry.register(RecordingComponent("a", events))
    registry.register(RecordingComponent("b", events, fail_stop=True))
    registry.register(RecordingComponent("c", events))

    await registry.start_all()
    events.clear()
    await registry.stop_all()

    # all three attempted to stop, in reverse order, despite b failing
    assert events == ["c:stop", "b:stop:fail", "a:stop"]
    assert registry.state_of("b") == ComponentState.FAILED
    assert registry.state_of("a") == ComponentState.STOPPED
    assert registry.state_of("c") == ComponentState.STOPPED


async def test_states_start_at_new() -> None:
    registry = ComponentRegistry()
    registry.register(RecordingComponent("solo", []))
    assert registry.state_of("solo") == ComponentState.NEW
