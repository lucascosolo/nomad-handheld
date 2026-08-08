"""Memory, as tools the model can call.

Same shape as `mcp/hardware.py`: a `ToolSpec`, a pydantic params model, an
`execute(params, ctx) -> ToolResult`. Being reachable over MCP changes how a
tool is addressed, never how it is authorized — these pass the broker and the
`GrantVault` exactly like `display_card` does.

Result strings follow `NOMAD.md`'s small-screen contract: short, lead with the
fact, no preamble. `recall` truncates each line, because its output lands in
the live context on every call and the whole design of this package is that
what enters the window stays bounded.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nomad.memory.errors import MemoryRefused
from nomad.memory.models import MAX_TEXT_CHARS, Memory, MemoryKind
from nomad.memory.store import MemoryStore
from nomad.tools.base import Risk, Tool, ToolContext, ToolResult, ToolSpec

#: How much of a memory a `recall` result shows per row.
RECALL_PREVIEW_CHARS = 160


def _preview(memory: Memory) -> str:
    text = " ".join(memory.text.split())
    if len(text) > RECALL_PREVIEW_CHARS:
        text = text[: RECALL_PREVIEW_CHARS - 1].rstrip() + "…"
    pin = " [pinned]" if memory.pinned else ""
    return f"- {text} ({memory.kind}, {memory.id}){pin}"


class RememberParams(BaseModel):
    text: str = Field(
        max_length=MAX_TEXT_CHARS,
        description=(
            "One small fact, a sentence or two. Split anything longer into "
            "separate memories. Never a credential."
        ),
    )
    kind: MemoryKind = Field(
        default=MemoryKind.FACT,
        description="operator | preference | project | fact.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Words you would search for this by later.",
    )
    pinned: bool = Field(
        default=False,
        description=(
            "Inject this at the start of every future session. Scarce and "
            "capped — pin only what shapes every conversation."
        ),
    )


class RememberTool:
    """Store one fact so it survives this session."""

    spec = ToolSpec(
        name="remember",
        description=(
            "Remember one small fact about your operator or their work so it "
            "survives this session. Pin only what shapes every conversation."
        ),
        params_model=RememberParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def execute(self, params: RememberParams, ctx: ToolContext) -> ToolResult:
        try:
            memory = await self._store.remember(
                params.text,
                kind=params.kind,
                keywords=params.keywords,
                pinned=params.pinned,
                session_id=ctx.session_id,
            )
        except MemoryRefused as exc:
            # A refusal is an answer, not a crash: the model needs to read what
            # tripped or it will rephrase the same thing and try again.
            return ToolResult.failure(str(exc))
        state = "pinned" if memory.pinned else "stored"
        return ToolResult.success(
            f"{state}: {memory.text}",
            memory_id=memory.id,
            kind=str(memory.kind),
            pinned=memory.pinned,
        )


class RecallParams(BaseModel):
    query: str = Field(
        default="",
        description="Words to search for. Empty returns what is most current.",
    )
    kind: MemoryKind | None = Field(
        default=None, description="Optionally restrict to one kind."
    )
    limit: int | None = Field(
        default=None, ge=1, le=25, description="Rows to return. Omit for the default."
    )


class RecallTool:
    """Search stored memories. Cheap, and the way to reach anything unpinned."""

    spec = ToolSpec(
        name="recall",
        description=(
            "Search what you remember about your operator and their work. "
            "Cheap — prefer looking something up over asking them to repeat it."
        ),
        params_model=RecallParams,
        # READ_ONLY with no permissions and no target, so the broker
        # auto-allows it in every mode including `manual`. That is intended:
        # recall reads Nomad's own store, transmits nothing, and a memory the
        # device must ask permission to consult is a memory it will not use.
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def execute(self, params: RecallParams, ctx: ToolContext) -> ToolResult:
        memories = await self._store.recall(
            params.query, kind=params.kind, limit=params.limit
        )
        if not memories:
            subject = f" about '{params.query}'" if params.query else ""
            return ToolResult.success(f"nothing remembered{subject}", count=0)
        lines = "\n".join(_preview(m) for m in memories)
        return ToolResult.success(
            f"{len(memories)} remembered:\n{lines}",
            count=len(memories),
            memory_ids=[m.id for m in memories],
        )


class ForgetParams(BaseModel):
    memory_id: str = Field(description="The id from `remember` or `recall`.")


class ForgetTool:
    """Drop a memory that has gone stale. Reversible."""

    spec = ToolSpec(
        name="forget",
        description=(
            "Forget a stored memory by id, when it has gone stale or wrong. "
            "Reversible — the record is kept, only hidden from recall."
        ),
        params_model=ForgetParams,
        # MUTATING, deliberately **not** DESTRUCTIVE. `DESTRUCTIVE` is
        # `never_auto` in every mode (D14), and prompting the operator every
        # time the device corrects one of its own stale beliefs would make it
        # feel broken — they would stop using the mode and, eventually, the
        # feature. This is a reversible soft delete that leaves an audit row,
        # so `MUTATING` is the honest label, not the convenient one.
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def execute(self, params: ForgetParams, ctx: ToolContext) -> ToolResult:
        existing = await self._store.get(params.memory_id)
        if existing is None:
            return ToolResult.failure(f"no memory with id {params.memory_id}")
        if not await self._store.forget(params.memory_id):
            return ToolResult.success(f"already forgotten: {existing.text}", changed=False)
        return ToolResult.success(f"forgot: {existing.text}", changed=True)


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    """The three memory tools, over one store."""
    return [RememberTool(store), RecallTool(store), ForgetTool(store)]
