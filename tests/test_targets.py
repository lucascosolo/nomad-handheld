"""Targets and their capabilities (D12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomad.core.errors import TargetError
from nomad.targets import (
    Capability,
    ExecOps,
    FilesystemOps,
    HidOps,
    HidTarget,
    LocalTarget,
    SshTarget,
    TargetKind,
    TargetRegistry,
    require_capabilities,
    require_capability,
    require_exec,
    require_filesystem,
    require_hid,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "alpha.py").write_text("import os\nvalue = 1\n")
    (tmp_path / "pkg" / "beta.txt").write_text("value = 2\nother\n")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "junk.py").write_text("value = 3\n")
    return tmp_path


# -- capability shape -------------------------------------------------------


def test_local_target_has_filesystem_and_exec() -> None:
    target = LocalTarget()
    assert target.kind is TargetKind.LOCAL
    assert target.capabilities == frozenset({Capability.FILESYSTEM, Capability.EXEC})


def test_hid_target_has_only_hid_output() -> None:
    target = HidTarget()
    assert target.kind is TargetKind.HID
    assert target.capabilities == frozenset({Capability.HID_OUTPUT})
    assert Capability.FILESYSTEM not in target.capabilities


def test_ssh_target_carries_an_explicit_identity() -> None:
    target = SshTarget(alias="workstation", host="10.0.0.5", user="lucas")
    assert target.id == "ssh:workstation"
    assert target.kind is TargetKind.SSH
    assert target.identity == "lucas@10.0.0.5:22"


def test_a_filesystem_tool_cannot_be_pointed_at_a_hid_target() -> None:
    """The structural half of D12: this fails on capabilities alone."""
    hid = HidTarget()
    with pytest.raises(TargetError) as excinfo:
        require_filesystem(hid)
    assert "lacks capability" in str(excinfo.value)
    assert not isinstance(hid, FilesystemOps)


def test_hid_target_is_not_an_exec_target() -> None:
    with pytest.raises(TargetError):
        require_exec(HidTarget())


def test_local_target_is_not_a_hid_target() -> None:
    with pytest.raises(TargetError):
        require_hid(LocalTarget())


def test_require_capabilities_accepts_a_satisfied_set() -> None:
    require_capabilities(LocalTarget(), frozenset({Capability.FILESYSTEM, Capability.EXEC}))
    require_capability(HidTarget(), Capability.HID_OUTPUT)


def test_protocol_conformance() -> None:
    assert isinstance(LocalTarget(), FilesystemOps)
    assert isinstance(LocalTarget(), ExecOps)
    assert isinstance(HidTarget(), HidOps)


# -- stubs ------------------------------------------------------------------


async def test_ssh_operations_raise_not_implemented(tmp_path: Path) -> None:
    target = SshTarget(alias="ws", host="h", user="u")
    with pytest.raises(NotImplementedError, match="ROADMAP"):
        await target.read_text(tmp_path / "x")
    with pytest.raises(NotImplementedError):
        await target.run_command("ls", cwd=tmp_path)
    with pytest.raises(NotImplementedError):
        await target.system_info()


async def test_hid_operations_raise_not_implemented() -> None:
    target = HidTarget()
    with pytest.raises(NotImplementedError, match="ROADMAP"):
        await target.send_text("hello")
    with pytest.raises(NotImplementedError):
        await target.send_keys(["ctrl", "c"])
    with pytest.raises(NotImplementedError):
        await target.move_pointer(1, 1)
    with pytest.raises(NotImplementedError):
        await target.click()


# -- registry ---------------------------------------------------------------


def test_registry_resolves_lists_and_filters() -> None:
    registry = TargetRegistry()
    local, hid = LocalTarget(), HidTarget()
    ssh = SshTarget(alias="ws", host="h", user="u")
    for target in (local, hid, ssh):
        registry.register(target)

    assert registry.get("local") is local
    assert registry.ids() == ["local", "hid", "ssh:ws"]
    assert registry.of_kind(TargetKind.SSH) == [ssh]
    assert registry.with_capability(Capability.HID_OUTPUT) == [hid]
    assert registry.try_get("nope") is None
    assert {entry["id"] for entry in registry.describe()} == {"local", "hid", "ssh:ws"}


def test_registry_rejects_unknown_and_duplicate_targets() -> None:
    registry = TargetRegistry()
    registry.register(LocalTarget())
    with pytest.raises(TargetError, match="already registered"):
        registry.register(LocalTarget())
    with pytest.raises(TargetError, match="Unknown target"):
        registry.get("ssh:nope")


# -- local filesystem and exec ---------------------------------------------


async def test_local_read_write_and_stat(sandbox: Path) -> None:
    target = LocalTarget()
    path = sandbox / "pkg" / "gamma.txt"

    assert await target.write_text(path, "hello") == 5
    assert await target.read_text(path) == "hello"
    assert await target.write_text(path, " again", append=True) == 6
    assert await target.read_text(path) == "hello again"

    info = await target.stat(path)
    assert info.exists and info.is_file and info.size == 11

    missing = await target.stat(sandbox / "nope")
    assert missing.exists is False


async def test_local_write_create_only_refuses_to_clobber(sandbox: Path) -> None:
    target = LocalTarget()
    path = sandbox / "pkg" / "alpha.py"
    with pytest.raises(TargetError, match="already exists"):
        await target.write_text(path, "x", create_only=True)


async def test_local_read_errors_are_target_errors(sandbox: Path) -> None:
    target = LocalTarget()
    with pytest.raises(TargetError, match="No such file"):
        await target.read_text(sandbox / "missing.txt")
    with pytest.raises(TargetError, match="Is a directory"):
        await target.read_text(sandbox / "pkg")


async def test_local_read_truncates_large_files(sandbox: Path) -> None:
    target = LocalTarget()
    path = sandbox / "big.txt"
    path.write_text("x" * 5000)
    text = await target.read_text(path, max_bytes=100)
    assert text.startswith("x" * 100)
    assert "truncated" in text


async def test_local_list_dir(sandbox: Path) -> None:
    target = LocalTarget()
    names = [entry.name for entry in await target.list_dir(sandbox / "pkg")]
    assert names[0] == "__pycache__"  # directories first
    assert set(names) == {"__pycache__", "alpha.py", "beta.txt"}

    with pytest.raises(TargetError, match="Not a directory"):
        await target.list_dir(sandbox / "pkg" / "alpha.py")


async def test_local_glob_skips_noise_directories(sandbox: Path) -> None:
    target = LocalTarget()
    matches = await target.glob(sandbox, "*.py")
    assert [Path(m).name for m in matches] == ["alpha.py"]


async def test_local_grep_matches_and_filters(sandbox: Path) -> None:
    target = LocalTarget()
    matches = await target.grep(sandbox, r"^value = \d")
    assert {Path(m.path).name for m in matches} == {"alpha.py", "beta.txt"}

    filtered = await target.grep(sandbox, r"value", file_glob="*.py")
    assert {Path(m.path).name for m in filtered} == {"alpha.py"}
    assert filtered[0].line_number == 2


async def test_local_grep_rejects_a_bad_regex(sandbox: Path) -> None:
    with pytest.raises(TargetError, match="Invalid regular expression"):
        await LocalTarget().grep(sandbox, "(unclosed")


async def test_local_run_command(sandbox: Path) -> None:
    target = LocalTarget()
    result = await target.run_command("echo nomad", cwd=sandbox)
    assert result.exit_code == 0
    assert result.stdout.strip() == "nomad"
    assert result.timed_out is False

    failure = await target.run_command("exit 3", cwd=sandbox)
    assert failure.exit_code == 3


async def test_local_run_command_times_out(sandbox: Path) -> None:
    result = await LocalTarget().run_command("sleep 5", cwd=sandbox, timeout=0.2)
    assert result.timed_out is True
    assert result.exit_code == -1


async def test_local_system_info() -> None:
    info = await LocalTarget().system_info()
    assert info["target"] == "local"
    assert info["cpu_count"] >= 1
    assert "python_version" in info and "system" in info
