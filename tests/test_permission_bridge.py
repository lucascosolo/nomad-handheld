"""The `can_use_tool` bridge (D21). Security-critical — these are the contract.

The bridge is the only thing between the model and the device: Claude Code
keeps its full toolset and is not confined by `--add-dir`, so if a call gets
past here, nothing else stops it. Every test below is really the same
question asked in a different way — *does the unhappy path deny?*
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from nomad.agent.claude_tools import CLAUDE_CODE_TOOLS, ForeignTool, register_backend_tools
from nomad.agent.permission_bridge import (
    MCP_SERVER_NAME,
    PermissionBridge,
)
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import ToolError
from nomad.core.events import EventBus
from nomad.mcp.server import build_hardware_tools, register_hardware_tools
from nomad.storage.db import Database
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets import HidTarget, LocalTarget, TargetRegistry
from nomad.tools.permissions import (
    SCOPE_SOURCE_TREE,
    AuthorizationQueue,
    GrantVault,
    PermissionBroker,
    ToolExecutor,
    ToolRequest,
    nomad_source_root,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

SESSION_ID = "bridge-session"


@dataclass
class Harness:
    bridge: PermissionBridge
    broker: PermissionBroker
    queue: AuthorizationQueue
    tools: ToolRegistry
    grants: GrantsRepository
    vault: GrantVault
    workspace: Workspace
    executor: ToolExecutor
    mode: PermissionMode = PermissionMode.MANUAL

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode


def _harness(
    db: Database,
    bus: EventBus,
    root: Path,
    *,
    auth_timeout: float = 5.0,
    bridge_timeout: float = 5.0,
) -> Harness:
    config = NomadConfig.model_validate({"workspace": {"root": str(root)}})
    workspace = Workspace(root)
    workspace.ensure_exists()

    tools = ToolRegistry()
    register_hardware_tools(tools, build_hardware_tools())
    register_backend_tools(tools)

    targets = TargetRegistry()
    targets.register(LocalTarget())
    targets.register(HidTarget())

    grants = GrantsRepository(db)
    broker = PermissionBroker(
        tools=tools, targets=targets, workspace=workspace, grants=grants, bus=bus, config=config
    )
    executor = ToolExecutor(
        tools=tools, targets=targets, workspace=workspace, grants=grants, bus=bus, config=config
    )
    queue = AuthorizationQueue(
        broker=broker, grants=grants, bus=bus, default_timeout=auth_timeout
    )
    vault = GrantVault()
    harness = Harness(
        bridge=None,  # type: ignore[arg-type]
        broker=broker,
        queue=queue,
        tools=tools,
        grants=grants,
        vault=vault,
        workspace=workspace,
        executor=executor,
    )
    harness.bridge = PermissionBridge(
        broker=broker,
        queue=queue,
        tools=tools,
        bus=bus,
        session_id_provider=lambda: SESSION_ID,
        mode_provider=lambda: harness.mode,
        vault=vault,
        timeout=bridge_timeout,
    )
    return harness


def _workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("hello\n")
    return root


@pytest.fixture
async def harness(db: Database, event_bus: EventBus, tmp_path: Path) -> Harness:
    """A generous prompt timeout, for tests that answer the prompt themselves."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    return _harness(db, event_bus, _workspace_root(tmp_path))


@pytest.fixture
async def impatient(db: Database, event_bus: EventBus, tmp_path: Path) -> Harness:
    """A prompt nobody will answer, so it should expire quickly.

    Separate from `harness` purely for speed: the `never_auto` tests below all
    park on a prompt on purpose, and waiting out a realistic timeout a dozen
    times would add a minute to the suite for no extra coverage.
    """
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    return _harness(db, event_bus, _workspace_root(tmp_path), auth_timeout=0.1)


