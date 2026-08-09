"""`run_command` — arbitrary command execution (D5).

This exists because crippling it would defeat the point of a pocket coding
agent. Three things keep it safe, and all three live outside this file:

1. It is **disabled by default** (`tools.enable_run_command = false`), so the
   registry does not even advertise it to the model.
2. Its spec declares `never_auto=True`, so the broker escalates it to a human
   prompt in *every* mode including `auto`, and refuses to mint an automatic
   grant for it at all.
3. Its `cwd` is workspace-confined, so a command cannot be launched from
   outside the root.

What it deliberately does **not** do is filter the command string. A denylist
of dangerous commands is security theatre — `sh -c` defeats any of them — and
pretending otherwise would encourage relaxing the three real controls above.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nomad.targets.base import Capability, require_exec
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec


class RunCommandParams(BaseModel):
    command: str = Field(description="Shell command to run.")
    cwd: str = Field(default=".", description="Workspace-relative working directory.")
    timeout_seconds: float | None = Field(
        default=None, ge=1, le=3600, description="Overrides the configured default."
    )


class RunCommandTool:
    spec = ToolSpec(
        name="run_command",
        description=(
            "Run a shell command on the target, from a directory inside the "
            "workspace. Always requires explicit authorization."
        ),
        params_model=RunCommandParams,
        risk=Risk.PRIVILEGED,
        permissions=frozenset({Permission.EXEC}),
        required_capabilities=frozenset({Capability.EXEC}),
        workspace_confined=True,
        never_auto=True,
        path_params=("cwd",),
    )

    async def execute(self, params: RunCommandParams, ctx: ToolContext) -> ToolResult:
        target = require_exec(ctx.target)
        cwd = ctx.workspace.resolve(params.cwd)
        timeout = params.timeout_seconds or ctx.timeout_seconds
        result = await target.run_command(params.command, cwd=cwd, timeout=timeout)

        sections = [f"$ {result.command}"]
        if result.stdout:
            sections.append(result.stdout.rstrip())
        if result.stderr:
            sections.append(f"[stderr]\n{result.stderr.rstrip()}")
        if result.timed_out:
            sections.append(f"[timed out after {timeout:.0f}s]")
        sections.append(f"[exit {result.exit_code}]")
        body = "\n".join(sections)

        metadata = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "cwd": ctx.workspace.relative(cwd),
        }
        if result.exit_code != 0:
            return ToolResult(
                ok=False, content=body, error=f"exit {result.exit_code}", metadata=metadata
            )
        return ToolResult.success(body, **metadata)
