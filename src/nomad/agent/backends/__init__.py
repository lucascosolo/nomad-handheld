"""Backend selection, by config string, exactly like a hardware driver (D9, D24)."""

from __future__ import annotations

from nomad.agent.backends.base import (
    AgentBackend,
    AgentEvent,
    AgentEventKind,
    BackendCapability,
)
from nomad.agent.backends.mock import MockBackend
from nomad.agent.backends.remote_llm import RemoteLlmBackend
from nomad.agent.permission_bridge import PermissionBridge
from nomad.core.config import AgentBackendKind, NomadConfig
from nomad.core.errors import AgentError
from nomad.mcp.server import McpToolRouter

__all__ = [
    "AgentBackend",
    "AgentEvent",
    "AgentEventKind",
    "BackendCapability",
    "MockBackend",
    "RemoteLlmBackend",
    "create_backend",
]


def create_backend(
    config: NomadConfig,
    *,
    bridge: PermissionBridge | None = None,
    router: McpToolRouter | None = None,
    cwd: str | None = None,
    resume_session_id: str | None = None,
) -> AgentBackend:
    """Build the backend named by `[agent].backend`.

    `claude_cli` is imported lazily, inside the branch that needs it — that is
    what lets `mock` (the default) run on a machine with no `claude-agent-sdk`
    installed. Importing it at module scope would make the optional dependency
    mandatory in practice and break D9.
    """
    kind = config.agent.backend

    if kind is AgentBackendKind.MOCK:
        return MockBackend()

    if kind is AgentBackendKind.REMOTE_LLM:
        return RemoteLlmBackend(config=config.agent.remote_llm)

    if kind is AgentBackendKind.CLAUDE_CLI:
        if bridge is None:
            # Refusing here rather than defaulting to "no gate" is the whole of
            # D21 in one branch: a backend with a full toolset and no broker in
            # front of it is exactly the thing that must not be constructible.
            raise AgentError(
                "The claude_cli backend requires a PermissionBridge; refusing to run ungated",
                {"backend": str(kind)},
            )
        from nomad.agent.backends.claude_cli import ClaudeCliBackend

        return ClaudeCliBackend(
            cli_config=config.agent.claude_cli,
            bridge=bridge,
            router=router,
            cwd=cwd,
            resume_session_id=resume_session_id,
        )

    raise AgentError(f"Unknown agent backend '{kind}'", {"backend": str(kind)})
