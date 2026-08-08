"""Concrete drivers behind the facades in `mcp/hardware.py` (D2, D9, D18).

Nothing is wired up on this device yet — no ESP32, no serial port, no PiSugar.
That is the situation every test here runs in, and it is the point: if the
drivers cannot be exercised with no hardware attached, the rest of the stack
cannot be built before the soldering iron comes out.
"""

from __future__ import annotations

import pytest

from nomad.core.config import BatteryConfig, DisplayConfig
from nomad.hardware import (
    Esp32Display,
    HeadlessDisplay,
    create_battery_driver,
    create_display_driver,
)
from nomad.hardware.errors import HardwareError
from nomad.mcp.hardware import DisplayDriver
from nomad.protocol import JsonCodec, Link, LinkKind, MessageType, MockTransport, ScreenKind


async def _esp32(width: int = 320, height: int = 240) -> tuple[Esp32Display, MockTransport, Link]:
    transport = MockTransport()
    link = Link(transport, kind=LinkKind.DISPLAY, name="esp32", codec=JsonCodec())
    await link.start()
    return Esp32Display(link, width=width, height=height), transport, link


# -- the headless display ---------------------------------------------------


async def test_the_headless_display_renders_every_screen_kind() -> None:
    """It is what makes app authoring demoable before the panel exists."""
    display = HeadlessDisplay()
    await display.show_text("hello", title="Nomad")
    assert "hello" in display.screen.text and "Nomad" in display.screen.text

    await display.show_card("Battery", "Healthy", [("Charge", "41%")])
    assert "Charge: 41%" in display.screen.text

    await display.show_list("Apps", [("Notes", None), ("Chess", "vs bot")], selectable=True)
    assert "Notes" in display.screen.text and "(vs bot)" in display.screen.text

    await display.show_choice("Continue?", ["Yes", "No"])
    assert "[1] Yes" in display.screen.text and "[2] No" in display.screen.text


async def test_the_headless_display_keeps_history() -> None:
    display = HeadlessDisplay()
    await display.show_text("first")
    await display.show_text("second")
    assert len(display.history) == 2
    assert display.screen.text.endswith("second")


async def test_the_headless_display_escapes_html() -> None:
    """The HTML view is for a human to look at, not a place to inject markup."""
    display = HeadlessDisplay()
    await display.show_text("<script>alert(1)</script>")
    assert "<script>" not in display.screen.html
    assert "&lt;script&gt;" in display.screen.html


def test_every_display_driver_satisfies_the_widened_protocol() -> None:
    """A mock that takes a different branch than production is worse than none."""
    from nomad.mcp.hardware import MockDisplay

    for driver in (MockDisplay(), HeadlessDisplay()):
        assert isinstance(driver, DisplayDriver), f"{type(driver).__name__} is not a DisplayDriver"


# -- the ESP32 driver -------------------------------------------------------


async def test_the_esp32_driver_sends_structure_not_pixels() -> None:
    """A card is a few dozen bytes as `display.state` and ~8KB as a region.

    The first draft of this driver packed JSON into `display.draw`'s `pixels`
    field, which is documented as raw image data. Structure on the wire also
    puts layout on the side that owns the panel and its fonts.
    """
    display, transport, link = await _esp32()
    await display.show_card("Battery", "Healthy", [("Charge", "41%")])
    await link.stop()

    sent = transport.sent
    assert len(sent) == 1
    message = _decode(sent[0])
    assert message.type == MessageType.DISPLAY_STATE
    assert message.payload["kind"] == ScreenKind.CARD
    assert message.payload["rows"] == [["Charge", "41%"]]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda d: d.show_text("hi"), ScreenKind.TEXT),
        (lambda d: d.show_card("t", "b", []), ScreenKind.CARD),
        (lambda d: d.show_list("t", []), ScreenKind.LIST),
        (lambda d: d.show_choice("q?", ["a", "b"]), ScreenKind.CHOICE),
    ],
)
async def test_every_screen_kind_reaches_the_wire(call, expected: ScreenKind) -> None:
    display, transport, link = await _esp32()
    await call(display)
    await link.stop()
    assert _decode(transport.sent[0]).payload["kind"] == expected


async def test_the_backlight_uses_its_own_message() -> None:
    display, transport, link = await _esp32()
    await display.set_backlight(128)
    await link.stop()
    message = _decode(transport.sent[0])
    assert message.type == MessageType.DISPLAY_BACKLIGHT
    assert message.payload["level"] == 128


async def test_the_esp32_driver_needs_no_serial_port() -> None:
    """The whole point of D2: a driver composes a `Link`, never a port."""
    display, _, link = await _esp32()
    await display.show_text("no hardware here")
    await link.stop()


# -- driver selection (D9) --------------------------------------------------


def test_display_selection_defaults_to_no_hardware() -> None:
    assert isinstance(create_display_driver(DisplayConfig()), HeadlessDisplay)


def test_selecting_the_esp32_display_without_a_link_is_a_clear_error() -> None:
    """Not an AttributeError three calls later."""
    config = DisplayConfig.model_validate({"driver": "esp32"})
    with pytest.raises(HardwareError, match="requires a Link"):
        create_display_driver(config)


def test_an_unknown_display_driver_is_a_clear_error() -> None:
    config = DisplayConfig.model_validate({"driver": "holograph"})
    with pytest.raises(HardwareError, match="Unknown display driver"):
        create_display_driver(config)


def test_battery_selection_defaults_to_no_hardware() -> None:
    driver = create_battery_driver(BatteryConfig())
    assert hasattr(driver, "read")


def test_an_unknown_battery_driver_is_a_clear_error() -> None:
    config = BatteryConfig.model_validate({"driver": "nuclear"})
    with pytest.raises(HardwareError, match="Unknown battery driver"):
        create_battery_driver(config)


def test_the_pisugar_driver_imports_without_hardware_present() -> None:
    """An optional dependency must fail at construction, never at import.

    A module-scope import of a hardware library makes `pytest` fail on a
    laptop, which is the one place this suite has to run (D9).
    """
    from nomad.hardware import pisugar_battery

    assert hasattr(pisugar_battery, "PiSugarBattery")


def _decode(frame: bytes):
    """Unwrap one framed, codec-encoded message from what a transport sent."""
    from nomad.protocol import Framing

    framing = Framing()
    result = framing.feed(frame)
    assert result.frames, "transport sent bytes that did not contain a whole frame"
    return JsonCodec().decode(result.frames[0])
