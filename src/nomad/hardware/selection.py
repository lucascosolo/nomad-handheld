"""Driver selection by config string, mirroring `agent/backends/__init__.py`
(D9): a plain string picks the implementation, the optional dependency for a
hardware-backed driver is imported lazily inside the branch that needs it,
and every category has a dependency-free default.

**Deliberately does not know about `nomad.mcp.hardware.MockDisplay` /
`MockBattery`.** Those are the recorders the MCP composition root
(`mcp.server.build_hardware_tools`) defaults to, and `hardware` must not
import `mcp` (layering). `"mock"` here resolves to this layer's own
dependency-free defaults — `HeadlessDisplay` and a static battery reading —
which are functionally the same "no hardware attached" answer, just not the
literal recorder class the MCP tool tests assert against. Whichever module
ends up wiring `NomadConfig` into `build_hardware_tools()` should keep using
`MockDisplay`/`MockBattery` directly for that path rather than routing
through here; this module exists for the driver strings that need a
concrete, hardware-facing implementation.
"""

from __future__ import annotations

from nomad.core.config import BatteryConfig, DisplayConfig
from nomad.hardware.errors import HardwareError
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.hardware.pisugar_battery import BatteryReading
from nomad.protocol import Link


class _StaticBattery:
    """Dependency-free default battery reading. Distinct from
    `mcp.hardware.MockBattery` purely to keep this module out of `mcp`."""

    def __init__(self, reading: BatteryReading | None = None) -> None:
        self.reading = reading or BatteryReading(percent=100.0, charging=False, voltage=None)

    async def read(self) -> BatteryReading:
        return self.reading


def create_display_driver(config: DisplayConfig, *, link: Link | None = None) -> object:
    """Build the display named by `[display].driver`.

    `esp32` is imported lazily inside its branch — that is what lets `mock`
    and `headless` run with nothing but the standard library.
    """
    kind = config.driver

    if kind in ("mock", "headless"):
        return HeadlessDisplay()

    if kind == "esp32":
        if link is None:
            raise HardwareError(
                "the esp32 display driver requires a Link to the ESP32-S3",
                {"driver": kind},
            )
        from nomad.hardware.esp32_display import Esp32Display

        return Esp32Display(link, width=config.width, height=config.height)

    raise HardwareError(f"Unknown display driver '{kind}'", {"driver": kind})


def create_battery_driver(config: BatteryConfig) -> object:
    """Build the battery reader named by `[battery].driver`."""
    kind = config.driver

    if kind in ("mock", "headless"):
        return _StaticBattery()

    if kind == "pisugar":
        from nomad.hardware.pisugar_battery import PiSugarBattery

        return PiSugarBattery()

    raise HardwareError(f"Unknown battery driver '{kind}'", {"driver": kind})
