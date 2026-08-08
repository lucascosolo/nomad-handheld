"""`can_use_tool` -> Nomad's `PermissionBroker` (D21).

Every tool call the backend proposes arrives here before it runs. This is the
only thing between the model and the device, and D21 states the consequence
plainly: the workspace root stopped being a hard wall and became a policy
line. There is no sandbox underneath this. If the bridge is wrong, nothing
catches it.

So the whole module is built around one rule: **anything that is not an
explicit approval is a denial.** An unknown tool, an unparseable argument, a
broker exception, a timeout, a target that is not registered, a human who
never answers — all deny. There is no path through this function that reaches
"allow" by falling off the end.

**No SDK type appears here.** The bridge speaks Nomad's `BridgeDecision`, and
`agent/backends/claude_cli.py` translates it into whatever the SDK wants. That
keeps the security-critical logic testable with no `claude-agent-sdk`
installed, which in turn means it is tested on every laptop and in CI rather
than only where credentials exist (D24).

**Grants and execution.** For a tool the backend runs itself (`Bash`, `Read`,
`Write`), Nomad mints and persists a grant purely as the audit record — D4's
promise that nothing runs without one holds because the approval *is* the
grant. For a tool Nomad runs (its MCP hardware tools), the minted grant is
stashed in the `GrantVault` and the MCP handler must pop it before executing.
A handler that finds no grant refuses. That is what keeps "`ToolExecutor.run()`
takes a grant, never a request" true across a process boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nomad.core.config import PermissionMode
from nomad.core.errors import NomadError
from nomad.core.events import Event, EventBus
from nomad.core.logging import get_logger
from nomad.targets.base import Capability
from nomad.tools.base import ToolSpec
from nomad.tools.permissions import (
    AuthorizationGrant,
    AuthorizationQueue,
    Decision,
    DecisionOutcome,
    GrantVault,
    PermissionBroker,
    Resolution,
    ToolRequest,
)
from nomad.tools.registry import ToolRegistry

logger = get_logger(__name__)

EVENT_BRIDGE_DENIED = "agent.bridge_denied"

#: Claude Code namespaces MCP tools as `mcp__<server>__<tool>`. Nomad's
#: in-process server is registered under this name (see `nomad.mcp.server`).
MCP_SERVER_NAME = "nomad"
_MCP_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

#: Hard ceiling on one bridge decision, human approval included. A pocket
#: device that hangs forever on an unanswered prompt is a bricked device; a
#: prompt that expires into a denial is merely a device that did nothing.
DEFAULT_BRIDGE_TIMEOUT = 300.0


@dataclass(frozen=True)
class BridgeDecision:
    """Nomad's answer, in Nomad's vocabulary. The SDK adapter translates it."""

    allow: bool
    reason: str
    tool: str
    #: Set only for an allow, and only for tools Nomad itself will execute.
    grant_id: str | None = None

    @classmethod
    def deny(cls, tool: str, reason: str) -> BridgeDecision:
        return cls(allow=False, reason=reason, tool=tool)


class PermissionBridge:
    """Turns a backend's proposed tool call into an allow or a deny (D21)."""

    def __init__(
        self,
        *,
        broker: PermissionBroker,
        queue: AuthorizationQueue,
        tools: ToolRegistry,
        bus: EventBus,
        session_id_provider: Callable[[], str],
        mode_provider: Callable[[], PermissionMode],
        turn_id_provider: Callable[[], str | None] = lambda: None,
        vault: GrantVault | None = None,
        target_id: str = "local",
        hid_target_id: str = "hid",
        timeout: float = DEFAULT_BRIDGE_TIMEOUT,
    ) -> None:
        self._broker = broker
        self._queue = queue
        self._tools = tools
        self._bus = bus
        self._session_id = session_id_provider
        self._mode = mode_provider
        self._turn_id = turn_id_provider
        # `is None`, never `or`: `GrantVault` defines `__len__`, so an empty
        # vault is falsy and `vault or GrantVault()` would silently substitute
        # a fresh one — detaching the bridge from the router that reads it, and
        # turning every MCP call into "no authorization on record".
        self._vault = GrantVault() if vault is None else vault
        self._target_id = target_id
        self._hid_target_id = hid_target_id
        self._timeout = timeout

    @property
    def vault(self) -> GrantVault:
        return self._vault

    @staticmethod
    def nomad_tool_name(tool_name: str) -> str:
        """Strip the MCP namespace so Nomad's own tools match the registry.

        `mcp__nomad__display_text` is Nomad's `display_text`. A tool from any
        *other* MCP server keeps its full prefixed name, finds no spec, and is
        denied as unknown — which is the correct answer for a server Nomad did
        not configure.
        """
        if tool_name.startswith(_MCP_PREFIX):
            return tool_name[len(_MCP_PREFIX) :]
        return tool_name

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any] | None, context: Any = None
    ) -> BridgeDecision:
        """The gate. Returns a denial for every failure mode, never raises."""
        try:
            return await asyncio.wait_for(
                self._decide(tool_name, tool_input), timeout=self._timeout
            )
        except TimeoutError:
            return await self._denied(tool_name, "permission decision timed out")
        except asyncio.CancelledError:  # pragma: no cover - propagate shutdown
            raise
        except NomadError as exc:
            return await self._denied(tool_name, f"permission error: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - a broken bridge must deny, not crash
            logger.error(
                "Permission bridge raised; denying",
                extra={"tool": tool_name, "error": f"{type(exc).__name__}: {exc}"},
            )
            return await self._denied(tool_name, f"permission error: {type(exc).__name__}")

    async def _decide(self, tool_name: str, tool_input: dict[str, Any] | None) -> BridgeDecision:
        name = self.nomad_tool_name(tool_name)

        if not isinstance(tool_input, dict):
            # A non-dict payload cannot be classified, so it cannot be scoped,
            # so it cannot be safely allowed. Deny rather than coerce.
            if tool_input is not None:
                return await self._denied(name, "tool input was not an object")
            tool_input = {}

        tool = self._tools.try_get(name)
        if tool is None:
            # The fail-closed default D21 requires: a tool nobody has
            # classified in `claude_tools.py` does not run, even in `auto`.
            return await self._denied(
                name, f"'{name}' has no permission declaration in Nomad; refusing"
            )

        request = ToolRequest(
            tool=name,
            target_id=self._target_for(tool.spec),
            params=dict(tool_input),
            session_id=self._session_id(),
            turn_id=self._turn_id(),
        )

        mode = self._mode()
        decision = await self._broker.decide(request, mode)

        if decision.outcome is DecisionOutcome.DENY:
            return await self._denied(name, decision.reason)

        if decision.outcome is DecisionOutcome.NEEDS_AUTH:
            return await self._escalate(request, decision, mode)

        grant = await self._broker.authorize(request, decision)
        return self._allow(request, grant, decision.reason)

    def _target_for(self, spec: ToolSpec) -> str:
        """Which target this call lands on (D12).

        Derived from the spec's declared capabilities rather than from the
        tool's name, so a HID tool cannot be routed at the local machine by
        naming it something innocuous. If no HID target is registered the id
        simply does not resolve and the broker denies — the right answer for a
        device with nothing plugged into it.
        """
        if Capability.HID_OUTPUT in spec.required_capabilities:
            return self._hid_target_id
        return self._target_id

    async def _escalate(
        self, request: ToolRequest, decision: Decision, mode: PermissionMode
    ) -> BridgeDecision:
        """Park on the touchscreen. Every non-approval outcome denies."""
        spec = self._tools.get(request.tool).spec
        resolution, grant = await self._queue.request(request, decision, spec=spec)
        if resolution is Resolution.APPROVED and grant is not None:
            return self._allow(request, grant, f"approved by operator ({decision.reason})")
        return await self._denied(request.tool, f"{resolution}: {decision.reason}")

    def _allow(
        self, request: ToolRequest, grant: AuthorizationGrant, reason: str
    ) -> BridgeDecision:
        # Stash unconditionally. Whether Nomad or the backend executes the call
        # is not something the bridge should have to know, and an unclaimed
        # grant simply expires — the cheap direction to be wrong in.
        self._vault.stash(grant, request)
        return BridgeDecision(allow=True, reason=reason, tool=request.tool, grant_id=grant.id)

    async def _denied(self, tool: str, reason: str) -> BridgeDecision:
        logger.warning("Bridge denied a tool call", extra={"tool": tool, "reason": reason})
        await self._bus.publish(
            Event(
                type=EVENT_BRIDGE_DENIED,
                source="permission_bridge",
                payload={"tool": tool, "reason": reason, "session_id": self._session_id()},
            )
        )
        return BridgeDecision.deny(tool, reason)
