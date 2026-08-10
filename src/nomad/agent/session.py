"""`AgentSession` — the persistent session (D11), now around a backend (D19).

Nomad is a session, not a request/response service. `AgentSession` is a
long-lived `Component` started at boot and running until shutdown; HTTP,
WebSocket and the ESP32 display are *views* onto it. Closing the screen does
not end the conversation.

The session owns four things a view must never own:

* **conversation state**, including which turn is in flight;
* **the permission mode** (D14) — switchable at runtime, persisted, so a
  reboot does not silently restore a more permissive setting than the last
  one chosen;
* **the pending-authorization queue**, so a prompt raised while the screen was
  off is still waiting when it comes back;
* **the backend** (D24), started and stopped with the session rather than per
  request — a subprocess respawned per message would lose the context window
  that makes the thing worth having.

What it no longer owns, after D19: the turn loop and context compaction. The
backend does those. What survived unchanged: the entire permission pipeline,
which is why the pivot cost far less than it looks.

At boot, `resume()` looks for turns left non-terminal by a crash or power cut
and either re-drives them or aborts them cleanly, recording which. It never
leaves one in limbo.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from nomad.agent.backends import create_backend
from nomad.agent.backends.base import AgentBackend, AgentEvent, AgentEventKind, BackendCapability
from nomad.agent.claude_tools import register_backend_tools
from nomad.agent.permission_bridge import PermissionBridge
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import AgentError
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.core.turns import TurnSource
from nomad.mcp.server import McpToolRouter, build_hardware_tools, register_hardware_tools
from nomad.memory.briefing import compose_briefing
from nomad.memory.rollover import should_roll
from nomad.memory.store import MemoryStore
from nomad.storage.repositories.conversations import ConversationsRepository, TurnStatus
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.registry import TargetRegistry
from nomad.tools.base import Tool
from nomad.tools.permissions import (
    DEFAULT_AUTHORIZATION_TIMEOUT,
    AuthorizationGrant,
    AuthorizationQueue,
    Classifier,
    GrantVault,
    PendingAuthorization,
    PermissionBroker,
    ToolExecutor,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

logger = get_logger(__name__)

EVENT_MODE_CHANGED = "agent.mode_changed"
EVENT_RESUMED = "agent.resumed"
EVENT_AGENT_EVENT = "agent.event"
EVENT_SESSION_ROLLED = "agent.session_rolled"
#: A turn's own boundaries, published for views (D11: HTTP, WebSocket and the
#: screen are views onto the session). `agent.event` alone cannot bracket a
#: turn: the first backend event may be seconds away, so a view watching only
#: that shows nothing between the keypress and the model's first token. And
#: because the bus drops slow subscribers by design (D6), a view that
#: accumulated the text chunks itself could end a turn holding an incomplete
#: answer — so `agent.turn_finished` carries the whole of it, and is the only
#: thing a view should treat as final.
EVENT_TURN_STARTED = "agent.turn_started"
EVENT_TURN_FINISHED = "agent.turn_finished"

_NON_RESUMABLE = ("running", "awaiting_grant")


class TurnOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


#: The agent's outcome vocabulary is not storage's. `turns.status` is
#: constrained by migration 001 to `complete`/`failed`/`aborted`; writing the
#: enum's own spelling straight through raised `CHECK constraint failed` on
#: *every* finished turn, so no turn was ever recorded as done. It went unseen
#: because a `# type: ignore[arg-type]` sat on the call — the annotation was
#: correct and the silencer was the bug. Map explicitly, and let an unmapped
#: member be a KeyError rather than a database error at the end of a turn.
_STORED_STATUS: dict[TurnOutcomeStatus, TurnStatus] = {
    TurnOutcomeStatus.COMPLETED: "complete",
    TurnOutcomeStatus.FAILED: "failed",
    TurnOutcomeStatus.INTERRUPTED: "aborted",
}


class TurnOutcome(BaseModel):
    """What one turn produced. The unit a view renders and storage records."""

    turn_id: str
    status: TurnOutcomeStatus
    #: Carried here rather than looked up, so `agent.turn_finished` — the only
    #: event a view should treat as final — says who asked for the turn it is
    #: closing. A view that had to query the row to find out would be a view
    #: that never bothers.
    source: TurnSource = TurnSource.USER
    text: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


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
        memory: MemoryStore | None = None,
        backend: AgentBackend | None = None,
        hardware_tools: list[Tool] | None = None,
        skill_index: str = "",
        classifier: Classifier | None = None,
        session_id: str | None = None,
        resume_pending: bool = True,
        authorization_timeout: float = DEFAULT_AUTHORIZATION_TIMEOUT,
        turn_timeout: float | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._conversations = conversations
        self._grants = grants
        self._targets = targets
        self._tools = tools
        self._workspace = workspace
        self._memory = memory
        # Rendered, never a library: `agent` may not import `nomad.skills`
        # (D39, `tests/test_layering.py`), so the index crosses this boundary
        # as text exactly like the memory briefing does. Fixed for the life of
        # the session because installing a skill is an operator act, not a
        # tool call (D26) — there is nothing that can change it mid-session.
        self._skill_index = skill_index
        self._session_id = session_id
        self._resume_pending = resume_pending
        self._authorization_timeout = authorization_timeout
        self._turn_timeout = turn_timeout

        self._mode = config.agent.mode
        self._state = ComponentState.NEW
        self._turn_lock = asyncio.Lock()
        self._current_turn_id: str | None = None
        self._current_turn_task: asyncio.Task[Any] | None = None
        self._resume_report: ResumeReport | None = None
        # Rollover bookkeeping. These track the *backend* session, which is
        # not the Nomad session: rolling starts a fresh transcript while the
        # conversation, the mode and the memory all carry straight on.
        self._backend_started_at = datetime.now(UTC)
        self._backend_turns = 0

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

        # The tools the backend may call, on both sides of the boundary:
        # Nomad's hardware (which Nomad executes) and the backend's own tools
        # (which it executes, but which Nomad still classifies and gates).
        self._hardware = (
            hardware_tools if hardware_tools is not None else build_hardware_tools(store=memory)
        )
        register_hardware_tools(tools, self._hardware)
        register_backend_tools(tools)

        self._vault = GrantVault()
        self._bridge = PermissionBridge(
            broker=self._broker,
            queue=self._queue,
            tools=tools,
            bus=bus,
            session_id_provider=lambda: self.session_id,
            mode_provider=lambda: self._mode,
            turn_id_provider=lambda: self._current_turn_id,
            vault=self._vault,
            timeout=authorization_timeout,
        )
        self._router = McpToolRouter(
            executor=self._executor,
            vault=self._vault,
            specs=[tool.spec for tool in self._hardware],
        )
        # An injected backend is the caller's to manage: the session must not
        # replace it at start, nor roll it. Rollover rebuilds via
        # `create_backend`, which cannot reconstruct something handed in.
        self._backend_injected = backend is not None
        self._backend = backend or self._new_backend()
        self._events: AsyncIterator[AgentEvent] | None = None

    def _new_backend(
        self, *, briefing: str = "", resume_session_id: str | None = None
    ) -> AgentBackend:
        """Every backend this session ever builds gets the index.

        `briefing` is a parameter because it needs I/O and is therefore only
        available after `start()`; the index is not, because it was read from
        disk before the session existed. So it is passed unconditionally here
        rather than at one call site — the backend built in `__init__` and the
        one built by a rollover must not differ in what the model is told it
        knows how to do.
        """
        return create_backend(
            self._config,
            bridge=self._bridge,
            router=self._router,
            cwd=str(self._workspace.root),
            briefing=briefing,
            skill_index=self._skill_index,
            resume_session_id=resume_session_id,
        )

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
    def bridge(self) -> PermissionBridge:
        return self._bridge

    @property
    def router(self) -> McpToolRouter:
        return self._router

    @property
    def backend(self) -> AgentBackend:
        return self._backend

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    @property
    def busy(self) -> bool:
        """Whether a turn holds the session right now.

        Reads the turn lock and not `current_turn_id`, because the two are not
        the same fact: `send()` takes the lock *before* `_run_turn` writes the
        turn row, so there is a window in which a turn is unquestionably in
        flight and `current_turn_id` is still `None`.

        That distinction is the whole reason this property exists. Anything
        that starts a turn nobody asked for has to be able to see a live turn
        and stand down — `send()` would otherwise queue silently behind the
        operator and surface minutes later as a reply to nothing they said.
        """
        return self._turn_lock.locked()

    @property
    def last_resume_report(self) -> ResumeReport | None:
        """What boot recovery did, for a view that reconnects and asks."""
        return self._resume_report

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._workspace.ensure_exists()

        existing = (
            await self._conversations.get_session(self._session_id) if self._session_id else None
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

        # The briefing needs I/O and `create_backend` is deliberately sync, so
        # the backend built in `__init__` is replaced here — once, before it is
        # started, and only when there is something to inject. Constructing a
        # backend has no side effects; starting one does.
        briefing = await self._compose_briefing()
        if briefing and not self._backend_injected:
            self._backend = self._new_backend(briefing=briefing)

        await self._backend.start()
        self._events = self._backend.events()
        self._backend_started_at = datetime.now(UTC)
        self._backend_turns = 0

        self._resume_report = await self.resume()
        self._state = ComponentState.STARTED
        logger.info(
            "Agent session started",
            extra={
                "session_id": self._session_id,
                "mode": str(self._mode),
                "backend": self._backend.name,
            },
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
        try:
            await self._backend.stop()
        except Exception as exc:  # noqa: BLE001 - never fail a shutdown
            logger.warning("Backend stop failed", extra={"error": str(exc)})
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
        #: Provenance has to survive the recovery that re-drives a turn, or a
        #: power cut would quietly relabel the device's own work as the
        #: operator's. Collected on the pass below rather than re-read later,
        #: which would be a second query for a fact already in hand.
        sources: dict[str, TurnSource] = {}

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
            sources[turn.id] = turn.source

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
                messages = await self._conversations.get_messages_for_turn(turn_id)
                prompt = next((m for m in messages if m.role == "user"), None)
                if prompt is None:  # pragma: no cover - filtered above
                    continue
                await self._conversations.update_turn_status(turn_id, "aborted")
                await self.send(
                    str(prompt.content.get("text", "")),
                    source=sources.get(turn_id, TurnSource.USER),
                )
        return report

    # -- memory ------------------------------------------------------------

    async def _compose_briefing(self) -> str:
        """What Nomad already knows, bounded, ready to append to the identity.

        Never raises: a memory store that cannot be read is a session without
        a briefing, not a device that will not boot.
        """
        if self._memory is None or not self._config.memory.enabled:
            return ""
        try:
            pinned = await self._memory.for_briefing()
            counts = await self._memory.unpinned_counts_by_kind()
        except Exception as exc:  # noqa: BLE001 - memory must never block a boot
            logger.warning(
                "Could not read memory for the session briefing",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return ""
        return compose_briefing(
            pinned,
            budget_chars=self._config.memory.injection_budget_chars,
            max_memories=self._config.memory.injection_max_memories,
            unpinned_counts=counts,
        )

    async def _maybe_roll_backend(self) -> None:
        """Start a fresh backend session at a turn boundary when it has run long.

        The new session gets `resume_session_id=None` and the briefing — not
        the old transcript. Replaying the transcript would reintroduce exactly
        the unbounded growth the roll exists to escape; memory is what makes
        dropping it survivable, which is why this policy lives with memory and
        not with the backend.
        """
        if self._memory is None or self._backend_injected:
            return
        decision = should_roll(
            started_at=self._backend_started_at,
            turn_count=self._backend_turns,
            now=datetime.now(UTC),
            config=self._config.memory,
        )
        if not decision.roll:
            return

        briefing = await self._compose_briefing()
        try:
            await self._backend.stop()
        except Exception as exc:  # noqa: BLE001 - a roll must not fail the turn
            logger.warning("Backend stop during rollover failed", extra={"error": str(exc)})
        self._backend = self._new_backend(briefing=briefing, resume_session_id=None)
        await self._backend.start()
        self._events = self._backend.events()
        self._backend_started_at = datetime.now(UTC)
        self._backend_turns = 0

        await self._bus.publish(
            Event(
                type=EVENT_SESSION_ROLLED,
                source="agent_session",
                payload={
                    "session_id": self.session_id,
                    "reason": decision.reason,
                    "briefing_chars": len(briefing),
                },
            )
        )
        logger.info("Backend session rolled over", extra={"reason": decision.reason})

    # -- conversation ------------------------------------------------------

    async def send(self, text: str, *, source: TurnSource = TurnSource.USER) -> TurnOutcome:
        """Run one turn. Serialized: one turn at a time per session.

        `source` defaults to `USER` because that is what every caller that
        predates it meant, and because a caller that forgets it should be
        wrong in the direction of "a person asked for this" rather than
        silently attributing an operator's words to the device.

        Note the serialization: a caller that is *not* a person must check
        `busy` first and skip, because waiting on this lock means running
        whenever the operator happens to stop typing.
        """
        async with self._turn_lock:
            task = asyncio.ensure_future(self._run_turn(text, source))
            self._current_turn_task = task
            try:
                return await task
            finally:
                self._current_turn_task = None

    async def _run_turn(self, text: str, source: TurnSource = TurnSource.USER) -> TurnOutcome:
        """Drive one turn through the backend and record what happened.

        The backend owns the loop (D19), so this is not a turn *loop* — it is
        bookkeeping around one. The turn row is written **before** the backend
        is asked to do anything, so a power cut mid-turn leaves a durable
        record of what was in flight rather than a silent gap (D11, D18).
        """
        if BackendCapability.OWN_LOOP not in self._backend.capabilities:
            # An honest refusal beats half a loop. D24 records that a backend
            # without OWN_LOOP needs Nomad to supply one; until that exists,
            # say so rather than appearing to work.
            raise AgentError(
                f"Backend '{self._backend.name}' does not bring its own loop and Nomad "
                "does not supply one yet (D24)",
                {"backend": self._backend.name},
            )
        if self._events is None:
            raise RuntimeError("AgentSession has not been started")

        await self._maybe_roll_backend()
        self._backend_turns += 1

        turn = await self._conversations.create_turn(
            session_id=self.session_id, status="running", source=source
        )
        self._current_turn_id = turn.id
        await self._conversations.add_message(
            turn_id=turn.id, session_id=self.session_id, role="user", content={"text": text}
        )

        await self._bus.publish(
            Event(
                type=EVENT_TURN_STARTED,
                source="agent_session",
                payload={
                    "session_id": self.session_id,
                    "turn_id": turn.id,
                    "text": text,
                    # An added key, never a renamed event. `resources/governor.py`
                    # matches these two events by *name* as text and
                    # `tests/test_resources.py` pins that, which is exactly why
                    # provenance travels in the payload.
                    "source": str(source),
                },
            )
        )

        outcome = TurnOutcome(turn_id=turn.id, status=TurnOutcomeStatus.COMPLETED, source=source)
        try:
            await self._backend.send(text, session_id=self.session_id)
            await self._consume(turn.id, outcome)
        except asyncio.CancelledError:
            await self._conversations.update_turn_status(turn.id, "aborted")
            self._current_turn_id = None
            outcome.status = TurnOutcomeStatus.INTERRUPTED
            await self._publish_turn_finished(outcome)
            raise
        except Exception as exc:  # noqa: BLE001 - a backend failure is a failed turn
            logger.error(
                "Turn failed", extra={"turn_id": turn.id, "error": f"{type(exc).__name__}: {exc}"}
            )
            outcome.status = TurnOutcomeStatus.FAILED
            outcome.error = f"{type(exc).__name__}: {exc}"

        await self._conversations.update_turn_status(turn.id, _STORED_STATUS[outcome.status])
        if outcome.text:
            await self._conversations.add_message(
                turn_id=turn.id,
                session_id=self.session_id,
                role="assistant",
                content={"text": outcome.text, "tool_calls": outcome.tool_calls},
            )
        self._current_turn_id = None
        await self._publish_turn_finished(outcome)
        return outcome

    async def _publish_turn_finished(self, outcome: TurnOutcome) -> None:
        """Announce the end of a turn, carrying the whole answer.

        Never raises: a view that cannot be told is not a reason to fail a turn
        that already succeeded.
        """
        try:
            await self._bus.publish(
                Event(
                    type=EVENT_TURN_FINISHED,
                    source="agent_session",
                    payload={
                        "session_id": self._session_id,
                        **outcome.model_dump(mode="json"),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - telling a view must never fail a turn
            logger.warning("Could not publish turn_finished", extra={"error": str(exc)})

    async def _consume(self, turn_id: str, outcome: TurnOutcome) -> None:
        """Drain backend events until the turn ends, republishing each one."""
        assert self._events is not None
        chunks: list[str] = []

        async def drain() -> None:
            assert self._events is not None
            async for event in self._events:
                stamped = event.model_copy(update={"turn_id": event.turn_id or turn_id})
                await self._bus.publish(
                    Event(
                        type=EVENT_AGENT_EVENT,
                        source="agent_session",
                        payload=stamped.model_dump(mode="json"),
                    )
                )
                if stamped.kind is AgentEventKind.TEXT:
                    chunks.append(stamped.text)
                elif stamped.kind is AgentEventKind.TOOL_CALL and stamped.tool_name:
                    outcome.tool_calls.append(stamped.tool_name)
                elif stamped.kind is AgentEventKind.USAGE:
                    outcome.input_tokens += stamped.input_tokens
                    outcome.output_tokens += stamped.output_tokens
                elif stamped.kind is AgentEventKind.ERROR:
                    outcome.status = TurnOutcomeStatus.FAILED
                    outcome.error = stamped.error
                    return
                elif stamped.kind is AgentEventKind.TURN_COMPLETE:
                    if not stamped.ok:
                        outcome.status = TurnOutcomeStatus.FAILED
                        outcome.error = stamped.error
                    return
            # The stream ended without a terminal event: the backend went away
            # mid-turn. That is a failed turn, not a completed empty one.
            outcome.status = TurnOutcomeStatus.FAILED
            outcome.error = outcome.error or "backend stream ended mid-turn"

        try:
            if self._turn_timeout is None:
                await drain()
            else:
                await asyncio.wait_for(drain(), timeout=self._turn_timeout)
        except TimeoutError:
            outcome.status = TurnOutcomeStatus.FAILED
            outcome.error = f"turn exceeded {self._turn_timeout}s"
            await self.interrupt()
        finally:
            outcome.text = "".join(chunks)

    async def interrupt(self) -> None:
        """Stop the turn in flight. Safe to call when there is none."""
        try:
            await self._backend.interrupt()
        except Exception as exc:  # noqa: BLE001 - an interrupt must not raise
            logger.warning("Backend interrupt failed", extra={"error": str(exc)})

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


#: Kept as a named type so views can annotate a callback without importing the
#: whole session module's internals.
ModeProvider = Callable[[], PermissionMode]
