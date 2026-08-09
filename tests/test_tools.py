"""Tool specs, registry, and the built-in tools (D5, D15)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomad.core.config import NomadConfig
from nomad.core.errors import PermissionDenied, ToolError
from nomad.core.logging import get_logger
from nomad.targets import Capability, HidTarget, LocalTarget
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult
from nomad.tools.builtin import build_default_registry
from nomad.tools.builtin.files import (
    GlobParams,
    GlobTool,
    GrepParams,
    GrepTool,
    ListDirParams,
    ListDirTool,
    ReadFileParams,
    ReadFileTool,
    WriteFileParams,
    WriteFileTool,
)
from nomad.tools.builtin.shell import RunCommandParams, RunCommandTool
from nomad.tools.builtin.system import GetSystemInfoTool, SystemInfoParams
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\n\n\ndef main() -> None:\n    pass\n")
    (root / "notes.md").write_text("# notes\nalpha\nbeta\n")
    (tmp_path / "outside.txt").write_text("secret")
    return Workspace(root)


@pytest.fixture
def ctx(workspace: Workspace) -> ToolContext:
    return ToolContext(
        target=LocalTarget(),
        workspace=workspace,
        session_id="session-1",
        turn_id="turn-1",
        logger=get_logger("nomad.test"),
    )


# -- specs and registry -----------------------------------------------------


def test_spec_exposes_a_json_schema_view_for_the_model() -> None:
    schema = ReadFileTool.spec.to_model_schema()
    assert schema["name"] == "read_file"
    assert schema["risk"] == "read_only"
    assert schema["permissions"] == ["fs_read"]
    assert schema["required_capabilities"] == ["filesystem"]
    assert "path" in schema["input_schema"]["properties"]


def test_specs_declare_coherent_risk_and_permissions() -> None:
    assert WriteFileTool.spec.risk is Risk.MUTATING
    assert Permission.FS_WRITE in WriteFileTool.spec.permissions
    assert WriteFileTool.spec.workspace_confined is True
    assert WriteFileTool.spec.path_params == ("path",)
    assert Capability.FILESYSTEM in GrepTool.spec.required_capabilities
    assert GetSystemInfoTool.spec.risk is Risk.READ_ONLY


def test_run_command_declares_itself_never_auto() -> None:
    """D5: never_auto is a property of the tool, not of the mode."""
    assert RunCommandTool.spec.never_auto is True
    assert RunCommandTool.spec.risk is Risk.PRIVILEGED
    assert RunCommandTool.spec.path_params == ("cwd",)


def test_run_command_is_registered_but_disabled_by_default() -> None:
    registry = build_default_registry(NomadConfig())
    assert registry.has("run_command")
    assert registry.is_enabled("run_command") is False
    assert "run_command" not in [s["name"] for s in registry.model_schemas()]


def test_run_command_is_enabled_when_config_says_so() -> None:
    config = NomadConfig.model_validate({"tools": {"enable_run_command": True}})
    registry = build_default_registry(config)
    assert registry.is_enabled("run_command") is True
    assert "run_command" in [s["name"] for s in registry.model_schemas()]


def test_registry_rejects_duplicates_and_unknown_names() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    with pytest.raises(ToolError, match="already registered"):
        registry.register(ReadFileTool())
    with pytest.raises(ToolError, match="Unknown tool"):
        registry.get("nope")
    assert registry.try_get("nope") is None
    with pytest.raises(ToolError):
        registry.set_enabled("nope", False)


def test_tool_result_helpers() -> None:
    ok = ToolResult.success("body", count=2)
    assert ok.ok and ok.metadata["count"] == 2 and ok.error is None
    bad = ToolResult.failure("boom")
    assert not bad.ok and bad.error == "boom"


# -- built-in tools ---------------------------------------------------------


async def test_get_system_info_runs_on_a_local_target(ctx: ToolContext) -> None:
    result = await GetSystemInfoTool().execute(SystemInfoParams(), ctx)
    assert result.ok
    assert "system:" in result.content
    assert result.metadata["cpu_count"] >= 1


async def test_get_system_info_rejects_a_hid_target(workspace: Workspace) -> None:
    hid_ctx = ToolContext(
        target=HidTarget(),
        workspace=workspace,
        session_id="s",
        turn_id=None,
        logger=get_logger("nomad.test"),
    )
    with pytest.raises(Exception, match="lacks capability"):
        await GetSystemInfoTool().execute(SystemInfoParams(), hid_ctx)


async def test_read_file_returns_numbered_lines(ctx: ToolContext) -> None:
    result = await ReadFileTool().execute(ReadFileParams(path="notes.md"), ctx)
    assert result.ok
    assert result.content.splitlines()[0].endswith("# notes")
    assert result.metadata["total_lines"] == 3


async def test_read_file_honours_offset_and_limit(ctx: ToolContext) -> None:
    result = await ReadFileTool().execute(ReadFileParams(path="notes.md", offset=2, limit=1), ctx)
    assert result.metadata["returned_lines"] == 1
    assert "alpha" in result.content
    assert result.metadata["truncated"] is True


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../outside.txt"])
async def test_read_file_refuses_to_leave_the_workspace(ctx: ToolContext, path: str) -> None:
    with pytest.raises(PermissionDenied):
        await ReadFileTool().execute(ReadFileParams(path=path), ctx)


async def test_write_file_modes(ctx: ToolContext, workspace: Workspace) -> None:
    tool = WriteFileTool()
    await tool.execute(WriteFileParams(path="out/new.txt", content="one"), ctx)
    assert (workspace.root / "out" / "new.txt").read_text() == "one"

    await tool.execute(WriteFileParams(path="out/new.txt", content="-two", mode="append"), ctx)
    assert (workspace.root / "out" / "new.txt").read_text() == "one-two"

    from nomad.core.errors import TargetError

    with pytest.raises(TargetError, match="already exists"):
        await tool.execute(WriteFileParams(path="out/new.txt", content="x", mode="create"), ctx)


async def test_write_file_refuses_to_leave_the_workspace(ctx: ToolContext) -> None:
    with pytest.raises(PermissionDenied):
        await WriteFileTool().execute(WriteFileParams(path="../escaped.txt", content="x"), ctx)


async def test_write_file_refuses_a_symlink_out_of_the_workspace(
    ctx: ToolContext, workspace: Workspace, tmp_path: Path
) -> None:
    (workspace.root / "door").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionDenied):
        await WriteFileTool().execute(WriteFileParams(path="door/planted.txt", content="x"), ctx)
    assert not (tmp_path / "planted.txt").exists()


async def test_list_dir(ctx: ToolContext) -> None:
    result = await ListDirTool().execute(ListDirParams(path="."), ctx)
    assert result.ok
    assert "notes.md" in result.content
    assert result.metadata["entries"] == 2


async def test_glob(ctx: ToolContext) -> None:
    result = await GlobTool().execute(GlobParams(pattern="*.py"), ctx)
    assert result.content.strip() == "src/app.py"
    assert result.metadata["matches"] == 1


async def test_grep(ctx: ToolContext) -> None:
    result = await GrepTool().execute(GrepParams(pattern="alpha"), ctx)
    assert result.metadata["matches"] == 1
    assert result.content.startswith("notes.md:2:")

    empty = await GrepTool().execute(GrepParams(pattern="zzz-not-here"), ctx)
    assert empty.content == "(no matches)"


async def test_run_command_executes_inside_the_workspace(
    ctx: ToolContext, workspace: Workspace
) -> None:
    result = await RunCommandTool().execute(RunCommandParams(command="pwd", cwd="src"), ctx)
    assert result.ok
    assert str(workspace.root / "src") in result.content
    assert result.metadata["exit_code"] == 0


async def test_run_command_reports_a_non_zero_exit_without_raising(ctx: ToolContext) -> None:
    result = await RunCommandTool().execute(RunCommandParams(command="exit 7"), ctx)
    assert result.ok is False
    assert result.error == "exit 7"
    assert "[exit 7]" in result.content


async def test_run_command_cwd_is_workspace_confined(ctx: ToolContext) -> None:
    with pytest.raises(PermissionDenied):
        await RunCommandTool().execute(RunCommandParams(command="ls", cwd="/etc"), ctx)
