"""The swappable agent backend (D24).

Nomad does not implement its own think->tool->observe loop any more; a backend
does (D19). But *which* backend is deliberately not knowable from above this
module, because the long-term goal is to replace the cloud model with a local
one reached over Tailscale, and that swap is only cheap if nothing upstream
has learned the current backend's shape.

Three rules make it cheap, and all three are enforced elsewhere:

* `claude-agent-sdk` is imported in exactly one module,
  `agent/backends/claude_cli.py`. `tests/test_sdk_boundary.py` enforces it.
* Backends emit Nomad's own `AgentEvent`. **No SDK type crosses this
  boundary** — a backend that leaked one would make the interface a lie.
* The permission bridge (D21) sits *above* the backend, so gating is
  backend-independent. A local model gets policed by the same broker.

**The honest asymmetry** (D24), recorded here because it is the thing most
likely to be forgotten: Claude Code brings its own loop, tools and compaction.
A raw local LLM brings none of the three. So this interface must never assume
the backend is agentic. Backends declare what they bring via `capabilities`,
and Nomad supplies the rest — which is why `BackendCapability` exists rather
than every backend pretending to be Claude Code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BackendCapability(StrEnum):
    """What a backend brings for itself (D24).

    Absence is the interesting direction: a backend *without* `OWN_TOOLS`
    needs Nomad to advertise its tool registry and execute the calls, which is
    exactly what the retired `agent/loop.py` did and what `remote_llm` will
    have to grow back.
    """

    #: Runs its own think->tool->observe loop; `send()` returns a whole turn.
    OWN_LOOP = "own_loop"
    #: Brings its own toolset; Nomad does not advertise or execute tools for it.
    OWN_TOOLS = "own_tools"
    #: Manages its own context window; `[agent].compact_at` is ignored.
    OWN_COMPACTION = "own_compaction"


class AgentEventKind(StrEnum):
    """Nomad's own event vocabulary. Deliberately smaller than any SDK's."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    TURN_COMPLETE = "turn_complete"
    ERROR = "error"


class AgentEvent(BaseModel):
    """One thing that happened inside a backend.

    A plain Pydantic model rather than a passthrough of the backend's own type
    — this is the boundary D24 exists to defend. `raw` carries backend-specific
    detail as plain JSON-able data for logging and display; it is never a live
    SDK object and nothing may branch on its contents to decide policy.
    """

    kind: AgentEventKind
    session_id: str
    turn_id: str | None = None
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_use_id: str | None = None
    ok: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AgentBackend(Protocol):
    """Whatever is currently running the conversation (D24)."""

    name: str
    capabilities: frozenset[BackendCapability]

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, text: str, *, session_id: str) -> None: ...

    def events(self) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...
