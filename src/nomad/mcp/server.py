"""The hardware toolset and the call router behind Nomad's MCP server.

Deliberately free of any SDK import. `create_sdk_mcp_server` and the `@tool`
decorator live in `agent/backends/claude_cli.py`, which is the only module
allowed to touch `claude-agent-sdk` (D24); this module hands it plain data —
tool specs and an async callable — and it does the wiring.

That split is not ceremony. It means the hardware surface, including the part
that refuses to execute without a grant, is exercised by the test suite on a
laptop with no SDK installed.

**Why the router re-checks authorization.** By the time a tool call reaches
here it has already passed `can_use_tool`. Trusting that would make the
approval and the execution two separate facts connected only by convention.
Instead the bridge stashes the minted grant and the router must pop it; a call
that arrives with nothing in the vault is executed by nobody. `ToolExecutor`
still takes a grant, never a request, across the process boundary (D4).
"""

from __future__ import annotations

from typing import Any

from nomad.core.logging import get_logger
from nomad.mcp.context import GetContextTool, MotionDriver
from nomad.mcp.hardware import (
    BatteryDriver,
    ChoicePrompter,
    DisplayCardTool,
    DisplayChoiceTool,
    DisplayDriver,
    DisplayListTool,
    DisplayTextTool,
    HidDriver,
    HidTypeTextTool,
    MockBattery,
    MockDisplay,
    MockHid,
    ReadBatteryTool,
)
from nomad.mcp.memory import build_memory_tools
from nomad.mcp.utilities import build_utility_tools
from nomad.memory.store import MemoryStore
from nomad.notifications.queue import NotificationQueue
from nomad.tools.base import Tool, ToolSpec
from nomad.tools.builtin.system import GetSystemInfoTool
from nomad.tools.permissions import GrantVault, ToolExecutor
from nomad.tools.registry import ToolRegistry
from nomad.utilities.notes import NoteStore
from nomad.utilities.stopwatch import StopwatchStore

logger = get_logger(__name__)

#: The MCP server name. Claude Code addresses these tools as
#: `mcp__nomad__<tool>`, which `PermissionBridge.nomad_tool_name` reverses.
SERVER_NAME = "nomad"
SERVER_VERSION = "0.1.0"


def build_hardware_tools(
    *,
    display: DisplayDriver | None = None,
    battery: BatteryDriver | None = None,
    hid: HidDriver | None = None,
    prompter: ChoicePrompter | None = None,
    motion: MotionDriver | None = None,
    store: MemoryStore | None = None,
    notifications: NotificationQueue | None = None,
    notes: NoteStore | None = None,
    stopwatches: StopwatchStore | None = None,
) -> list[Tool]:
    """The tools Claude Code cannot bring for itself.

    Mocks by default, and the default is what the suite runs against (D9).

    `store` is optional so every existing caller keeps working: with no store
    there are no memory tools, rather than three tools that fail at call time.
    A device wired without a database should look like a device with no
    memory, not like one whose memory is broken. The same rule governs the
    utility tools: the pure ones (conversion, world clock) need nothing and are
    always present, and the durable ones appear only once there is somewhere
    durable to put them.
    """
    display = display or MockDisplay()
    battery = battery or MockBattery()
    tools: list[Tool] = [
        GetSystemInfoTool(),
        DisplayTextTool(display),
        DisplayCardTool(display),
        DisplayListTool(display),
        DisplayChoiceTool(display, prompter),
        ReadBatteryTool(battery),
        GetContextTool(battery, motion=motion),
        HidTypeTextTool(hid or MockHid()),
    ]
    if store is not None:
        tools.extend(build_memory_tools(store))
    tools.extend(
        build_utility_tools(
            notifications=notifications, notes=notes, stopwatches=stopwatches
        )
    )
    return tools


def register_hardware_tools(registry: ToolRegistry, tools: list[Tool]) -> ToolRegistry:
    """Register the hardware tools, tolerating ones already present."""
    for tool in tools:
        if not registry.has(tool.spec.name):
            registry.register(tool)
    return registry


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """MCP's own content shape. Plain data, not an SDK object."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpToolRouter:
    """Executes an MCP tool call against the grant the bridge already minted."""

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        vault: GrantVault,
        specs: list[ToolSpec],
    ) -> None:
        self._executor = executor
        self._vault = vault
        self._specs = {spec.name: spec for spec in specs}

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tool. Returns an error result rather than raising, always.

        An exception escaping into the MCP server would surface to the model as
        an opaque transport failure; an error *result* tells it what happened
        and lets it choose differently.
        """
        args = dict(args or {})
        if name not in self._specs:
            return _text_result(f"unknown hardware tool '{name}'", is_error=True)

        claimed = self._vault.pop(name, args)
        if claimed is None:
            # No grant on record for exactly these arguments. Either the call
            # never passed the bridge, or its arguments changed afterwards.
            # Both are the same answer.
            logger.warning("MCP call with no authorization on record", extra={"tool": name})
            return _text_result(
                f"'{name}' has no authorization on record for these arguments; refusing",
                is_error=True,
            )

        grant, request = claimed
        try:
            result = await self._executor.run(grant, request)
        except Exception as exc:  # noqa: BLE001 - never leak a raise into MCP
            logger.error(
                "Hardware tool failed",
                extra={"tool": name, "error": f"{type(exc).__name__}: {exc}"},
            )
            return _text_result(f"{type(exc).__name__}: {exc}", is_error=True)

        if not result.ok:
            return _text_result(result.error or "tool failed", is_error=True)
        return _text_result(result.content)
