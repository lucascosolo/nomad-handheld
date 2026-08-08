"""Nomad's hardware, as tools the model can call (D19).

Claude Code arrives with filesystem, shell, search and web tools that are
better than anything Nomad would write. What it has no equivalent for is *this
device*: a screen, a battery, a joystick, a USB HID output. That gap is what
`mcp/` fills, and it is the entire reason Nomad runs an MCP server at all.

These are ordinary Nomad `Tool`s. They carry a `ToolSpec`, they are gated by
the same broker, and they are executed by `ToolExecutor.run(grant, request)`
like everything else — being reachable over MCP changes how a tool is
*addressed*, never how it is *authorized*.

**Driver facades, not drivers.** The concrete drivers land in `hardware/` with
chunk E. Until then these tools talk to the narrow protocols below, which is
also what lets the whole surface be tested with no hardware attached (D9).
Chunk E implements these protocols; it should not need to touch this file.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from nomad.targets.base import Capability
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec


class BatteryStatus(BaseModel):
    """What the power system reports (D18)."""

    percent: float
    charging: bool = False
    voltage: float | None = None


@runtime_checkable
class DisplayDriver(Protocol):
    """The screen, as much of it as a tool needs to know about."""

    async def show_text(self, text: str, *, title: str | None = None) -> None: ...


@runtime_checkable
class BatteryDriver(Protocol):
    async def read(self) -> BatteryStatus: ...


@runtime_checkable
class HidDriver(Protocol):
    """The RP2040. Treated as an output weapon, never a convenience (D12)."""

    async def type_text(self, text: str) -> None: ...


class MockDisplay:
    """Records what would have been drawn. The default (D9)."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str | None]] = []

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        self.shown.append((text, title))


class MockBattery:
    def __init__(self, status: BatteryStatus | None = None) -> None:
        self.status = status or BatteryStatus(percent=87.0, charging=False, voltage=4.02)

    async def read(self) -> BatteryStatus:
        return self.status


class MockHid:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def type_text(self, text: str) -> None:
        self.typed.append(text)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class DisplayTextParams(BaseModel):
    text: str = Field(description="The text to show on Nomad's screen.")
    title: str | None = Field(default=None, description="Optional heading.")


class DisplayTextTool:
    """Draw text on the device's own screen."""

    spec = ToolSpec(
        name="display_text",
        description="Show a short message on Nomad's built-in screen.",
        params_model=DisplayTextParams,
        # Mutating rather than read-only: it changes what the user sees, and a
        # screen the model can silently overwrite is a screen that can lie
        # about what the device is doing.
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, display: DisplayDriver) -> None:
        self._display = display

    async def execute(self, params: DisplayTextParams, ctx: ToolContext) -> ToolResult:
        await self._display.show_text(params.text, title=params.title)
        return ToolResult.success(f"displayed {len(params.text)} characters")


class BatteryParams(BaseModel):
    """No parameters."""


class ReadBatteryTool:
    """Report charge state, so the agent can defer work before power runs out."""

    spec = ToolSpec(
        name="read_battery",
        description="Report Nomad's battery percentage and charging state.",
        params_model=BatteryParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, battery: BatteryDriver) -> None:
        self._battery = battery

    async def execute(self, params: BatteryParams, ctx: ToolContext) -> ToolResult:
        status = await self._battery.read()
        state = "charging" if status.charging else "discharging"
        return ToolResult.success(
            f"battery: {status.percent:.0f}% ({state})",
            percent=status.percent,
            charging=status.charging,
            voltage=status.voltage,
        )


class HidTypeParams(BaseModel):
    text: str = Field(description="Text to type into the attached computer.")


class HidTypeTextTool:
    """Type into whatever Nomad is plugged into.

    The most dangerous thing this device can do, and the reason `never_auto`
    exists (D14, D21). It is `EXTERNAL_DEVICE` risk, it declares
    `HID_OUTPUT`, and it requires a target with the `HID_OUTPUT` capability —
    three independent reasons it can never be auto-approved, in any mode.
    A mode switch must not be able to turn a pocket computer into a keystroke
    injector.
    """

    spec = ToolSpec(
        name="hid_type_text",
        description=(
            "Type text as a USB keyboard into the computer Nomad is plugged into. "
            "Always requires explicit human approval."
        ),
        params_model=HidTypeParams,
        risk=Risk.EXTERNAL_DEVICE,
        permissions=frozenset({Permission.HID_OUTPUT}),
        required_capabilities=frozenset({Capability.HID_OUTPUT}),
        never_auto=True,
    )

    def __init__(self, hid: HidDriver) -> None:
        self._hid = hid

    async def execute(self, params: HidTypeParams, ctx: ToolContext) -> ToolResult:
        await self._hid.type_text(params.text)
        return ToolResult.success(f"typed {len(params.text)} characters")
