"""SSH target — interface defined, implementation deliberately absent (D12).

The interface is complete and the target carries a real identity (host, user,
alias) so permissions can be evaluated per `(tool, target)` today. Every
operation raises `NotImplementedError`; nothing about wiring a real SSH
transport in later requires touching the agent loop, the broker, or any tool.

Note for whoever implements it: `never_auto` (D14) covers *any* action on an
SSH target, in every mode including `auto`. That rule lives in the broker and
must not be relaxed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nomad.targets.base import (
    Capability,
    CommandResult,
    DirEntry,
    FileStat,
    GrepMatch,
    TargetKind,
)

_UNIMPLEMENTED = "SSH target not implemented; see ROADMAP"


class SshTarget:
    """A remote host reachable over SSH, with its own identity."""

    kind = TargetKind.SSH
    capabilities = frozenset({Capability.FILESYSTEM, Capability.EXEC})

    def __init__(
        self,
        *,
        alias: str,
        host: str,
        user: str,
        port: int = 22,
        identity_file: str | None = None,
    ) -> None:
        self.id = f"ssh:{alias}"
        self.alias = alias
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"SshTarget(id={self.id!r}, host={self.host!r}, user={self.user!r})"

    @property
    def identity(self) -> str:
        """Stable, human-readable identity used in prompts and audit records."""
        return f"{self.user}@{self.host}:{self.port}"

    # -- filesystem -------------------------------------------------------

    async def read_text(self, path: Path, *, max_bytes: int = 1_000_000) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def write_text(
        self, path: Path, content: str, *, append: bool = False, create_only: bool = False
    ) -> int:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def list_dir(self, path: Path) -> list[DirEntry]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def stat(self, path: Path) -> FileStat:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def glob(self, root: Path, pattern: str, *, limit: int = 500) -> list[str]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def grep(
        self,
        root: Path,
        pattern: str,
        *,
        file_glob: str | None = None,
        limit: int = 200,
    ) -> list[GrepMatch]:
        raise NotImplementedError(_UNIMPLEMENTED)

    # -- exec -------------------------------------------------------------

    async def run_command(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def system_info(self) -> dict[str, Any]:
        raise NotImplementedError(_UNIMPLEMENTED)
