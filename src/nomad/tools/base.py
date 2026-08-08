"""Tool contracts: risk, permissions, spec, context, result (D5).

Every tool ships a `ToolSpec` declaring what it can do and how dangerous it
is. The model sees these declarations. Security is structural: the broker
reads the spec, not the system prompt.

A tool never touches the filesystem itself — it receives a `ToolContext`
carrying a `Target` (D12) and a `Workspace` (D15), resolves its paths through
the workspace, and hands absolute paths to the target.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from logging import LoggerAdapter
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from nomad.targets.base import Capability, Target
from nomad.tools.workspace import Workspace


class Risk(StrEnum):
    """How much damage a tool can do if it is wrong (D5)."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    PRIVILEGED = "privileged"
    DESTRUCTIVE = "destructive"
    EXTERNAL_DEVICE = "external_device"


class Permission(StrEnum):
    """What a tool needs to be allowed to do (D5)."""

    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    EXEC = "exec"
    HID_OUTPUT = "hid_output"
    NETWORK = "network"
    SYSTEM_INFO = "system_info"


#: Permissions whose use outside the workspace root is `never_auto` (D14).
ESCALATING_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.FS_WRITE, Permission.EXEC, Permission.HID_OUTPUT}
)


class ToolSpec(BaseModel):
    """The declaration the broker and the model both read.

    `path_params` names the parameters that are filesystem paths. The broker
    uses it to compute a grant scope (D14) without knowing anything about a
    specific tool — which is what keeps "approving `write_file` in the
    workspace does not approve it outside" a property of the *pipeline* rather
    than of each tool's implementation.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    params_model: type[BaseModel]
    risk: Risk
    permissions: frozenset[Permission] = frozenset()
    required_capabilities: frozenset[Capability] = frozenset()
    #: Paths must resolve inside the workspace root or the call is denied (D15).
    workspace_confined: bool = False
    #: Never auto-approved in any mode, regardless of scope or risk (D5/D14).
    never_auto: bool = False
    path_params: tuple[str, ...] = ()
    #: Names the parameters that are outbound URLs, so a `NETWORK` tool can be
    #: scoped by destination host (D31) exactly as `path_params` scopes a file
    #: tool by location. A network tool with none still scopes as `net:`.
    url_params: tuple[str, ...] = ()

    def to_model_schema(self) -> dict[str, Any]:
        """The JSON-schema view handed to the model.

        Risk and permissions are included deliberately: the model should be
        able to see that a tool is dangerous, even though nothing about
        enforcement depends on it noticing.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params_model.model_json_schema(),
            "risk": str(self.risk),
            "permissions": sorted(str(p) for p in self.permissions),
            "required_capabilities": sorted(str(c) for c in self.required_capabilities),
            "never_auto": self.never_auto,
        }


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool is allowed to know about the call.

    Notably absent: the conversation, the model, and the grant. A tool that
    could read the grant could be tempted to reason about it; validation is
    the executor's job and happens before `execute` is ever reached.
    """

    target: Target
    workspace: Workspace
    session_id: str
    turn_id: str | None
    logger: LoggerAdapter
    timeout_seconds: float = 120.0


class ToolResult(BaseModel):
    """What a tool hands back — and what the model eventually observes."""

    ok: bool
    content: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolResult:
        return cls(ok=True, content=content, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> ToolResult:
        return cls(ok=False, content="", error=error, metadata=metadata)


@runtime_checkable
class Tool(Protocol):
    """A capability the model can request (D5)."""

    spec: ToolSpec

    async def execute(self, params: BaseModel, ctx: ToolContext) -> ToolResult: ...
