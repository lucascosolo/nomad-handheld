"""`AgentSession` — the persistent session (D11).

Nomad is a session, not a request/response service. `AgentSession` is a
long-lived `Component` started at boot and running until shutdown; HTTP,
WebSocket and the ESP32 display are *views* onto it. Closing the screen does
not end the conversation.

The session owns three things a view must never own:

* **conversation state**, including which turns are in flight;
* **the permission mode** (D14) — switchable at runtime, persisted, so a
  reboot does not silently restore a more permissive setting than the last
  one chosen;
* **the pending-authorization queue**, so a prompt raised while the screen was
  off is still waiting when it comes back.

At boot, `resume()` looks for turns left non-terminal by a crash or power cut
and either re-drives them or aborts them cleanly, recording which. It never
leaves one in limbo.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from nomad.agent.context import ContextManager, Summarizer
from nomad.agent.loop import TurnLoop, TurnOutcome
from nomad.agent.provider import AIProvider
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.registry import TargetRegistry
from nomad.tools.permissions import (
    DEFAULT_AUTHORIZATION_TIMEOUT,
    AuthorizationGrant,
    AuthorizationQueue,
    Classifier,
    PendingAuthorization,
    PermissionBroker,
    ToolExecutor,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

logger = get_logger(__name__)

EVENT_MODE_CHANGED = "agent.mode_changed"
EVENT_RESUMED = "agent.resumed"

_NON_RESUMABLE = ("running", "awaiting_grant")


class ResumeReport(BaseModel):
    """What boot recovery actually did. Recorded, never guessed at later."""

    resumed: list[str] = []
    aborted: list[str] = []
    expired_authorizations: int = 0


class AgentSession:
    """The long-lived session component."""

    name = "agent_session"

    def __init__(
        self,
        *,
        config: NomadConfig,
        bus: EventBus,
        conversations: ConversationsRepository,
        grants: GrantsRepository,
        targets: TargetRegistry,
        tools: ToolRegistry,
        workspace: Workspace,
        provider: AIProvider,
        classifier: Classifier | None = None,
        summarizer: Summarizer | None = None,
        context: ContextManager | None = None,
        session_id: str | None = None,
        resume_pending: bool = True,
        authorization_timeout: float = DEFAULT_AUTHORIZATION_TIMEOUT,
    ) -> None:
        self._config = config
        self._bus = bus
        self._conversations = conversations
        self._grants = grants
        self._targets = targets
        self._tools = tools
        self._workspace = workspace
        self._provider = provider
        self._session_id = session_id
        self._resume_pending = resume_pending
        self._authorization_timeout = authorization_timeout
        self._summarizer = summarizer

        self._mode = config.agent.mode
        self._state = ComponentState.NEW
        self._turn_lock = asyncio.Lock()
        self._current_turn_task: asyncio.Task[Any] | None = None
        self._resume_report: ResumeReport | None = None

        self._broker = PermissionBroker(
            tools=tools,
            targets=targets,
            workspace=workspace,
            grants=grants,
            bus=bus,
            config=config,
            classifier=classifier,
        )
        self._executor = ToolExecutor(
            tools=tools,
            targets=targets,
            workspace=workspace,
            grants=grants,
            bus=bus,
            config=config,
        )
        self._queue = AuthorizationQueue(
            broker=self._broker,
            grants=grants,
            bus=bus,
            default_timeout=authorization_timeout,
        )
        self._context = context or ContextManager(
            conversations=conversations,
            bus=bus,
            compact_at=config.agent.compact_at,
        )
        self._loop: TurnLoop | None = None

    # -- accessors ---------------------------------------------------------

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("AgentSession has not been started")
        return self._session_id

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @property
    def broker(self) -> PermissionBroker:
        return self._broker

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    @property
    def queue(self) -> AuthorizationQueue:
        return self._queue

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def last_resume_report(self) -> ResumeReport | None:
        """What boot recovery did, for a view that reconnects and asks."""
        return self._resume_report

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._workspace.ensure_exists()

        existing = (
            await self._conversations.get_session(self._session_id)
            if self._session_id
            else None
        )
        if existing is None:
            session = await self._conversations.create_session(
                mode=str(self._mode), session_id=self._session_id
            )
            self._session_id = session.id
        else:
            self._session_id = existing.id
            # A persisted mode is the operator's last explicit choice; it
            # outranks the config default on restart (D14).
            self._mode = PermissionMode(existing.mode)

        self._loop = TurnLoop(
            provider=self._provider,
            broker=self._broker,
            executor=self._executor,
            queue=self._queue,
            tools=self._tools,
            conversations=self._conversations,
            context=self._context,
            bus=self._bus,
            config=self._config,
            session_id=self._session_id,
            mode_provider=lambda: self._mode,
            summarizer=self._summarizer,
            authorization_timeout=self._authorization_timeout,
        )

        self._resume_report = await self.resume()
        self._state = ComponentState.STARTED
        logger.info(
            "Agent session started",
            extra={"session_id": self._session_id, "mode": str(self._mode)},
        )

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        task = self._current_turn_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                logger.warning("In-flight turn failed during shutdown", extra={"error": str(exc)})
        # Anything still non-terminal is aborted rather than left in limbo.
        for turn in await self._conversations.find_incomplete_turns():
            if turn.session_id == self._session_id:
                await self._conversations.update_turn_status(turn.id, "aborted")
        self._state = ComponentState.STOPPED

    async def resume(self) -> ResumeReport:
        """Recover turns left non-terminal by a crash or power cut (D11).

        A turn that never began executing (`pending`) still has its user
        message on disk and can honestly be re-driven. A turn that was
        `running` or `awaiting_grant` may have applied side effects we cannot
        reconstruct, so it is aborted — a clean, recorded abort beats an
        optimistic replay that runs a tool twice.
        """
        report = ResumeReport(expired_authorizations=await self._queue.expire_all(self.session_id))

        for turn in await self._conversations.find_incomplete_turns():
            if turn.session_id != self.session_id:
                continue
            if turn.status in _NON_RESUMABLE:
                await self._conversations.update_turn_status(turn.id, "aborted")
                report.aborted.append(turn.id)
                continue
            messages = await self._conversations.get_messages_for_turn(turn.id)
            if not any(m.role == "user" for m in messages):
                await self._conversations.update_turn_status(turn.id, "aborted")
                report.aborted.append(turn.id)
                continue
            report.resumed.append(turn.id)

        await self._bus.publish(
            Event(
                type=EVENT_RESUMED,
                source="agent_session",
                payload={
                    "session_id": self.session_id,
                    "resumed": report.resumed,
                    "aborted": report.aborted,
                    "expired_authorizations": report.expired_authorizations,
                },
            )
        )
        logger.info(
            "Session recovery complete",
            extra={
                "session_id": self.session_id,
                "resumed": len(report.resumed),
                "aborted": len(report.aborted),
            },
        )

        if self._resume_pending:
            for turn_id in list(report.resumed):
                turn = await self._conversations.get_turn(turn_id)
                if turn is not None and self._loop is not None:
                    await self._loop.resume_turn(turn)
        return report

    # -- conversation ------------------------------------------------------

    async def send(self, text: str) -> TurnOutcome:
        """Run one turn. Serialized: one turn at a time per session."""
        if self._loop is None:
            raise RuntimeError("AgentSession has not been started")
        async with self._turn_lock:
            task = asyncio.ensure_future(self._loop.run_turn(text))
            self._current_turn_task = task
            try:
                return await task
            finally:
                self._current_turn_task = None

    # -- permission mode ---------------------------------------------------

    async def set_mode(
        self, mode: PermissionMode, *, revoke_session_grants: bool | None = None
    ) -> None:
        """Switch the permission mode at runtime and persist it (D14).

        Tightening to `manual` revokes standing session grants by default:
        keeping them would mean a mode chosen to make the device ask again
        silently does not. Pass `revoke_session_grants` explicitly to override.
        """
        previous = self._mode
        self._mode = mode
        await self._grants.set_session_mode(self.session_id, str(mode))

        should_revoke = (
            revoke_session_grants
            if revoke_session_grants is not None
            else mode is PermissionMode.MANUAL
        )
        revoked = 0
        if should_revoke:
            revoked = await self._grants.revoke_session_grants(
                self.session_id, now=datetime.now(UTC)
            )

        await self._bus.publish(
            Event(
                type=EVENT_MODE_CHANGED,
                source="agent_session",
                payload={
                    "session_id": self.session_id,
                    "from": str(previous),
                    "to": str(mode),
                    "revoked_session_grants": revoked,
                },
            )
        )
        logger.info(
            "Permission mode changed",
            extra={"session_id": self.session_id, "from": str(previous), "to": str(mode)},
        )

    # -- authorization -----------------------------------------------------

    def pending_authorizations(self) -> list[PendingAuthorization]:
        return self._queue.list_pending()

    async def approve(
        self, pending_id: str, *, scope_to_session: bool = False
    ) -> AuthorizationGrant:
        return await self._queue.approve(
            pending_id, scope_to_session=scope_to_session, mode=self._mode
        )

    async def deny(self, pending_id: str, reason: str = "denied by operator") -> None:
        await self._queue.deny(pending_id, reason)
