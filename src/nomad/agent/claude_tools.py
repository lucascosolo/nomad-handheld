"""Declarations for tools Claude Code owns but Nomad must still gate (D21).

Claude Code keeps its **full** toolset — capability was never the thing to
cripple — and executes those tools itself. Nomad never runs them. But every
one of them passes through `can_use_tool` into the same `PermissionBroker`
that gates Nomad's own tools, and the broker's entire design reads a
`ToolSpec`: risk, permissions, path params, `never_auto`. So each foreign tool
needs a spec.

That is what this module is: a **declaration table**, not an implementation.
Registering these in the ordinary `ToolRegistry` means D14's mode logic, D15's
workspace scoping, session grants, the audit trail and `never_auto` all apply
to Claude Code's `Bash` exactly as they apply to Nomad's `write_file`, with no
second policy engine and no parallel code path. One enforcement point was the
whole point of D21.

Two properties are load-bearing:

* **`execute()` raises.** These tools are declaration-only. If a future
  refactor ever routes one into `ToolExecutor.run()`, it fails loudly instead
  of silently doing nothing or, worse, half-implementing a duplicate.
* **An unlisted tool has no spec, so the broker denies it as "unknown tool".**
  That is the fail-closed default D21 requires: when Claude Code ships a new
  tool, Nomad refuses it until someone classifies it here. Adding a tool to
  this table is a deliberate security review, not a version bump.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from nomad.core.errors import ToolError
from nomad.targets.base import Capability
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec
from nomad.tools.registry import ToolRegistry


class ForeignParams(BaseModel):
    """Params for a tool whose schema belongs to someone else.

    `extra="allow"` is deliberate. The broker validates params against this
    model, and rejecting an argument Nomad has not heard of would break the
    device every time Claude Code added an optional flag. What matters for
    policy is the *path* arguments, and those are named explicitly per tool
    via `path_params`.
    """

    model_config = ConfigDict(extra="allow")


class ForeignTool:
    """A tool Nomad classifies but never executes."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    async def execute(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        raise ToolError(
            f"'{self.spec.name}' is owned by the agent backend and is never executed by Nomad",
            {"tool": self.spec.name},
        )


def _spec(
    name: str,
    description: str,
    *,
    risk: Risk,
    permissions: frozenset[Permission] = frozenset(),
    path_params: tuple[str, ...] = (),
    url_params: tuple[str, ...] = (),
    never_auto: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        params_model=ForeignParams,
        risk=risk,
        permissions=permissions,
        # Every one of these lands on the machine Claude Code runs on, which is
        # the local target. Requiring EXEC excludes a HID target structurally,
        # the same way `get_system_info` does.
        required_capabilities=frozenset({Capability.EXEC}),
        # Never `workspace_confined`: D21 is explicit that Claude Code is not
        # confined by `--add-dir`, so claiming a hard wall here would be a lie
        # in code. The boundary is a policy line — scope plus `never_auto` —
        # and saying so plainly is the honest version.
        workspace_confined=False,
        never_auto=never_auto,
        path_params=path_params,
        url_params=url_params,
    )


#: Claude Code's built-in tools, classified. Verified against CLI 2.1.224.
#:
#: Risk is assigned by what the tool can do at its worst, not by its common
#: case: `Bash` can delete a disk, so it is `never_auto` in every mode however
#: innocuous the command looks. A classifier that reasons about the *command*
#: belongs in `smart` mode (D14), behind the broker — not in this table.
CLAUDE_CODE_TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "Read",
        "Read a file from the filesystem.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        path_params=("file_path",),
    ),
    _spec(
        "Glob",
        "Find files matching a glob pattern.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        path_params=("path",),
    ),
    _spec(
        "Grep",
        "Search file contents with a regular expression.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        path_params=("path",),
    ),
    _spec(
        "NotebookRead",
        "Read a Jupyter notebook.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.FS_READ}),
        path_params=("notebook_path",),
    ),
    _spec(
        "Write",
        "Write a file, creating or overwriting it.",
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        path_params=("file_path",),
    ),
    _spec(
        "Edit",
        "Replace a string inside an existing file.",
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        path_params=("file_path",),
    ),
    _spec(
        "MultiEdit",
        "Apply several string replacements to one file.",
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        path_params=("file_path",),
    ),
    _spec(
        "NotebookEdit",
        "Modify a cell in a Jupyter notebook.",
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        path_params=("notebook_path",),
    ),
    _spec(
        "Bash",
        "Run a shell command.",
        # PRIVILEGED rather than DESTRUCTIVE so the reason the broker reports
        # is the accurate one — it is blocked because a shell is unbounded,
        # not because this particular command was judged destructive.
        risk=Risk.PRIVILEGED,
        permissions=frozenset({Permission.EXEC}),
        path_params=(),
        never_auto=True,
    ),
    _spec(
        "BashOutput",
        "Read buffered output from a background shell.",
        risk=Risk.READ_ONLY,
    ),
    _spec(
        "KillShell",
        "Terminate a background shell.",
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.EXEC}),
        never_auto=True,
    ),
    _spec(
        "WebFetch",
        # READ_ONLY describes its effect on *this* machine. It still transmits,
        # which is why `NETWORK` is excluded from the read-only auto-allow and
        # why the scope is the destination host (D31).
        "Fetch a URL and process it.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.NETWORK}),
        url_params=("url",),
    ),
    _spec(
        "WebSearch",
        "Search the web.",
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.NETWORK}),
    ),
    _spec(
        "TodoWrite",
        "Update the backend's own task list. Affects nothing outside the session.",
        risk=Risk.READ_ONLY,
    ),
    _spec(
        "Task",
        "Delegate work to a sub-agent.",
        # A sub-agent's own tool calls come back through `can_use_tool`
        # individually, so this gates the delegation, not the work.
        risk=Risk.MUTATING,
    ),
    _spec(
        "ExitPlanMode",
        "Leave plan mode.",
        risk=Risk.READ_ONLY,
    ),
    _spec(
        "SlashCommand",
        "Run a slash command.",
        risk=Risk.PRIVILEGED,
        permissions=frozenset({Permission.EXEC}),
        never_auto=True,
    ),
)


def register_backend_tools(
    registry: ToolRegistry, specs: tuple[ToolSpec, ...] = CLAUDE_CODE_TOOLS
) -> ToolRegistry:
    """Add the foreign declarations to `registry`, skipping ones already there.

    Skipping rather than raising matters: Nomad's own `get_system_info` is
    also reachable from the backend via MCP, and a name collision between a
    real tool and a declaration must resolve in favour of the real one.
    """
    for spec in specs:
        if not registry.has(spec.name):
            registry.register(ForeignTool(spec))
    return registry
