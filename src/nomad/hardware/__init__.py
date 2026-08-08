"""Concrete drivers behind the facades declared in `nomad.mcp.hardware` (D9).

Every category has a mock (or a dependency-free equivalent) and mock is
always the default: the whole system runs and tests on a laptop with no
hardware, no serial port, and no credentials attached.

This package imports `nomad.protocol` and `nomad.core` and nothing above
them — not `tools`, `targets`, `agent`, `api`, or `mcp`. `mcp.hardware`
declares the `DisplayDriver`/`BatteryDriver` protocols these implement
*structurally*; nothing here imports that module (D2's separation of
concerns applied to layering, not just to bytes).
"""

from __future__ import annotations

from nomad.hardware.errors import HardwareError
from nomad.hardware.esp32_display import Esp32Display
from nomad.hardware.headless_display import HeadlessDisplay, Screen
from nomad.hardware.pisugar_battery import BatteryReading, PiSugarBattery
from nomad.hardware.selection import create_battery_driver, create_display_driver

__all__ = [
    "BatteryReading",
    "Esp32Display",
    "HardwareError",
    "HeadlessDisplay",
    "PiSugarBattery",
    "Screen",
    "create_battery_driver",
    "create_display_driver",
]
