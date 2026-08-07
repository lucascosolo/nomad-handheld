"""The `Target` abstraction (D12).

A target is *where a tool action lands*. Three kinds, deliberately not one:
`LOCAL` (implemented), `SSH` and `HID` (interfaces defined, stubs raise).

Targets execute what they are given; they never decide whether they should.
Permission policy lives in `nomad.tools.permissions`, and workspace
confinement (D15) lives in `nomad.tools.workspace` — a target happily reads
any path it is handed, which is precisely why the tool layer must resolve
and check the path first.

Capabilities are the *structural* half of the safety story: a `HID` target
has `HID_OUTPUT` and nothing else, so a filesystem tool aimed at it fails a
capability check before any permission logic runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from nomad.core.errors import TargetError


class TargetKind(StrEnum):
    """What sort of place a target is."""

    LOCAL = "local"
    SSH = "ssh"
    HID = "hid"


class Capability(StrEnum):
    """What a target is structurally able to do."""

    FILESYSTEM = "filesystem"
    EXEC = "exec"
    HID_OUTPUT = "hid_output"


class DirEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int


class FileStat(BaseModel):
    path: str
    exists: bool
    is_dir: bool
    is_file: bool
    is_symlink: bool
    size: int
    modified: float | None


class GrepMatch(BaseModel):
    path: str
    line_number: int
    line: str


class CommandResult(BaseModel):
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False


@runtime_checkable
class Target(Protocol):
    """The minimum every target exposes (D12)."""

    id: str
    kind: TargetKind
    capabilities: frozenset[Capability]


@runtime_checkable
class FilesystemOps(Protocol):
    """Filesystem operations. Paths arriving here are already resolved and
    checked by the tool layer (D15) — a target does not enforce the boundary."""

    async def read_text(self, path: Path, *, max_bytes: int = 1_000_000) -> str: ...

    async def write_text(
        self, path: Path, content: str, *, append: bool = False, create_only: bool = False
    ) -> int: ...

    async def list_dir(self, path: Path) -> list[DirEntry]: ...

    async def stat(self, path: Path) -> FileStat: ...

    async def glob(self, root: Path, pattern: str, *, limit: int = 500) -> list[str]: ...

    async def grep(
        self,
        root: Path,
        pattern: str,
        *,
        file_glob: str | None = None,
        limit: int = 200,
    ) -> list[GrepMatch]: ...


@runtime_checkable
class ExecOps(Protocol):
    """Command execution and machine introspection."""

    async def run_command(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...

    async def system_info(self) -> dict[str, Any]: ...


@runtime_checkable
class HidOps(Protocol):
    """Keyboard/pointer output. No filesystem, by construction (D12)."""

    async def send_keys(self, keys: Sequence[str]) -> None: ...

    async def send_text(self, text: str) -> None: ...

    async def move_pointer(self, dx: int, dy: int) -> None: ...

    async def click(self, button: str = "left", *, count: int = 1) -> None: ...


def require_capability(target: Target, capability: Capability) -> None:
    """Raise `TargetError` unless `target` structurally supports `capability`.

    This is the check that must run *before* any permission logic (D12).
    """
    if capability not in target.capabilities:
        raise TargetError(
            f"Target '{target.id}' ({target.kind}) lacks capability '{capability}'",
            {
                "target": target.id,
                "kind": str(target.kind),
                "required": str(capability),
                "capabilities": sorted(str(c) for c in target.capabilities),
            },
        )


def require_capabilities(target: Target, capabilities: frozenset[Capability]) -> None:
    for capability in sorted(capabilities):
        require_capability(target, capability)


def require_filesystem(target: Target) -> FilesystemOps:
    require_capability(target, Capability.FILESYSTEM)
    if not isinstance(target, FilesystemOps):
        raise TargetError(
            f"Target '{target.id}' claims FILESYSTEM but does not implement it",
            {"target": target.id},
        )
    return target


def require_exec(target: Target) -> ExecOps:
    require_capability(target, Capability.EXEC)
    if not isinstance(target, ExecOps):
        raise TargetError(
            f"Target '{target.id}' claims EXEC but does not implement it",
            {"target": target.id},
        )
    return target


def require_hid(target: Target) -> HidOps:
    require_capability(target, Capability.HID_OUTPUT)
    if not isinstance(target, HidOps):
        raise TargetError(
            f"Target '{target.id}' claims HID_OUTPUT but does not implement it",
            {"target": target.id},
        )
    return target
