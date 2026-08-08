"""Nomad's own hardware, exposed to whatever model is running (D19).

Claude Code brings filesystem, shell, search and web tools. It cannot bring a
screen, a battery gauge or a USB HID output — those are what this package
serves. Nothing here imports `claude-agent-sdk`; the SDK wiring lives in
`agent/backends/claude_cli.py` (D24).
"""

from nomad.mcp.context import (
    AbsentMotion,
    AmbientContext,
    GetContextTool,
    MockMotion,
    MotionDriver,
    MotionReading,
    TimeOfDay,
    classify_time_of_day,
)
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
from nomad.mcp.utilities import (
    ConvertUnitsTool,
    NoteTool,
    NotificationsTool,
    SetAlarmTool,
    SetReminderTool,
    SetTimerTool,
    StopwatchTool,
    WorldClockTool,
    build_utility_tools,
)
from nomad.mcp.voice import SpeakParams, SpeakTool, build_voice_tools

__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "AbsentMotion",
    "AmbientContext",
    "BatteryDriver",
    "BatteryStatus",
    "ConvertUnitsTool",
    "DisplayDriver",
    "DisplayTextTool",
    "GetContextTool",
    "HidDriver",
    "HidTypeTextTool",
    "McpToolRouter",
    "MockBattery",
    "MockDisplay",
    "MockHid",
    "MockMotion",
    "MotionDriver",
    "MotionReading",
    "NoteTool",
    "NotificationsTool",
    "ReadBatteryTool",
    "SetAlarmTool",
    "SetReminderTool",
    "SetTimerTool",
    "SpeakParams",
    "SpeakTool",
    "StopwatchTool",
    "TimeOfDay",
    "WorldClockTool",
    "build_hardware_tools",
    "build_utility_tools",
    "build_voice_tools",
    "classify_time_of_day",
    "register_hardware_tools",
]
