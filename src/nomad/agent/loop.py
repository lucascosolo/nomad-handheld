"""The turn state machine (D11): think -> tool_calls -> await grants -> observe.

Two properties are load-bearing:

* **The turn row is persisted before anything executes.** A power cut mid-turn
  therefore leaves a durable record of what was in flight instead of a
  half-applied edit with no trace.
* **A tool call that needs authorization parks the turn; it does not fail it.**
  The loop sets the turn to `awaiting_grant`, waits, and resumes. An
  unanswered prompt times out into an auto-**deny**, which the model observes
  as a failed tool result and can react to — the turn still finishes.

The tool-call budget (`agent.max_tool_calls_per_turn`) parks too, rather than
crashing: the turn stops in `awaiting_grant` with a `PARKED` outcome so an
operator can decide whether to continue.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from nomad.agent.context import COMPACTION_ROLE, ContextManager, Summarizer
from nomad.agent.provider import (
    AIProvider,
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    StopReason,
)
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import NomadError
from nomad.core.events import Event, EventBus
from nomad.core.logging import get_logger
from nomad.storage.repositories.conversations import ConversationsRepository, Turn
from nomad.tools.permissions import (
    DEFAULT_AUTHORIZATION_TIMEOUT,
    AuthorizationQueue,
    DecisionOutcome,
    PermissionBroker,
    Resolution,
    ToolExecutor,
    ToolRequest,
)
from nomad.tools.registry import ToolRegistry

logger = get_logger(__name__)

EVENT_TURN_STARTED = "agent.turn_started"
EVENT_TURN_FINISHED = "agent.turn_finished"
EVENT_TURN_PARKED = "agent.turn_parked"

HISTORY_LIMIT = 500

DEFAULT_SYSTEM_PROMPT = (
    "You are Nomad, a persistent coding agent running on a handheld device. "
    "You act through declared tools on named targets. Tools that need "
    "authorization will pause until a human answers; a denial is information, "
    "not a failure — explain what you wanted to do and why."
)


class TurnOutcomeStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"
    PARKED = "parked"


class TurnOutcome(BaseModel):
    turn_id: str
    status: TurnOutcomeStatus
    text: str = ""
    tool_calls: int = 0
    reason: str | None = None


class TurnLoop:
    """Drives one turn at a time against a provider and the permission pipeline."""

    def __init__(
        self,
        *,
        provider: AIProvider,
        broker: PermissionBroker,
        executor: ToolExecutor,
        queue: AuthorizationQueue,
        tools: ToolRegistry,
        conversations: ConversationsRepository,
        context: ContextManager,
        bus: EventBus,
        config: NomadConfig,
        session_id: str,
        mode_provider: Any,
        summarizer: Summarizer | None = None,
        authorization_timeout: float = DEFAULT_AUTHORIZATION_TIMEOUT,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self._provider = provider
        self._broker = broker
        self._executor = executor
        self._queue = queue
        self._tools = tools
        self._conversations = conversations
        self._context = context
        self._bus = bus
        self._config = config
        self._session_id = session_id
        self._mode_provider = mode_provider
        self._summarizer = summarizer
        self._authorization_timeout = authorization_timeout
        self._system_prompt = system_prompt

    # -- entry points -----------------------------------------------------

    async def run_turn(self, user_text: str) -> TurnOutcome:
        """Persist the turn, then drive it (D11 — persist before execute)."""
        turn = await self._conversations.create_turn(
            session_id=self._session_id, status="pending"
        )
        await self._conversations.add_message(
            turn_id=turn.id,
            session_id=self._session_id,
            role=MessageRole.USER.value,
            content={"text": user_text},
        )
        return await self._drive(turn)

    async def resume_turn(self, turn: Turn) -> TurnOutcome:
        """Re-drive a turn that was persisted but never executed."""
        return await self._drive(turn)

    # -- the machine ------------------------------------------------------

    async def _drive(self, turn: Turn) -> TurnOutcome:
        await self._conversations.update_turn_status(turn.id, "running", set_started=True)
        await self._publish(
            EVENT_TURN_STARTED,
            {"turn_id": turn.id, "session_id": self._session_id},
        )

        messages = await self._load_history()
        tool_calls_used = 0
        budget = self._config.agent.max_tool_calls_per_turn
        final_text = ""

        while True:
            if self._context.should_compact(messages):
                messages, _ = await self._context.compact(
                    messages,
                    session_id=self._session_id,
                    turn_id=turn.id,
                    summarizer=self._summarizer,
                )

            response = await self._think(messages)
            if response.stop_reason is StopReason.ERROR:
                return await self._finish(
                    turn, TurnOutcomeStatus.FAILED, final_text, tool_calls_used, response.error
                )

            if response.usage.get("input_tokens"):
                self._context.observe_usage(messages, response.usage["input_tokens"])

            if response.text:
                final_text = response.text
                assistant = ProviderMessage(role=MessageRole.ASSISTANT, content=response.text)
                messages.append(assistant)
                await self._persist(turn.id, assistant)

            if not response.tool_calls:
                return await self._finish(
                    turn, TurnOutcomeStatus.COMPLETE, final_text, tool_calls_used, None
                )

            for call in response.tool_calls:
                if tool_calls_used >= budget:
                    return await self._park(
                        turn,
                        final_text,
                        tool_calls_used,
                        f"tool call budget of {budget} reached",
                    )
                tool_calls_used += 1
                observation = await self._invoke(call, turn)
                messages.append(observation)
                await self._persist(turn.id, observation)

    async def _think(self, messages: list[ProviderMessage]) -> ProviderResponse:
        request = ProviderRequest(
            messages=messages,
            tools=self._tools.model_schemas(),
            system=self._system_prompt,
        )
        try:
            return await self._provider.complete(request)
        except NomadError as exc:
            logger.error("Provider failed", extra={"error": exc.message})
            return ProviderResponse(stop_reason=StopReason.ERROR, error=exc.message)
        except Exception as exc:  # noqa: BLE001 - a provider bug must not kill the session
            logger.error("Provider raised unexpectedly", extra={"error": str(exc)})
            return ProviderResponse(
                stop_reason=StopReason.ERROR, error=f"{type(exc).__name__}: {exc}"
            )

    async def _invoke(self, call: ProviderToolCall, turn: Turn) -> ProviderMessage:
        """One trip through the D4 pipeline, always ending in an observation."""
        request = ToolRequest(
            tool=call.tool,
            target_id=call.target_id,
            params=call.params,
            session_id=self._session_id,
            turn_id=turn.id,
            call_id=call.id,
        )
        mode: PermissionMode = self._mode_provider()
        decision = await self._broker.decide(request, mode)

        if decision.outcome is DecisionOutcome.DENY:
            return self._observation(call, f"DENIED: {decision.reason}")

        if decision.outcome is DecisionOutcome.NEEDS_AUTH:
            tool = self._tools.get(request.tool)
            await self._conversations.update_turn_status(turn.id, "awaiting_grant")
            resolution, grant = await self._queue.request(
                request,
                decision,
                spec=tool.spec,
                timeout=self._authorization_timeout,
            )
            await self._conversations.update_turn_status(turn.id, "running")
            if resolution is not Resolution.APPROVED or grant is None:
                return self._observation(
                    call, f"NOT AUTHORIZED ({resolution}): {decision.reason}"
                )
        else:
            try:
                grant = await self._broker.authorize(request, decision)
            except NomadError as exc:
                return self._observation(call, f"DENIED: {exc.message}")

        try:
            result = await self._executor.run(grant, request)
        except NomadError as exc:
            return self._observation(call, f"DENIED: {exc.message}")

        body = result.content if result.ok else f"ERROR: {result.error}\n{result.content}".strip()
        return self._observation(call, body)

    def _observation(self, call: ProviderToolCall, content: str) -> ProviderMessage:
        return ProviderMessage(
            role=MessageRole.TOOL, content=content, tool_call_id=call.id, name=call.tool
        )

    # -- persistence helpers ----------------------------------------------

    async def _persist(self, turn_id: str, message: ProviderMessage) -> None:
        await self._conversations.add_message(
            turn_id=turn_id,
            session_id=self._session_id,
            role=message.role.value,
            content=message.to_storage(),
        )

    async def _load_history(self) -> list[ProviderMessage]:
        """Rebuild the conversation, honouring the most recent compaction (D16).

        Everything before the last compaction record is represented by that
        record's summary, so a compacted session stays compacted across
        restarts instead of silently re-inflating.
        """
        rows = await self._conversations.get_messages_for_session(
            self._session_id, limit=HISTORY_LIMIT
        )
        last_compaction = None
        for index, row in enumerate(rows):
            if row.role == COMPACTION_ROLE:
                last_compaction = index

        messages: list[ProviderMessage] = []
        start = 0
        if last_compaction is not None:
            summary = str(rows[last_compaction].content.get("summary", ""))
            messages.append(
                ProviderMessage(
                    role=MessageRole.SYSTEM,
                    content=f"Summary of earlier conversation:\n{summary}",
                )
            )
            start = last_compaction + 1

        for row in rows[start:]:
            if row.role == COMPACTION_ROLE:
                continue
            try:
                messages.append(ProviderMessage.from_storage(row.role, row.content))
            except ValueError:  # pragma: no cover - unknown role written by another chunk
                logger.warning("Skipping message with unknown role", extra={"role": row.role})
        return messages

    async def _finish(
        self,
        turn: Turn,
        status: TurnOutcomeStatus,
        text: str,
        tool_calls: int,
        reason: str | None,
    ) -> TurnOutcome:
        await self._conversations.update_turn_status(turn.id, status.value)  # type: ignore[arg-type]
        await self._publish(
            EVENT_TURN_FINISHED,
            {
                "turn_id": turn.id,
                "session_id": self._session_id,
                "status": str(status),
                "tool_calls": tool_calls,
                "reason": reason,
            },
        )
        return TurnOutcome(
            turn_id=turn.id, status=status, text=text, tool_calls=tool_calls, reason=reason
        )

    async def _park(
        self, turn: Turn, text: str, tool_calls: int, reason: str
    ) -> TurnOutcome:
        await self._conversations.update_turn_status(turn.id, "awaiting_grant")
        await self._publish(
            EVENT_TURN_PARKED,
            {
                "turn_id": turn.id,
                "session_id": self._session_id,
                "reason": reason,
                "tool_calls": tool_calls,
            },
        )
        logger.info("Turn parked", extra={"turn_id": turn.id, "reason": reason})
        return TurnOutcome(
            turn_id=turn.id,
            status=TurnOutcomeStatus.PARKED,
            text=text,
            tool_calls=tool_calls,
            reason=reason,
        )

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(type=event_type, source="agent_loop", payload=payload))
