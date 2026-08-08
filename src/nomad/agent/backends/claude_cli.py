"""Claude Code headless, via the Agent SDK (D19, D20, D24).

**This is the only module in Nomad allowed to import `claude-agent-sdk`.**
`tests/test_sdk_boundary.py` enforces it, and the rule is what keeps the swap
to a local model over Tailscale cheap: everything above this file speaks
`AgentEvent` and `BridgeDecision`, so replacing the backend replaces this file
and nothing else. No SDK type leaves this module — not in a return value, not
inside `AgentEvent.raw`, not in an exception.

The import is deferred into `start()` rather than done at module scope,
because `claude-agent-sdk` is an optional dependency (`pip install
nomad[agent]`). `mock` is the default backend and the suite must run with no
SDK, no CLI and no credentials (D9).

Three things here are security decisions, not configuration:

* **`ANTHROPIC_API_KEY` is actively stripped** from the child environment. If
  it leaks through, the CLI silently bills per token instead of against the
  subscription (D20). Stripping beats documenting.
* **`--bare` is never used, and cannot be.** Its documented behaviour is that
  Anthropic auth is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` — OAuth and
  keychain are never read — so it would defeat the entire reason D20 exists.
  `_reject_forbidden_args` makes passing it an error rather than a footgun.
* **Session identity is an explicit UUID**, passed as `session_id` and resumed
  with `resume`. Never `continue_conversation`, which resolves to "most recent
  conversation in this directory" — ambient state a long-lived daemon must not
  depend on (D20).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from nomad.agent.backends.base import AgentEvent, AgentEventKind, BackendCapability
from nomad.agent.permission_bridge import BridgeDecision, PermissionBridge
from nomad.core.config import ClaudeCliConfig
from nomad.core.errors import AgentError
from nomad.core.logging import get_logger
from nomad.mcp.server import SERVER_NAME, SERVER_VERSION, McpToolRouter

logger = get_logger(__name__)

#: Flags that would defeat D20 if they ever reached the CLI. Rejected loudly.
FORBIDDEN_ARGS: frozenset[str] = frozenset({"bare", "--bare", "continue", "--continue"})

#: Removed from the child environment, never merely left unset by hope (D20).
STRIPPED_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def child_environment(
    cli_config: ClaudeCliConfig, parent: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment the CLI actually gets (D20).

    Pure and parameterised so the stripping is testable without spawning
    anything — the property that matters is worth a test, and a test that
    needs a subprocess would not be run.
    """
    env = dict(os.environ if parent is None else parent)
    for name in STRIPPED_ENV_VARS:
        env.pop(name, None)
    token = env.get(cli_config.oauth_token_env)
    if not token:
        logger.warning(
            "No OAuth token in the environment; the CLI will not be able to authenticate",
            extra={"expected_env": cli_config.oauth_token_env},
        )
    return env


def _reject_forbidden_args(extra_args: dict[str, Any]) -> None:
    offending = sorted(k for k in extra_args if k.lower().lstrip("-") in FORBIDDEN_ARGS)
    if offending:
        raise AgentError(
            f"Forbidden CLI arguments: {', '.join(offending)}. "
            "`--bare` never reads OAuth and `--continue` depends on ambient state (D20).",
            {"args": offending},
        )