async def await_pending(harness: Harness, *, timeout: float = 3.0) -> object:
    """Wait for a prompt to actually appear.

    Polling rather than a fixed sleep: `AuthorizationQueue.request` persists
    the pending row before registering it in memory, and on a slow machine that
    write can outlast any sleep short enough to keep the suite quick.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = harness.queue.list_pending()
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no authorization prompt appeared")


# -- fail closed ------------------------------------------------------------


async def test_an_unclassified_tool_is_denied_even_in_auto_mode(harness: Harness) -> None:
    """A tool Claude Code ships that Nomad has never classified must not run.

    This is the property that makes a CLI upgrade safe: a new built-in tool
    arrives with no `ToolSpec`, so it is refused until someone reviews it.
    """
    harness.set_mode(PermissionMode.AUTO)
    decision = await harness.bridge.can_use_tool("SomeBrandNewTool", {"x": 1})
    assert decision.allow is False
    assert "no permission declaration" in decision.reason


async def test_a_tool_from_another_mcp_server_is_denied(harness: Harness) -> None:
    harness.set_mode(PermissionMode.AUTO)
    decision = await harness.bridge.can_use_tool("mcp__someone_else__do_thing", {})
    assert decision.allow is False


async def test_non_object_input_is_denied_rather_than_coerced(harness: Harness) -> None:
    harness.set_mode(PermissionMode.AUTO)
    decision = await harness.bridge.can_use_tool("Read", ["not", "a", "dict"])  # type: ignore[arg-type]
    assert decision.allow is False
    assert "not an object" in decision.reason


async def test_a_broker_that_raises_denies(harness: Harness, monkeypatch) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(harness.broker, "decide", boom)
    harness.set_mode(PermissionMode.AUTO)
    decision = await harness.bridge.can_use_tool("Read", {"file_path": "notes.md"})
    assert decision.allow is False
    assert "permission error" in decision.reason


async def test_a_broker_that_hangs_denies_on_timeout(
    db: Database, event_bus: EventBus, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    h = _harness(db, event_bus, root, bridge_timeout=0.1)

    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(h.broker, "decide", hang)
    decision = await h.bridge.can_use_tool("Read", {"file_path": "notes.md"})
    assert decision.allow is False
    assert "timed out" in decision.reason


async def test_an_unanswered_prompt_denies(
    db: Database, event_bus: EventBus, tmp_path: Path
) -> None:
    """The failure mode of an unattended pocket device is 'did nothing'."""
    root = tmp_path / "ws"
    root.mkdir()
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    h = _harness(db, event_bus, root, auth_timeout=0.1)
    decision = await h.bridge.can_use_tool("Write", {"file_path": "new.txt"})
    assert decision.allow is False
    assert "timeout" in decision.reason


# -- never_auto (D14, D21) --------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.MANUAL, PermissionMode.SESSION, PermissionMode.SMART, PermissionMode.AUTO],
)
async def test_bash_is_never_auto_in_any_mode(impatient: Harness, mode: PermissionMode) -> None:
    impatient.set_mode(mode)
    decision = await impatient.bridge.can_use_tool("Bash", {"command": "ls"})
    assert decision.allow is False, f"Bash auto-approved in {mode}"


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.MANUAL, PermissionMode.SESSION, PermissionMode.SMART, PermissionMode.AUTO],
)
async def test_hid_output_is_never_auto_in_any_mode(
    impatient: Harness, mode: PermissionMode
) -> None:
    """A mode switch must never turn this device into a keystroke injector."""
    impatient.set_mode(mode)
    decision = await impatient.bridge.can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__hid_type_text", {"text": "rm -rf /"}
    )
    assert decision.allow is False, f"HID output auto-approved in {mode}"


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.MANUAL, PermissionMode.SESSION, PermissionMode.SMART, PermissionMode.AUTO],
)
async def test_writing_nomads_own_source_tree_is_never_auto(
    impatient: Harness, mode: PermissionMode
) -> None:
    """D21: core changes go through D22's self-update path, never in place."""
    impatient.set_mode(mode)
    target = nomad_source_root() / "src" / "nomad" / "agent" / "session.py"
    decision = await impatient.bridge.can_use_tool("Write", {"file_path": str(target)})
    assert decision.allow is False, f"source-tree write auto-approved in {mode}"


async def test_reading_nomads_own_source_is_allowed(harness: Harness) -> None:
    """Forbidding *writes* is the rule; a device that cannot read its own code
    cannot reason about itself (D22)."""
    harness.set_mode(PermissionMode.MANUAL)
    target = nomad_source_root() / "pyproject.toml"
    decision = await harness.bridge.can_use_tool("Read", {"file_path": str(target)})
    assert decision.allow is True


async def test_source_tree_scope_wins_over_outside(harness: Harness) -> None:
    from nomad.tools.permissions import compute_scope

    spec = harness.tools.get("Write").spec
    target = harness.broker  # unused; keep the import honest
    assert target is not None
    scope = compute_scope(
        spec,
        LocalTarget(),
        {"file_path": str(nomad_source_root() / "src" / "nomad" / "__init__.py")},
        harness.workspace,
    )
    assert scope == SCOPE_SOURCE_TREE


# -- the allow path ---------------------------------------------------------


async def test_read_only_in_the_workspace_allows_and_mints_a_grant(harness: Harness) -> None:
    """Auto-approval mints a grant; it never skips minting one (D4)."""
    decision = await harness.bridge.can_use_tool("Read", {"file_path": "notes.md"})
    assert decision.allow is True
    assert decision.grant_id is not None
    stored = await harness.grants.get_grant(decision.grant_id)
    assert stored is not None and stored.tool == "Read"


