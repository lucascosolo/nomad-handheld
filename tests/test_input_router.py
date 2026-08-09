"""A press on the panel reaches the logical layer.

This file exists because `InputStream.feed_button`, `feed_joystick` and
`feed_touch` were fully tested and had no caller in `src/` outside their own
package. Every unit on both sides of that join was green while the join itself
did not exist — so the tests here are deliberately end-to-end across the seam:
framed bytes go in at the transport, and an `InputAction` comes out of
`InputStream.events()`.
"""

from __future__ import annotations

import asyncio

import pytest

from nomad.core.config import InputConfig
from nomad.input.events import ActionPhase, InputAction, TouchEvent
from nomad.input.mapper import InputMapper
from nomad.input.router import InputRouter
from nomad.input.stream import InputStream
from nomad.protocol.codec import JsonCodec
from nomad.protocol.framing import Framing
from nomad.protocol.link import Link, LinkKind
from nomad.protocol.messages import (
    DisplayLinkStatus,
    InputButton,
    InputJoystick,
    InputTouch,
    KeyPhase,
    Message,
    TouchPhase,
)
from nomad.protocol.transport import MockTransport

CODEC = JsonCodec()


def frame(message: Message) -> bytes:
    return Framing().encode(CODEC.encode(message))


class Rig:
    """A link over a mock transport, a stream, and the router joining them."""

    def __init__(self) -> None:
        self.transport = MockTransport()
        self.link = Link(self.transport, kind=LinkKind.DISPLAY, name="panel")
        self.stream = InputStream(InputMapper(InputConfig()))
        self.router = InputRouter(self.link, self.stream)
        self._seq = 0

    def send(self, message: Message) -> None:
        """Deliver one message inbound, keeping `seq` monotonic so the link
        does not read a test's fixtures as a panel reboot."""
        message.seq = self._seq
        self._seq += 1
        self.transport.deliver(frame(message))

    async def next_event(self, *, timeout: float = 2.0) -> InputAction | TouchEvent:
        async def first() -> InputAction | TouchEvent:
            async for event in self.stream.events():
                return event
            raise AssertionError("the stream ended without an event")

        return await asyncio.wait_for(first(), timeout)


@pytest.fixture
async def rig():
    rig = Rig()
    await rig.link.start()
    await rig.stream.start()
    await rig.router.start()
    try:
        yield rig
    finally:
        await rig.router.stop()
        await rig.stream.stop()
        await rig.link.stop()


async def test_a_button_press_on_the_panel_becomes_a_logical_action(rig: Rig) -> None:
    """The whole point of D13, exercised across the cable rather than in a unit."""
    rig.send(Message.build(InputButton(button="a", phase=KeyPhase.PRESS)))

    event = await rig.next_event()

    assert isinstance(event, InputAction)
    # `a` maps to CONFIRM in the shipped config. Application code never names
    # the left-hand side of that mapping — this is the seam where it stops.
    assert event.action == "CONFIRM"
    assert event.phase is ActionPhase.PRESS
    assert rig.router.stats.buttons == 1


async def test_a_touch_arrives_untranslated(rig: Rig) -> None:
    """D13: touch is a position, never synthesized navigation."""
    rig.send(Message.build(InputTouch(x=120, y=64, phase=TouchPhase.DOWN)))

    event = await rig.next_event()

    assert isinstance(event, TouchEvent)
    assert (event.x, event.y) == (120, 64)
    assert event.phase is TouchPhase.DOWN
    assert rig.router.stats.touches == 1


async def test_the_joystick_crosses_the_seam(rig: Rig) -> None:
    # Well past the 0.25 deadzone, or the mapper correctly emits nothing.
    rig.send(Message.build(InputJoystick(x=0.0, y=-0.9)))

    event = await rig.next_event()

    assert isinstance(event, InputAction)
    assert event.action == "NAV_UP"
    assert rig.router.stats.joystick == 1


async def test_a_malformed_payload_is_dropped_and_counted(rig: Rig) -> None:
    """Firmware and Pi disagreeing about a shape must not kill the reader.

    The router is the only consumer of the link's inbox, so an exception
    escaping it takes every future press with it — and the symptom is a panel
    that draws perfectly and responds to nothing, which reads to an operator
    as broken hardware rather than as a bug.
    """
    rig.send(Message(type="input.button", payload={"button": "nonesuch"}))
    rig.send(Message.build(InputButton(button="a", phase=KeyPhase.PRESS)))

    event = await rig.next_event()

    assert isinstance(event, InputAction)
    assert rig.router.stats.malformed == 1
    assert rig.router.stats.buttons == 1


async def test_a_message_the_router_has_no_handler_for_is_ignored(rig: Rig) -> None:
    """The link accepts every type in its catalogue; only the input ones are
    the router's business, and the rest must cost one counter, not the task."""
    rig.send(Message.build(DisplayLinkStatus(uptime_ms=1200, free_heap=90_000, last_seq_seen=0)))
    rig.send(Message.build(InputButton(button="a", phase=KeyPhase.PRESS)))

    event = await rig.next_event()

    assert isinstance(event, InputAction)
    assert rig.router.stats.ignored == 1
    assert rig.router.stats.malformed == 0


async def test_stopping_the_router_leaves_no_task_behind(rig: Rig) -> None:
    """It owns a task that lives as long as the device, so shutdown has to
    actually end it — a leaked reader keeps the serial port open across a
    restart."""
    await rig.router.stop()
    assert rig.router._task is None
    # Idempotent: the registry may stop a component it already stopped.
    await rig.router.stop()
