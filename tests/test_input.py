"""Tests for `nomad.input` — physical events to logical actions (D13)."""

from __future__ import annotations

import asyncio

import pytest

from nomad.core.config import (
    InputButtonsConfig,
    InputConfig,
    InputJoystickConfig,
    InputRepeatConfig,
)
from nomad.input.actions import ActionRegistry, UnknownActionError
from nomad.input.events import ActionPhase, InputSource
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.protocol.messages import (
    ButtonId,
    InputButton,
    InputJoystick,
    InputTouch,
    KeyPhase,
    TouchPhase,
)


class _FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _mapper_with_clock() -> tuple[InputMapper, _FakeClock]:
    clock = _FakeClock()
    mapper = InputMapper(InputConfig(), clock=clock)
    return mapper, clock


def test_button_press_produces_the_mapped_action() -> None:
    mapper, _ = _mapper_with_clock()
    events = mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=1.0)
    assert len(events) == 1
    assert events[0].action == "CONFIRM"
    assert events[0].phase == ActionPhase.PRESS
    assert events[0].source == InputSource.BUTTON
    assert events[0].ts == 1.0


def test_button_release_produces_matching_release_action() -> None:
    mapper, _ = _mapper_with_clock()
    mapper.on_button(InputButton(button=ButtonId.B, phase=KeyPhase.PRESS), now=1.0)
    events = mapper.on_button(InputButton(button=ButtonId.B, phase=KeyPhase.RELEASE), now=1.5)
    assert len(events) == 1
    assert events[0].action == "BACK"
    assert events[0].phase == ActionPhase.RELEASE


def test_remapping_a_button_through_config_changes_its_action() -> None:
    config = InputConfig(buttons=InputButtonsConfig(a="ACTION_2"))
    mapper = InputMapper(config)
    events = mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.0)
    assert events[0].action == "ACTION_2"


def test_mapping_a_button_to_an_unregistered_action_raises() -> None:
    config = InputConfig(buttons=InputButtonsConfig(a="NOT_REGISTERED"))
    mapper = InputMapper(config)
    with pytest.raises(UnknownActionError):
        mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.0)


def test_registering_and_mapping_to_a_custom_action() -> None:
    config = InputConfig(extra_actions=["ASSISTANT"], buttons=InputButtonsConfig(y="ASSISTANT"))
    mapper = InputMapper(config)
    assert mapper.registry.is_registered("ASSISTANT")
    events = mapper.on_button(InputButton(button=ButtonId.Y, phase=KeyPhase.PRESS), now=0.0)
    assert events[0].action == "ASSISTANT"


def test_registry_register_makes_an_unknown_action_usable() -> None:
    registry = ActionRegistry()
    assert not registry.is_registered("ASSISTANT")
    registry.register("ASSISTANT")
    assert registry.is_registered("ASSISTANT")
    assert registry.require("ASSISTANT") == "ASSISTANT"


def test_stick_inside_deadzone_produces_nothing() -> None:
    mapper, _ = _mapper_with_clock()
    events = mapper.on_joystick(InputJoystick(x=0.05, y=0.05), now=0.0)
    assert events == []


def test_stick_past_threshold_produces_correct_nav_direction() -> None:
    mapper, _ = _mapper_with_clock()
    events = mapper.on_joystick(InputJoystick(x=0.0, y=0.9), now=0.0)
    assert len(events) == 1
    assert events[0].action == "NAV_DOWN"
    assert events[0].phase == ActionPhase.PRESS
    assert events[0].source == InputSource.JOYSTICK


def test_stick_left_and_right_and_up_map_to_expected_nav_actions() -> None:
    mapper, _ = _mapper_with_clock()
    assert mapper.on_joystick(InputJoystick(x=-0.9, y=0.0), now=0.0)[0].action == "NAV_LEFT"
    mapper.on_joystick(InputJoystick(x=0.0, y=0.0), now=0.1)  # back to centre
    assert mapper.on_joystick(InputJoystick(x=0.9, y=0.0), now=0.2)[0].action == "NAV_RIGHT"
    mapper.on_joystick(InputJoystick(x=0.0, y=0.0), now=0.3)
    assert mapper.on_joystick(InputJoystick(x=0.0, y=-0.9), now=0.4)[0].action == "NAV_UP"