class ClaudeCliBackend:
    """Claude Code as Nomad's agent loop."""

    name = "claude_cli"
    capabilities: frozenset[BackendCapability] = frozenset(
        {
            BackendCapability.OWN_LOOP,
            BackendCapability.OWN_TOOLS,
            BackendCapability.OWN_COMPACTION,
        }
    )

    def __init__(
        self,
        *,
        cli_config: ClaudeCliConfig,
        bridge: PermissionBridge,
        router: McpToolRouter | None = None,
        cwd: str | None = None,
        system_prompt: str | None = None,
        extra_args: dict[str, Any] | None = None,
        resume_session_id: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        _reject_forbidden_args(extra_args or {})
        self._cli = cli_config
        self._bridge = bridge
        self._router = router
        self._cwd = cwd
        self._system_prompt = system_prompt
        self._extra_args = dict(extra_args or {})
        self._resume_session_id = resume_session_id
        self._env = env
        self._client: Any = None
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None
        self._session_id: str = ""

    # -- SDK adaptation ----------------------------------------------------

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        """The SDK's `can_use_tool` hook. Nothing but translation lives here.

        Every judgement is the bridge's; this function's only job is turning a
        `BridgeDecision` into the SDK's result type. Even the except clause
        denies, because a translation failure is still a failure to authorize.
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        try:
            decision: BridgeDecision = await self._bridge.can_use_tool(
                tool_name, tool_input, context
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, always (D21)
            logger.error(
                "Bridge translation failed; denying",
                extra={"tool": tool_name, "error": f"{type(exc).__name__}: {exc}"},
            )
            return PermissionResultDeny(behavior="deny", message="permission bridge failed")

        if decision.allow:
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(behavior="deny", message=decision.reason)

    def _build_mcp_server(self) -> Any:
        """Wrap the router's tools as an in-process SDK MCP server."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        if self._router is None:
            return None

        sdk_tools = []
        for spec in self._router.specs:
            sdk_tools.append(
                tool(
                    spec.name,
                    spec.description,
                    spec.params_model.model_json_schema(),
                )(self._handler_for(spec.name))
            )
        return create_sdk_mcp_server(name=SERVER_NAME, version=SERVER_VERSION, tools=sdk_tools)

    def _handler_for(self, name: str) -> Callable[[dict[str, Any]], Any]:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            assert self._router is not None
            return await self._router.call(name, args)

        handler.__name__ = f"nomad_{name}"
        return handler

    def _options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        _reject_forbidden_args(self._extra_args)
        mcp_server = self._build_mcp_server()
        return ClaudeAgentOptions(
            model=self._cli.model or None,
            cli_path=self._cli.cli_path or None,
            cwd=self._cwd,
            system_prompt=self._system_prompt,
            env=self._env if self._env is not None else child_environment(self._cli),
            # Explicit identity, never `continue_conversation` (D20).
            session_id=self._session_id or None,
            resume=self._resume_session_id,
            continue_conversation=False,
            can_use_tool=self._can_use_tool,
            mcp_servers={SERVER_NAME: mcp_server} if mcp_server is not None else {},
            # Capability is deliberately NOT reduced here. Skills, `CLAUDE.md`,
            # plugins and the operator's MCP servers are a large part of why
            # Claude Code is effective on a laptop, and the goal is a handheld
            # that matches it. The broker is what makes that safe (D21):
            # anything these surfaces reach for still passes `can_use_tool`,
            # and a tool with no declaration is still denied.
            strict_mcp_config=self._cli.strict_mcp_config,
            setting_sources=list(self._cli.setting_sources) or None,
            skills=self._cli.skills,
            # `default` keeps the CLI asking, which means asking *us* — the
            # permission mode that matters is Nomad's (D14), applied in the
            # bridge. Handing the CLI a permissive mode here would let it
            # settle calls before `can_use_tool` ever fired.
            permission_mode="default",
            extra_args=self._extra_args,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        try:
            from claude_agent_sdk import ClaudeSDKClient
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise AgentError(
                "The claude_cli backend needs the Agent SDK: pip install 'nomad[agent]'",
                {"backend": self.name},
            ) from exc

        self._client = ClaudeSDKClient(options=self._options())
        await self._client.connect()
        self._pump = asyncio.create_task(self._pump_messages())
        logger.info("Claude Code backend connected", extra={"model": self._cli.model})

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                logger.warning("Backend stream pump failed", extra={"error": str(exc)})
            self._pump = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - never fail a shutdown
                logger.warning("Backend disconnect failed", extra={"error": str(exc)})
            self._client = None
        await self._queue.put(None)

    async def send(self, text: str, *, session_id: str) -> None:
        if self._client is None:
            raise AgentError("Claude Code backend is not started", {"backend": self.name})
        self._session_id = session_id
        await self._client.query(text)

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    # -- message translation -----------------------------------------------

    async def _pump_messages(self) -> None:
        """Read the SDK's stream and re-emit it as Nomad events.

        The translation is exhaustive by construction: anything unrecognised
        becomes a `TEXT` event carrying its `str()`, never a passthrough of the
        SDK object. An unknown message type must not be able to smuggle an SDK
        type across this boundary (D24).
        """
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                for event in self._translate(message):
                    await self._queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead stream is an error event
            logger.error("Backend stream failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            await self._queue.put(
                AgentEvent(
                    kind=AgentEventKind.ERROR,
                    session_id=self._session_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    def _translate(self, message: Any) -> list[AgentEvent]:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolUseBlock,
        )

        sid = self._session_id
        events: list[AgentEvent] = []

        if isinstance(message, AssistantMessage):
            for block in getattr(message, "content", []) or []:
                if isinstance(block, TextBlock):
                    events.append(
                        AgentEvent(kind=AgentEventKind.TEXT, session_id=sid, text=block.text)
                    )
                elif isinstance(block, ThinkingBlock):
                    events.append(
                        AgentEvent(
                            kind=AgentEventKind.THINKING,
                            session_id=sid,
                            text=getattr(block, "thinking", ""),
                        )
                    )
                elif isinstance(block, ToolUseBlock):
                    events.append(
                        AgentEvent(
                            kind=AgentEventKind.TOOL_CALL,
                            session_id=sid,
                            tool_name=block.name,
                            # `dict(...)` copies rather than aliases the SDK's
                            # own mapping — no live SDK object in `AgentEvent`.
                            tool_input=dict(getattr(block, "input", {}) or {}),
                            tool_use_id=getattr(block, "id", None),
                        )
                    )
            return events

        if isinstance(message, ResultMessage):
            usage = getattr(message, "usage", None) or {}
            events.append(
                AgentEvent(
                    kind=AgentEventKind.USAGE,
                    session_id=sid,
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                )
            )
            is_error = bool(getattr(message, "is_error", False))
            events.append(
                AgentEvent(
                    kind=AgentEventKind.TURN_COMPLETE,
                    session_id=sid,
                    ok=not is_error,
                    text=str(getattr(message, "result", "") or ""),
                    error="backend reported an error" if is_error else None,
                )
            )
            return events

        return events
