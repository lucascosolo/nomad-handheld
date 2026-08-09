"""Backend selection, by config string, exactly like a hardware driver (D9, D24)."""

from __future__ import annotations

from pathlib import Path

from nomad.agent.backends.base import (
    AgentBackend,
    AgentEvent,
    AgentEventKind,
    BackendCapability,
)
from nomad.agent.backends.mock import MockBackend
from nomad.agent.backends.remote_llm import RemoteLlmBackend
from nomad.agent.identity import compose_identity, load_identity
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
    briefing: str = "",
    skill_index: str = "",
) -> AgentBackend:
    """Build the backend named by `[agent].backend`.

    `claude_cli` is imported lazily, inside the branch that needs it — that is
    what lets `mock` (the default) run on a machine with no `claude-agent-sdk`
    installed. Importing it at module scope would make the optional dependency
    mandatory in practice and break D9.

    `briefing` is what Nomad already knows, appended to the identity so it is
    in the prompt rather than waiting behind a tool call the model has no
    reason to make. It arrives pre-composed, as a string: this function stays
    sync and does no I/O, so composing it (which reads the store) belongs to
    the caller. A backend factory that awaits is one that cannot be called
    from a constructor.

    `skill_index` is the same shape for the same reason and one stronger one.
    D39 says the index — a name and one line per skill — is in every prompt,
    so it joins the identity here rather than waiting behind `load_skill`, a
    tool the model has no reason to call if nothing has told it skills exist.
    It arrives rendered because `agent` may not import `nomad.skills` at all
    (`tests/test_layering.py`): the composition root owns the library, this
    layer only ever sees text. That is what keeps a skill from being able to
    reach the permission path — there is no object here to reach it with.
    """
    kind = config.agent.backend

    if kind is AgentBackendKind.MOCK:
        return MockBackend()

    if kind is AgentBackendKind.REMOTE_LLM:
        return RemoteLlmBackend(config=config.agent.remote_llm)

    identity = compose_identity(
        load_identity(
            Path(config.agent.claude_cli.identity_path)
            if config.agent.claude_cli.identity_path
            else None
        ),
        briefing=briefing,
        skill_index=skill_index,
    )

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
            identity=identity,
            resume_session_id=resume_session_id,
        )

    raise AgentError(f"Unknown agent backend '{kind}'", {"backend": str(kind)})
