"""Nomad's own hardware, exposed to whatever model is running (D19).

Claude Code brings filesystem, shell, search and web tools. It cannot bring a
screen, a battery gauge or a USB HID output — those are what this package
serves. Nothing here imports `claude-agent-sdk`; the SDK wiring lives in
`agent/backends/claude_cli.py` (D24).
"""

from nomad.mcp.hardware import (
    BatteryDriver,
    BatteryStatus,
    DisplayDriver,
    DisplayTextTool,
    HidDriver,
    HidTypeTextTool,
    MockBattery,
    MockDisplay,
    MockHid,
    ReadBatteryTool,
)
from nomad.mcp.server import (
    SERVER_NAME,
    SERVER_VERSION,
    McpToolRouter,
    build_hardware_tools,
    register_hardware_tools,
)

__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "BatteryDriver",
    "BatteryStatus",
    "DisplayDriver",
    "DisplayTextTool",
    "HidDriver",
    "HidTypeTextTool",
    "McpToolRouter",
    "MockBattery",
    "MockDisplay",
    "MockHid",
    "ReadBatteryTool",
    "build_hardware_tools",
    "register_hardware_tools",
]
