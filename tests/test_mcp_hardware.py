"""Nomad's hardware, exposed to the model over MCP (D19).

The property under test throughout: **being reachable over MCP changes how a
tool is addressed, never how it is authorized.** A call that arrives at the
router without a grant is not executed, no matter how it got there.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.events import EventBus
from nomad.core.logging import get_logger
from nomad.mcp.hardware import (
    BatteryStatus,
    MockBattery,
    MockDisplay,
    MockHid,
)
from nomad.mcp.server import (
    SERVER_NAME,
    McpToolRouter,
    build_hardware_tools,
    register_hardware_tools,
)
from nomad.storage.db import Database
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets import HidTarget, LocalTarget, TargetRegistry
from nomad.tools.base import ToolContext
from nomad.tools.permissions import (
    AuthorizationGrant,
    GrantSource,
    GrantVault,
    PermissionBroker,
    ToolExecutor,
    ToolRequest,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

SESSION_ID = "mcp-session"


@pytest.fixture
async def rig(db: Database, event_bus: EventBus, tmp_path: Path):
    await ConversationsRepository(db).create_session(mode="auto", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    config = NomadConfig.model_validate({"workspace": {"root": str(root)}})
    workspace = Workspace(root)
    workspace.ensure_exists()

    display, battery, hid = MockDisplay(), MockBattery(), MockHid()
    tools_list = build_hardware_tools(display=display, battery=battery, hid=hid)
    tools = ToolRegistry()
    register_hardware_tools(tools, tools_list)

    targets = TargetRegistry()
    targets.register(LocalTarget())
    targets.register(HidTarget())

    grants = GrantsRepository(db)
    broker = PermissionBroker(
        tools=tools, targets=targets, workspace=workspace, grants=grants, bus=event_bus,
        config=config,
    )
    executor = ToolExecutor(
        tools=tools, targets=targets, workspace=workspace, grants=grants, bus=event_bus,
        config=config,
    )
    vault = GrantVault()
    router = McpToolRouter(executor=executor, vault=vault, specs=[t.spec for t in tools_list])
    return {
        "router": router,
        "vault": vault,
        "broker": broker,
        "tools": tools,
        "display": display,
        "battery": battery,
        "hid": hid,
        "grants": grants,
    }


async def _authorize(rig, tool: str, params: dict, target: str = "local") -> None:
    """Run the real pipeline so the router finds a real grant."""
    request = ToolRequest(
        tool=tool, target_id=target, params=params, session_id=SESSION_ID
    )
    decision = await rig["broker"].decide(request, PermissionMode.AUTO)
    grant = await rig["broker"].authorize(request, decision)
    rig["vault"].stash(grant, request)


# -- the toolset ------------------------------------------------------------


def test_the_toolset_is_what_claude_code_cannot_bring() -> None:
    names = {tool.spec.name for tool in build_hardware_tools()}
    assert names == {
        "get_system_info",
        "display_text",
        "display_card",
        "display_list",
        "display_choice",
        "read_battery",
        "get_context",
        "hid_type_text",
        # Chunk U. Pure utilities: no drivers, no store, no network, so unlike
        # the memory tools they are unconditional. Claude Code can convert a
        # unit by shelling out to python; it cannot do it with the network
        # down, which is the whole point of the offline tier (chunk O).
        "convert_units",
        "world_clock",
    }


def test_the_toolset_defaults_to_mocks() -> None:
    """Mock is the default everywhere, so the suite runs with no hardware (D9)."""
    tools = build_hardware_tools()
    assert len(tools) == 10


def test_registering_hardware_twice_is_harmless() -> None:
    registry = ToolRegistry()
    register_hardware_tools(registry, build_hardware_tools())
    register_hardware_tools(registry, build_hardware_tools())
    assert len(registry.names()) == 10


def test_the_server_name_matches_what_the_bridge_strips() -> None:
    from nomad.agent.permission_bridge import MCP_SERVER_NAME

    assert SERVER_NAME == MCP_SERVER_NAME


# -- authorization is not optional -----------------------------------------


async def test_a_call_with_no_grant_is_refused(rig) -> None:
    result = await rig["router"].call("read_battery", {})
    assert result["isError"] is True
    assert "no authorization on record" in result["content"][0]["text"]
    assert rig["battery"].status is not None  # nothing was read


async def test_a_call_whose_arguments_changed_is_refused(rig) -> None:
    """The grant is for *these* arguments. Swapping them loses it."""
    await _authorize(rig, "display_text", {"text": "hello"})
    result = await rig["router"].call("display_text", {"text": "something else"})
    assert result["isError"] is True
    assert rig["display"].shown == []


async def test_a_grant_cannot_be_replayed(rig) -> None:
    await _authorize(rig, "display_text", {"text": "hello"})
    first = await rig["router"].call("display_text", {"text": "hello"})
    second = await rig["router"].call("display_text", {"text": "hello"})
    assert first["isError"] is False
    assert second["isError"] is True
    assert len(rig["display"].shown) == 1


async def test_an_unknown_tool_is_refused(rig) -> None:
    result = await rig["router"].call("launch_missiles", {})
    assert result["isError"] is True
    assert "unknown hardware tool" in result["content"][0]["text"]


# -- the tools themselves ---------------------------------------------------


async def test_display_text_reaches_the_driver(rig) -> None:
    await _authorize(rig, "display_text", {"text": "hello", "title": "Nomad"})
    result = await rig["router"].call("display_text", {"text": "hello", "title": "Nomad"})
    assert result["isError"] is False
    assert rig["display"].shown == [("hello", "Nomad")]


async def test_display_card_reaches_the_driver(rig) -> None:
    params = {"title": "Weather", "body": "Clear skies", "rows": [{"key": "Temp", "value": "72F"}]}
    await _authorize(rig, "display_card", params)
    result = await rig["router"].call("display_card", params)
    assert result["isError"] is False
    assert rig["display"].cards == [("Weather", "Clear skies", [("Temp", "72F")])]


async def test_display_list_reaches_the_driver(rig) -> None:
    params = {
        "title": "Apps",
        "items": [{"label": "Notes"}, {"label": "Chess", "detail": "vs bot"}],
        "selectable": True,
    }
    await _authorize(rig, "display_list", params)
    result = await rig["router"].call("display_list", params)
    assert result["isError"] is False
    assert rig["display"].lists == [
        ("Apps", [("Notes", None), ("Chess", "vs bot")], True)
    ]


async def test_display_choice_reaches_the_driver(rig) -> None:
    params = {"question": "Continue?", "options": ["Yes", "No"]}
    await _authorize(rig, "display_choice", params)
    result = await rig["router"].call("display_choice", params)
    assert result["isError"] is False
    assert rig["display"].choices == [("Continue?", ["Yes", "No"])]


async def test_get_context_reports_time_battery_network_and_uptime(rig) -> None:
    """Direct tool call: `network_check`/`clock`/`started_at` are injectable
    precisely so this never depends on real network access or wall time."""
    from datetime import datetime

    from nomad.mcp.context import GetContextParams, GetContextTool

    rig["battery"].status = BatteryStatus(percent=55.0, charging=False, voltage=3.8)

    async def reachable() -> bool:
        return True

    tool = GetContextTool(
        rig["battery"],
        network_check=reachable,
        clock=lambda: datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        started_at=0.0,
    )
    target = rig["broker"]._targets.get("local")
    ctx = ToolContext(
        target=target,
        workspace=rig["broker"]._workspace,
        session_id=SESSION_ID,
        turn_id=None,
        logger=get_logger("test"),
    )
    result = await tool.execute(GetContextParams(), ctx)
    assert result.ok
    assert "55%" in result.content
    assert "network reachable" in result.content
    assert result.metadata["network_reachable"] is True
    assert result.metadata["battery_percent"] == 55.0


async def test_get_context_survives_an_unreachable_network(rig) -> None:
    from nomad.mcp.context import GetContextParams, GetContextTool

    async def unreachable() -> bool:
        return False

    tool = GetContextTool(rig["battery"], network_check=unreachable, started_at=0.0)
    target = rig["broker"]._targets.get("local")
    ctx = ToolContext(
        target=target,
        workspace=rig["broker"]._workspace,
        session_id=SESSION_ID,
        turn_id=None,
        logger=get_logger("test"),
    )
    result = await tool.execute(GetContextParams(), ctx)
    assert result.ok
    assert "network unreachable" in result.content
    assert result.metadata["network_reachable"] is False


def test_every_displaydriver_implementation_satisfies_the_widened_protocol() -> None:
    """A mock that takes a different branch than production is worse than no
    mock — every implementation must carry the whole vocabulary."""
    from nomad.hardware import Esp32Display, HeadlessDisplay
    from nomad.mcp.hardware import DisplayDriver
    from nomad.protocol import Link, LinkKind, MockTransport

    assert isinstance(MockDisplay(), DisplayDriver)
    assert isinstance(HeadlessDisplay(), DisplayDriver)

    link = Link(MockTransport(), kind=LinkKind.DISPLAY)
    assert isinstance(Esp32Display(link), DisplayDriver)


async def test_read_battery_reports_charge(rig) -> None:
    rig["battery"].status = BatteryStatus(percent=41.0, charging=True, voltage=3.9)
    await _authorize(rig, "read_battery", {})
    result = await rig["router"].call("read_battery", {})
    assert result["isError"] is False
    assert "41%" in result["content"][0]["text"]
    assert "charging" in result["content"][0]["text"]


async def test_get_system_info_survived_the_pivot(rig) -> None:
    """D19 retired Nomad's filesystem tools; this one it kept."""
    await _authorize(rig, "get_system_info", {})
    result = await rig["router"].call("get_system_info", {})
    assert result["isError"] is False
    assert "system:" in result["content"][0]["text"].lower()


