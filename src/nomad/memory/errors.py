"""Memory's one error: a refusal to store something.

Derives from `NomadError` like everything else. It is raised rather than
returned so that `MemoryStore.remember` can keep an honest `-> Memory`
signature — a caller either gets the memory or is told, loudly, why not. The
MCP tool catches it and turns it into a `ToolResult.failure`, so what the
model sees is a readable sentence and not a stack trace.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class MemoryRefused(NomadError):
    """Nomad declined to store this. The message says why."""