async def test_an_approved_prompt_allows(harness: Harness) -> None:
    task = asyncio.ensure_future(
        harness.bridge.can_use_tool("Write", {"file_path": "new.txt", "content": "x"})
    )
    pending = await await_pending(harness)
    assert pending.tool == "Write"
    await harness.queue.approve(pending.id, mode=PermissionMode.MANUAL)
    decision = await task
    assert decision.allow is True


async def test_a_denied_prompt_denies(harness: Harness) -> None:
    task = asyncio.ensure_future(
        harness.bridge.can_use_tool("Write", {"file_path": "new.txt", "content": "x"})
    )
    pending = await await_pending(harness)
    await harness.queue.deny(pending.id, "not this time")
    decision = await task
    assert decision.allow is False


async def test_auto_mode_allows_an_ordinary_workspace_write(harness: Harness) -> None:
    harness.set_mode(PermissionMode.AUTO)
    decision = await harness.bridge.can_use_tool(
        "Write", {"file_path": "new.txt", "content": "x"}
    )
    assert decision.allow is True


# -- routing and naming -----------------------------------------------------


def test_mcp_names_are_stripped_only_for_nomads_own_server() -> None:
    assert PermissionBridge.nomad_tool_name(f"mcp__{MCP_SERVER_NAME}__read_battery") == (
        "read_battery"
    )
    assert PermissionBridge.nomad_tool_name("mcp__other__read_battery") == (
        "mcp__other__read_battery"
    )
    assert PermissionBridge.nomad_tool_name("Bash") == "Bash"


async def test_a_hid_tool_is_routed_at_the_hid_target(harness: Harness) -> None:
    """Routing follows declared capabilities, not the tool's name (D12)."""
    task = asyncio.ensure_future(
        harness.bridge.can_use_tool(f"mcp__{MCP_SERVER_NAME}__hid_type_text", {"text": "hi"})
    )
    pending = await await_pending(harness)
    assert pending.target == "hid"
    await harness.queue.deny(pending.id)
    await task


# -- the grant vault --------------------------------------------------------


async def test_an_allowed_call_stashes_a_claimable_grant(harness: Harness) -> None:
    decision = await harness.bridge.can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__read_battery", {}
    )
    assert decision.allow is True
    claimed = harness.vault.pop("read_battery", {})
    assert claimed is not None and claimed[0].id == decision.grant_id


async def test_a_stashed_grant_is_single_use(harness: Harness) -> None:
    await harness.bridge.can_use_tool(f"mcp__{MCP_SERVER_NAME}__read_battery", {})
    assert harness.vault.pop("read_battery", {}) is not None
    assert harness.vault.pop("read_battery", {}) is None


async def test_a_stashed_grant_does_not_match_different_arguments(harness: Harness) -> None:
    """Approval is for *these* arguments. Changing them loses the grant."""
    harness.set_mode(PermissionMode.AUTO)
    await harness.bridge.can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__display_text", {"text": "hello"}
    )
    assert harness.vault.pop("display_text", {"text": "goodbye"}) is None
    assert harness.vault.pop("display_text", {"text": "hello"}) is not None


def test_the_vault_expires_entries() -> None:
    vault = GrantVault(ttl_seconds=-1.0)
    request = ToolRequest(tool="read_battery", params={}, session_id=SESSION_ID)
    from nomad.tools.permissions import AuthorizationGrant, GrantSource

    vault.stash(
        AuthorizationGrant(
            session_id=SESSION_ID,
            tool="read_battery",
            target="local",
            scope="none",
            source=GrantSource.AUTO,
        ),
        request,
    )
    assert vault.pop("read_battery", {}) is None
    assert len(vault) == 0


# -- the declaration table --------------------------------------------------


def test_every_declared_tool_refuses_to_be_executed_by_nomad() -> None:
    """These are declarations. Nomad classifies them; the backend runs them."""
    tool = ForeignTool(CLAUDE_CODE_TOOLS[0])
    with pytest.raises(ToolError, match="never executed by Nomad"):
        asyncio.run(tool.execute(tool.spec.params_model(), None))  # type: ignore[arg-type]


def test_registering_declarations_never_shadows_a_real_tool() -> None:
    registry = ToolRegistry()
    register_hardware_tools(registry, build_hardware_tools())
    register_backend_tools(registry)
    # `get_system_info` is a real Nomad tool reachable over MCP. If a foreign
    # declaration ever shadowed it, the model would get a tool that raises.
    assert not isinstance(registry.get("get_system_info"), ForeignTool)


def test_the_declaration_table_covers_the_dangerous_tools() -> None:
    names = {spec.name for spec in CLAUDE_CODE_TOOLS}
    assert {"Bash", "Write", "Edit", "Read", "WebFetch"} <= names
    by_name = {spec.name: spec for spec in CLAUDE_CODE_TOOLS}
    assert by_name["Bash"].never_auto is True
    assert by_name["Read"].path_params == ("file_path",)
