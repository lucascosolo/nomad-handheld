"""The local machine as a target (D12). The only fully implemented target.

Every blocking filesystem call goes through `asyncio.to_thread` (D1); command
execution uses asyncio's own subprocess support and never blocks the loop.

`LocalTarget` performs no path confinement of its own — it acts on the paths
it is handed. Confinement is the tool layer's job (`nomad.tools.workspace`,
D15), so that the boundary exists in exactly one place instead of being
re-implemented per target.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import platform
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nomad.core.errors import TargetError
from nomad.core.logging import get_logger
from nomad.targets.base import (
    Capability,
    CommandResult,
    DirEntry,
    FileStat,
    GrepMatch,
    TargetKind,
)

logger = get_logger(__name__)

#: Directory names never descended into by `glob`/`grep`. Walking a virtualenv
#: or a git object store on a Pi is slow and never what was meant.
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
)

MAX_GREP_FILE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 100_000


class LocalTarget:
    """The Pi (or dev laptop) itself: filesystem + exec."""

    kind = TargetKind.LOCAL
    capabilities = frozenset({Capability.FILESYSTEM, Capability.EXEC})

    def __init__(self, target_id: str = "local") -> None:
        self.id = target_id

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"LocalTarget(id={self.id!r})"

    # -- filesystem -------------------------------------------------------

    async def read_text(self, path: Path, *, max_bytes: int = 1_000_000) -> str:
        def _do() -> str:
            if not path.exists():
                raise TargetError(f"No such file: {path}", {"path": str(path)})
            if path.is_dir():
                raise TargetError(f"Is a directory: {path}", {"path": str(path)})
            data = path.read_bytes()[: max_bytes + 1]
            truncated = len(data) > max_bytes
            text = data[:max_bytes].decode("utf-8", errors="replace")
            if truncated:
                text += f"\n... [truncated at {max_bytes} bytes]"
            return text

        return await asyncio.to_thread(_do)

    async def write_text(
        self, path: Path, content: str, *, append: bool = False, create_only: bool = False
    ) -> int:
        def _do() -> int:
            if create_only and path.exists():
                raise TargetError(f"File already exists: {path}", {"path": str(path)})
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with path.open(mode, encoding="utf-8") as handle:
                return handle.write(content)

        return await asyncio.to_thread(_do)

    async def list_dir(self, path: Path) -> list[DirEntry]:
        def _do() -> list[DirEntry]:
            if not path.exists():
                raise TargetError(f"No such directory: {path}", {"path": str(path)})
            if not path.is_dir():
                raise TargetError(f"Not a directory: {path}", {"path": str(path)})
            entries: list[DirEntry] = []
            for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                try:
                    size = child.stat().st_size if child.is_file() else 0
                except OSError:  # pragma: no cover - broken symlink
                    size = 0
                entries.append(
                    DirEntry(
                        name=child.name,
                        path=str(child),
                        is_dir=child.is_dir(),
                        size=size,
                    )
                )
            return entries

        return await asyncio.to_thread(_do)

    async def stat(self, path: Path) -> FileStat:
        def _do() -> FileStat:
            if not path.exists() and not path.is_symlink():
                return FileStat(
                    path=str(path),
                    exists=False,
                    is_dir=False,
                    is_file=False,
                    is_symlink=False,
                    size=0,
                    modified=None,
                )
            info = path.stat()
            return FileStat(
                path=str(path),
                exists=True,
                is_dir=path.is_dir(),
                is_file=path.is_file(),
                is_symlink=path.is_symlink(),
                size=info.st_size,
                modified=info.st_mtime,
            )

        return await asyncio.to_thread(_do)

    async def glob(self, root: Path, pattern: str, *, limit: int = 500) -> list[str]:
        def _do() -> list[str]:
            matches: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                base = Path(dirpath)
                for name in sorted(filenames):
                    candidate = base / name
                    rel = candidate.relative_to(root).as_posix()
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                        matches.append(str(candidate))
                        if len(matches) >= limit:
                            return matches
            return matches

        return await asyncio.to_thread(_do)

    async def grep(
        self,
        root: Path,
        pattern: str,
        *,
        file_glob: str | None = None,
        limit: int = 200,
    ) -> list[GrepMatch]:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise TargetError(f"Invalid regular expression: {exc}", {"pattern": pattern}) from exc

        def _do() -> list[GrepMatch]:
            matches: list[GrepMatch] = []
            targets = [root] if root.is_file() else []
            if not targets:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    base = Path(dirpath)
                    targets.extend(base / name for name in sorted(filenames))
            for candidate in targets:
                if file_glob is not None and not fnmatch.fnmatch(candidate.name, file_glob):
                    continue
                try:
                    if candidate.stat().st_size > MAX_GREP_FILE_BYTES:
                        continue
                    with candidate.open("r", encoding="utf-8") as handle:
                        for number, line in enumerate(handle, start=1):
                            if regex.search(line):
                                matches.append(
                                    GrepMatch(
                                        path=str(candidate),
                                        line_number=number,
                                        line=line.rstrip("\n")[:500],
                                    )
                                )
                                if len(matches) >= limit:
                                    return matches
                except (OSError, UnicodeDecodeError):
                    continue  # binary or unreadable: not an error, just not a match
            return matches

        return await asyncio.to_thread(_do)

    # -- exec -------------------------------------------------------------

    async def run_command(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run `command` through the system shell.

        This is the sharp end of D5. It exists because crippling it would
        defeat the point of a pocket coding agent — the safety comes from
        `run_command` being disabled by default and `never_auto` in every
        mode, not from a neutered implementation.
        """
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            raw_out, raw_err = await process.communicate()

        stdout = raw_out.decode("utf-8", errors="replace")
        stderr = raw_err.decode("utf-8", errors="replace")
        truncated = len(stdout) > MAX_OUTPUT_CHARS or len(stderr) > MAX_OUTPUT_CHARS
        return CommandResult(
            command=command,
            exit_code=-1 if timed_out else (process.returncode or 0),
            stdout=stdout[:MAX_OUTPUT_CHARS],
            stderr=stderr[:MAX_OUTPUT_CHARS],
            timed_out=timed_out,
            truncated=truncated,
        )

    async def system_info(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            usage = shutil.disk_usage(Path.home() if Path.home().exists() else Path("/"))
            uname = platform.uname()
            return {
                "target": self.id,
                "system": uname.system,
                "release": uname.release,
                "machine": uname.machine,
                "processor": uname.processor or uname.machine,
                "hostname": uname.node,
                "python_version": sys.version.split()[0],
                "cpu_count": os.cpu_count() or 1,
                "disk_total_bytes": usage.total,
                "disk_free_bytes": usage.free,
                "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
                "observed_at": datetime.now(UTC).isoformat(),
            }

        return await asyncio.to_thread(_do)