def test_hysteresis_prevents_chatter_at_the_boundary() -> None:
    config = InputConfig(joystick=InputJoystickConfig(deadzone=0.5))
    mapper = InputMapper(config)
    # Cross the enter threshold to establish NAV_DOWN.
    first = mapper.on_joystick(InputJoystick(x=0.0, y=0.6), now=0.0)
    assert first[0].action == "NAV_DOWN"
    # Drop below the enter threshold but stay above the (lower) exit
    # threshold: must NOT release/chatter.
    chatter = mapper.on_joystick(InputJoystick(x=0.0, y=0.4), now=0.1)
    assert chatter == []
    # Drop below the exit threshold: now it releases.
    released = mapper.on_joystick(InputJoystick(x=0.0, y=0.1), now=0.2)
    assert len(released) == 1
    assert released[0].phase == ActionPhase.RELEASE
    assert released[0].action == "NAV_DOWN"


def test_edge_triggered_consumption_yields_exactly_one_action_per_press() -> None:
    mapper, _ = _mapper_with_clock()
    events = mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.0)
    assert len(events) == 1
    # A repeated press notification while still held (e.g. a duplicate wire
    # message) must not produce a second edge.
    events = mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.05)
    assert events == []
    # No REPEAT is produced unless tick() is called — a menu consumer that
    # never calls tick() sees exactly one action for the whole press.
    assert mapper.tick(now=0.01) == []


def test_buttons_and_the_stick_share_one_repeat_configuration() -> None:
    """Repeat timing used to live under `[input.joystick]` and be read for
    buttons too — merely odd until someone wants the two to differ."""
    config = InputConfig(repeat=InputRepeatConfig(delay_ms=200, interval_ms=50))
    mapper = InputMapper(config)

    mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.0)
    assert mapper.tick(now=0.1) == []
    assert len(mapper.tick(now=0.2)) == 1

    mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.RELEASE), now=0.3)
    mapper.on_joystick(InputJoystick(x=0.0, y=-1.0), now=1.0)
    assert mapper.tick(now=1.1) == []
    assert len(mapper.tick(now=1.2)) == 1


def test_auto_repeat_produces_initial_delay_then_faster_interval() -> None:
    config = InputConfig(repeat=InputRepeatConfig(delay_ms=400, interval_ms=100))
    mapper = InputMapper(config)
    mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS), now=0.0)

    # Before the delay elapses: nothing.
    assert mapper.tick(now=0.2) == []
    # At/after the initial delay: first repeat.
    first = mapper.tick(now=0.4)
    assert len(first) == 1
    assert first[0].phase == ActionPhase.REPEAT
    # Before the (shorter) interval elapses again: nothing.
    assert mapper.tick(now=0.45) == []
    # After the interval: another repeat.
    second = mapper.tick(now=0.5)
    assert len(second) == 1
    assert second[0].phase == ActionPhase.REPEAT
    # Release stops repeats.
    mapper.on_button(InputButton(button=ButtonId.A, phase=KeyPhase.RELEASE), now=0.5)
    assert mapper.tick(now=1.0) == []


def test_touch_passes_through_as_a_positional_event_without_navigation() -> None:
    mapper, _ = _mapper_with_clock()
    event = mapper.on_touch(InputTouch(x=10, y=20, phase=TouchPhase.DOWN), now=0.0)
    assert event.x == 10
    assert event.y == 20
    assert event.phase == TouchPhase.DOWN
    assert not hasattr(event, "action")


def test_input_stream_delivers_button_press_through_events_generator() -> None:
    async def run() -> None:
        mapper, _ = _mapper_with_clock()
        stream = InputStream(mapper, tick_interval_s=1000.0)
        await stream.start()
        try:
            await stream.feed_button(InputButton(button=ButtonId.A, phase=KeyPhase.PRESS))
            gen = stream.events()
            event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            assert event.action == "CONFIRM"
            assert event.phase == ActionPhase.PRESS
        finally:
            await stream.stop()

    asyncio.run(run())
