"""The persistent session and the swappable agent backend (D11, D19, D24).

`loop.py`, `context.py` and `provider.py` lived here until D19. Claude Code
does the loop and the compaction better, so they were retired rather than
maintained in parallel — see `agent/backends/remote_llm.py` for the one case
that will need them back, and git history for the code itself.
"""

from nomad.agent.backends import (
    AgentBackend,
    AgentEvent,
    AgentEventKind,
    BackendCapability,
    MockBackend,
    RemoteLlmBackend,
    create_backend,
)
from nomad.agent.claude_tools import (
    CLAUDE_CODE_TOOLS,
    ForeignParams,
    ForeignTool,
    register_backend_tools,
)
from nomad.agent.permission_bridge import (
    MCP_SERVER_NAME,
    BridgeDecision,
    PermissionBridge,
)
from nomad.agent.session import (
    AgentSession,
    ResumeReport,
    TurnOutcome,
    TurnOutcomeStatus,
)

__all__ = [
    "CLAUDE_CODE_TOOLS",
    "MCP_SERVER_NAME",
    "AgentBackend",
    "AgentEvent",
    "AgentEventKind",
    "AgentSession",
    "BackendCapability",
    "BridgeDecision",
    "ForeignParams",
    "ForeignTool",
    "MockBackend",
    "PermissionBridge",
    "RemoteLlmBackend",
    "ResumeReport",
    "TurnOutcome",
    "TurnOutcomeStatus",
    "create_backend",
    "register_backend_tools",
]
