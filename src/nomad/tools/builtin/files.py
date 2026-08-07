"""Workspace-confined filesystem tools (D5, D12, D15).

These are meant to be genuinely useful — a pocket coding agent that cannot
read a file properly is a toy. The confinement is the workspace root, not a
reduced feature set: every path goes through `ctx.workspace.resolve()` before
it reaches the target, and nothing else about the tools is crippled.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nomad.targets.base import Capability, require_filesystem
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec

MAX_READ_BYTES = 1_000_000


class ReadFileParams(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to read.")
    offset: int = Field(default=1, ge=1, description="First line to return (1-based).")
    limit: int = Field(default=2000, ge=1, le=20000, description="Maximum lines to return.")


class ReadFileTool:
    spec = ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file inside the workspace. Returns numbered lines so "
            "they can be referred to precisely. Large files are truncated."
        ),
        params_model=ReadFileParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: ReadFileParams, ctx: ToolContext) -> ToolResult:
        fs = require_filesystem(ctx.target)
        path = ctx.workspace.resolve(params.path)
        text = await fs.read_text(path, max_bytes=MAX_READ_BYTES)
        lines = text.splitlines()
        start = params.offset - 1
        selected = lines[start : start + params.limit]
        body = "\n".join(
            f"{number:>6}\t{line}" for number, line in enumerate(selected, start=params.offset)
        )
        return ToolResult.success(
            body,
            path=ctx.workspace.relative(path),
            total_lines=len(lines),
            returned_lines=len(selected),
            truncated=start + len(selected) < len(lines),
        )


class WriteFileParams(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to write.")
    content: str = Field(description="Full text to write.")
    mode: Literal["overwrite", "append", "create"] = Field(
        default="overwrite",
        description="'create' fails if the file already exists; 'append' adds to the end.",
    )


class WriteFileTool:
    spec = ToolSpec(
        name="write_file",
        description=(
            "Write a UTF-8 text file inside the workspace, creating parent "
            "directories as needed."
        ),
        params_model=WriteFileParams,
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: WriteFileParams, ctx: ToolContext) -> ToolResult:
        fs = require_filesystem(ctx.target)
        path = ctx.workspace.resolve(params.path)
        written = await fs.write_text(
            path,
            params.content,
            append=params.mode == "append",
            create_only=params.mode == "create",
        )
        relative = ctx.workspace.relative(path)
        return ToolResult.success(
            f"Wrote {written} characters to {relative} ({params.mode}).",
            path=relative,
            characters=written,
            mode=params.mode,
        )


class ListDirParams(BaseModel):
    path: str = Field(default=".", description="Workspace-relative directory to list.")


class ListDirTool:
    spec = ToolSpec(
        name="list_dir",
        description="List the entries of a directory inside the workspace.",
        params_model=ListDirParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: ListDirParams, ctx: ToolContext) -> ToolResult:
        fs = require_filesystem(ctx.target)
        path = ctx.workspace.resolve(params.path)
        entries = await fs.list_dir(path)
        lines = [
            f"{'d' if entry.is_dir else '-'} {entry.size:>10}  {entry.name}" for entry in entries
        ]
        body = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult.success(
            body, path=ctx.workspace.relative(path), entries=len(entries)
        )


class GlobParams(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py' or 'test_*.py'.")
    path: str = Field(default=".", description="Workspace-relative directory to search under.")
    limit: int = Field(default=200, ge=1, le=2000)


class GlobTool:
    spec = ToolSpec(
        name="glob",
        description="Find files inside the workspace by glob pattern.",
        params_model=GlobParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: GlobParams, ctx: ToolContext) -> ToolResult:
        fs = require_filesystem(ctx.target)
        root = ctx.workspace.resolve(params.path)
        matches = await fs.glob(root, params.pattern, limit=params.limit)
        relative = [ctx.workspace.relative(m) for m in matches]
        body = "\n".join(relative) if relative else "(no matches)"
        return ToolResult.success(body, matches=len(relative), pattern=params.pattern)


class GrepParams(BaseModel):
    pattern: str = Field(description="Python regular expression to search for.")
    path: str = Field(default=".", description="Workspace-relative file or directory.")
    file_glob: str | None = Field(
        default=None, description="Only search files whose name matches this glob."
    )
    limit: int = Field(default=200, ge=1, le=2000)


class GrepTool:
    spec = ToolSpec(
        name="grep",
        description=(
            "Search file contents inside the workspace with a regular expression. "
            "Binary and oversized files are skipped."
        ),
        params_model=GrepParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: GrepParams, ctx: ToolContext) -> ToolResult:
        fs = require_filesystem(ctx.target)
        root = ctx.workspace.resolve(params.path)
        matches = await fs.grep(
            root, params.pattern, file_glob=params.file_glob, limit=params.limit
        )
        lines = [
            f"{ctx.workspace.relative(m.path)}:{m.line_number}: {m.line}" for m in matches
        ]
        body = "\n".join(lines) if lines else "(no matches)"
        return ToolResult.success(body, matches=len(matches), pattern=params.pattern)
