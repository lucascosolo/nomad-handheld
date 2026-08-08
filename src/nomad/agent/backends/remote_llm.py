"""A model on the tailnet. Planned, not implemented (D24).

This file exists as a stub on purpose. It is the reason `AgentBackend` has a
`capabilities` set at all, and keeping it visible is what stops the interface
quietly growing Claude-Code-shaped assumptions between now and the swap.

**The honest asymmetry** (D24): Claude Code brings its own loop, its own
tools, and its own compaction. A raw local LLM brings none of the three, so
this backend declares an empty capability set and Nomad has to supply all
three — a think->tool->observe loop, tool advertisement and execution against
the registry, and context compaction at `[agent].compact_at`. That is roughly
what the retired `agent/loop.py` and `agent/context.py` did, and it is why
retiring them was the right call *now* but not a permanent one; the code is
recoverable from git history when this backend is built.

Nomad holds a **tailnet** address here, never a public one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from nomad.agent.backends.base import AgentEvent, BackendCapability
from nomad.core.config import RemoteLlmConfig
from nomad.core.errors import AgentError

_UNIMPLEMENTED = (
    "The remote_llm backend is not implemented yet (D24). It must supply the loop, "
    "tool execution and compaction that Claude Code brings for itself — see "
    "docs/BUILD_LEDGER.md under 'What comes after the skeleton'."
)


class RemoteLlmBackend:
    """A local model reached over Tailscale."""

    name = "remote_llm"
    #: Empty on purpose. Nomad must supply the loop, the tools and compaction.
    capabilities: frozenset[BackendCapability] = frozenset()

    def __init__(self, *, config: RemoteLlmConfig) -> None:
        self._config = config

    async def start(self) -> None:
        raise AgentError(_UNIMPLEMENTED, {"backend": self.name, "base_url": self._config.base_url})

    async def stop(self) -> None:
        return None

    async def send(self, text: str, *, session_id: str) -> None:
        raise AgentError(_UNIMPLEMENTED, {"backend": self.name})

    async def events(self) -> AsyncIterator[AgentEvent]:
        raise AgentError(_UNIMPLEMENTED, {"backend": self.name})
        yield  # pragma: no cover - makes this an async generator

    async def interrupt(self) -> None:
        raise AgentError(_UNIMPLEMENTED, {"backend": self.name})