async def test_hid_output_needs_a_hid_target_and_never_auto_approves(rig) -> None:
    """Three independent reasons it cannot be auto-approved (D12, D14, D21)."""
    request = ToolRequest(
        tool="hid_type_text", target_id="hid", params={"text": "x"}, session_id=SESSION_ID
    )
    decision = await rig["broker"].decide(request, PermissionMode.AUTO)
    assert decision.never_auto is True
    assert not decision.allowed


async def test_a_hid_grant_cannot_be_forged_by_hand(rig) -> None:
    """A hand-built grant still fails the executor's own re-validation (D4)."""
    forged = AuthorizationGrant(
        session_id=SESSION_ID,
        tool="hid_type_text",
        target="local",  # wrong target: HID capability is missing there
        scope="none",
        source=GrantSource.AUTO,
    )
    request = ToolRequest(
        tool="hid_type_text", target_id="local", params={"text": "x"}, session_id=SESSION_ID
    )
    rig["vault"].stash(forged, request)
    result = await rig["router"].call("hid_type_text", {"text": "x"})
    assert result["isError"] is True
    assert rig["hid"].typed == []


# -- failures surface as results, never as raises --------------------------


async def test_a_failing_driver_becomes_an_error_result(rig, monkeypatch) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("screen is on fire")

    monkeypatch.setattr(rig["display"], "show_text", boom)
    await _authorize(rig, "display_text", {"text": "hello"})
    result = await rig["router"].call("display_text", {"text": "hello"})
    assert result["isError"] is True
    assert "screen is on fire" in result["content"][0]["text"]


async def test_none_arguments_are_treated_as_empty(rig) -> None:
    await _authorize(rig, "read_battery", {})
    result = await rig["router"].call("read_battery", None)
    assert result["isError"] is False
