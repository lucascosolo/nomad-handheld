"""The persistent session, the turn state machine, and compaction (D11, D16)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from nomad.agent.context import COMPACTION_ROLE, ContextManager, estimate_tokens
from nomad.agent.loop import TurnOutcomeStatus
from nomad.agent.provider import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    StopReason,
)
from nomad.agent.session import AgentSession
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import ProviderError
from nomad.core.events import Event, EventBus
from nomad.storage.db import Database
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets import HidTarget, LocalTarget, SshTarget, TargetRegistry
from nomad.tools.builtin import build_default_registry
from nomad.tools.workspace import Workspace

SESSION_ID = "agent-session-under-test"


# -- fakes ------------------------------------------------------------------


class ScriptedProvider:
    """Returns a scripted response per call. Chunk F implements the real thing."""

    name = "scripted"

    def __init__(self, *responses: ProviderResponse, repeat_last: bool = False) -> None:
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.requests: list[ProviderRequest] = []
        self.on_call = None

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self.on_call is not None:
            await self.on_call(request)
        index = len(self.requests) - 1
        if index >= len(self.responses):
            if self.repeat_last:
                return self.responses[-1]
            return ProviderResponse(text="done", stop_reason=StopReason.END_TURN)
        return self.responses[index]


class ExplodingProvider:
    name = "exploding"

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise ProviderError("no network")


def tool_use(call_id: str, tool: str, target_id: str = "local", **params: object):
    return ProviderResponse(
        stop_reason=StopReason.TOOL_USE,
        tool_calls=[
            ProviderToolCall(id=call_id, tool=tool, target_id=target_id, params=params)
        ],
    )


# -- fixtures ---------------------------------------------------------------


def make_session(
    db: Database,
    bus: EventBus,
    root: Path,
    provider: object,
    *,
    mode: PermissionMode = PermissionMode.MANUAL,
    max_tool_calls: int = 25,
    session_id: str | None = SESSION_ID,
    resume_pending: bool = True,
    authorization_timeout: float = 0.1,
) -> AgentSession:
    config = NomadConfig.model_validate(
        {
            "workspace": {"root": str(root)},
            "agent": {"mode": str(mode), "max_tool_calls_per_turn": max_tool_calls},
        }
    )
    workspace = Workspace(root)
    targets = TargetRegistry()
    targets.register(LocalTarget())
    targets.register(HidTarget())
    targets.register(SshTarget(alias="ws", host="h", user="u"))
    return AgentSession(
        config=config,
        bus=bus,
        conversations=ConversationsRepository(db),
        grants=GrantsRepository(db),
        targets=targets,
        tools=build_default_registry(config),
        workspace=workspace,
        provider=provider,  # type: ignore[arg-type]
        session_id=session_id,
        resume_pending=resume_pending,
        authorization_timeout=authorization_timeout,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "notes.md").write_text("hello\n")
    return workspace_root


# -- the vertical slice -----------------------------------------------------


async def test_a_read_only_tool_call_runs_end_to_end_without_a_prompt(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    """ARCHITECTURE.md's vertical slice, in manual mode: no human needed."""
    provider = ScriptedProvider(
        tool_use("c1", "get_system_info"),
        ProviderResponse(text="You are running Linux.", stop_reason=StopReason.END_TURN),
    )
    session = make_session(db, event_bus, root, provider)
    await session.start()

    outcome = await session.send("What system are you running on?")

    assert outcome.status is TurnOutcomeStatus.COMPLETE
    assert outcome.text == "You are running Linux."
    assert outcome.tool_calls == 1

    turn = await ConversationsRepository(db).get_turn(outcome.turn_id)
    assert turn is not None and turn.status == "complete"

    messages = await ConversationsRepository(db).get_messages_for_turn(outcome.turn_id)
    assert [m.role for m in messages] == ["user", "tool", "assistant"]
    assert "system:" in messages[1].content["text"]

    # The tool observation was fed back to the model on the second call.
    assert provider.requests[1].messages[-1].role is MessageRole.TOOL
    await session.stop()


async def test_the_turn_row_exists_before_the_provider_is_called(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    """D11: persist before execute, so a power cut leaves a record."""
    seen: list[str] = []

    async def inspect(request: ProviderRequest) -> None:
        rows = await db.fetch_all("SELECT id, status FROM turns")
        seen.extend(f"{row['id']}:{row['status']}" for row in rows)

    provider = ScriptedProvider(ProviderResponse(text="ok"))
    provider.on_call = inspect
    session = make_session(db, event_bus, root, provider)
    await session.start()
    outcome = await session.send("hello")

    assert seen == [f"{outcome.turn_id}:running"]
    await session.stop()


async def test_a_denied_tool_call_becomes_an_observation_not_a_failed_turn(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="../escape.txt", content="x"),
        ProviderResponse(text="I could not do that.", stop_reason=StopReason.END_TURN),
    )
    session = make_session(db, event_bus, root, provider)
    await session.start()
    outcome = await session.send("write outside")

    assert outcome.status is TurnOutcomeStatus.COMPLETE
    observation = provider.requests[1].messages[-1]
    assert observation.content.startswith("DENIED:")
    assert "outside the workspace root" in observation.content
    await session.stop()


async def test_an_unanswered_authorization_auto_denies_without_failing_the_turn(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="new.txt", content="x"),
        ProviderResponse(text="I was not allowed.", stop_reason=StopReason.END_TURN),
    )
    session = make_session(db, event_bus, root, provider, authorization_timeout=0.05)
    await session.start()
    outcome = await session.send("write a file")

    assert outcome.status is TurnOutcomeStatus.COMPLETE
    observation = provider.requests[1].messages[-1]
    assert "NOT AUTHORIZED" in observation.content
    assert "timeout" in observation.content
    assert not (root / "new.txt").exists()
    await session.stop()


async def test_an_approved_authorization_lets_the_turn_continue(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="new.txt", content="written"),
        ProviderResponse(text="Done.", stop_reason=StopReason.END_TURN),
    )
    session = make_session(db, event_bus, root, provider, authorization_timeout=5)
    await session.start()

    task = asyncio.ensure_future(session.send("write a file"))
    pending = await _await_pending(session)
    assert pending.tool == "write_file"

    # The turn parks rather than failing while it waits.
    turn = await ConversationsRepository(db).get_turn(
        (await ConversationsRepository(db).find_incomplete_turns())[0].id
    )
    assert turn is not None and turn.status == "awaiting_grant"

    await session.approve(pending.id)
    outcome = await task

    assert outcome.status is TurnOutcomeStatus.COMPLETE
    assert (root / "new.txt").read_text() == "written"
    await session.stop()


async def test_a_denied_authorization_is_reported_to_the_model(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="new.txt", content="x"),
        ProviderResponse(text="Understood.", stop_reason=StopReason.END_TURN),
    )
    session = make_session(db, event_bus, root, provider, authorization_timeout=5)
    await session.start()

    task = asyncio.ensure_future(session.send("write a file"))
    pending = await _await_pending(session)
    await session.deny(pending.id, "not now")
    outcome = await task

    assert outcome.status is TurnOutcomeStatus.COMPLETE
    assert "NOT AUTHORIZED" in provider.requests[1].messages[-1].content
    assert not (root / "new.txt").exists()
    await session.stop()


async def test_the_tool_call_budget_parks_the_turn_instead_of_crashing(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(tool_use("c1", "get_system_info"), repeat_last=True)
    session = make_session(db, event_bus, root, provider, max_tool_calls=3)
    await session.start()
    outcome = await session.send("loop forever")

    assert outcome.status is TurnOutcomeStatus.PARKED
    assert outcome.tool_calls == 3
    assert "budget" in (outcome.reason or "")

    turn = await ConversationsRepository(db).get_turn(outcome.turn_id)
    assert turn is not None and turn.status == "awaiting_grant"
    await session.stop()


async def test_a_provider_failure_fails_the_turn_cleanly(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    session = make_session(db, event_bus, root, ExplodingProvider())
    await session.start()
    outcome = await session.send("hello")

    assert outcome.status is TurnOutcomeStatus.FAILED
    assert outcome.reason == "no network"
    turn = await ConversationsRepository(db).get_turn(outcome.turn_id)
    assert turn is not None and turn.status == "failed"
    await session.stop()


async def test_turn_events_are_published(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    event_bus.subscribe("agent.*", handler)
    session = make_session(db, event_bus, root, ScriptedProvider(ProviderResponse(text="ok")))
    await session.start()
    await session.send("hi")
    await asyncio.sleep(0.05)

    types = {event.type for event in seen}
    assert {"agent.resumed", "agent.turn_started", "agent.turn_finished"} <= types
    await session.stop()


# -- permission mode is session state (D14) --------------------------------


async def test_the_permission_mode_is_persisted_across_a_restart(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    first = make_session(db, event_bus, root, ScriptedProvider())
    await first.start()
    assert first.mode is PermissionMode.MANUAL
    await first.set_mode(PermissionMode.AUTO)
    await first.stop()

    second = make_session(db, event_bus, root, ScriptedProvider())
    await second.start()
    # The operator's last explicit choice outranks the config default.
    assert second.mode is PermissionMode.AUTO
    await second.stop()


async def test_tightening_to_manual_revokes_standing_session_grants(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="a.txt", content="x"),
        ProviderResponse(text="ok"),
    )
    session = make_session(
        db, event_bus, root, provider, mode=PermissionMode.SESSION, authorization_timeout=5
    )
    await session.start()

    task = asyncio.ensure_future(session.send("write"))
    pending = await _await_pending(session)
    grant = await session.approve(pending.id, scope_to_session=True)
    await task
    assert grant.source.value == "session"

    grants = GrantsRepository(db)
    assert await grants.find_session_grants(
        session_id=session.session_id,
        tool="write_file",
        target="local",
        scope="workspace",
        now=grant.granted_at,
    )

    await session.set_mode(PermissionMode.MANUAL)
    from datetime import UTC, datetime

    assert not await grants.find_session_grants(
        session_id=session.session_id,
        tool="write_file",
        target="local",
        scope="workspace",
        now=datetime.now(UTC),
    )
    await session.stop()


# -- boot recovery (D11) ---------------------------------------------------


async def test_resume_aborts_a_turn_that_was_mid_execution(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    interrupted = await conversations.create_turn(session_id=SESSION_ID, status="running")

    session = make_session(db, event_bus, root, ScriptedProvider(), resume_pending=False)
    report = await _start_and_report(session)

    assert interrupted.id in report.aborted
    assert interrupted.id not in report.resumed
    turn = await conversations.get_turn(interrupted.id)
    assert turn is not None and turn.status == "aborted"
    await session.stop()


async def test_resume_aborts_a_pending_turn_with_nothing_to_replay(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    empty = await conversations.create_turn(session_id=SESSION_ID, status="pending")

    session = make_session(db, event_bus, root, ScriptedProvider(), resume_pending=False)
    report = await _start_and_report(session)
    assert empty.id in report.aborted
    await session.stop()


async def test_resume_replays_a_turn_that_never_started(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    queued = await conversations.create_turn(session_id=SESSION_ID, status="pending")
    await conversations.add_message(
        turn_id=queued.id, session_id=SESSION_ID, role="user", content={"text": "resume me"}
    )

    provider = ScriptedProvider(ProviderResponse(text="resumed and answered"))
    session = make_session(db, event_bus, root, provider)
    await session.start()

    turn = await conversations.get_turn(queued.id)
    assert turn is not None and turn.status == "complete"
    assert provider.requests[0].messages[0].content == "resume me"
    await session.stop()


async def test_unresolved_prompts_do_not_survive_a_restart(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    provider = ScriptedProvider(
        tool_use("c1", "write_file", path="a.txt", content="x"),
        ProviderResponse(text="ok"),
    )
    session = make_session(db, event_bus, root, provider, authorization_timeout=5)
    await session.start()
    task = asyncio.ensure_future(session.send("write"))
    pending = await _await_pending(session)
    await session.deny(pending.id)
    await task
    await session.stop()

    grants = GrantsRepository(db)
    assert await grants.list_unresolved(SESSION_ID) == []


async def test_stop_aborts_anything_left_in_flight(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    session = make_session(db, event_bus, root, ScriptedProvider(), resume_pending=False)
    await session.start()
    stranded = await conversations.create_turn(session_id=SESSION_ID, status="running")
    await session.stop()

    turn = await conversations.get_turn(stranded.id)
    assert turn is not None and turn.status == "aborted"


# -- context budget and compaction (D16) -----------------------------------


def message(role: MessageRole, content: str) -> ProviderMessage:
    return ProviderMessage(role=role, content=content)


def test_the_token_estimator_is_monotonic_and_roughly_four_chars_per_token() -> None:
    short = [message(MessageRole.USER, "x" * 40)]
    long = [message(MessageRole.USER, "x" * 400)]
    assert estimate_tokens(short) < estimate_tokens(long)
    assert 10 <= estimate_tokens(short) <= 20


def test_compaction_triggers_at_the_configured_fraction() -> None:
    manager = ContextManager(max_tokens=1000, compact_at=0.75)
    assert manager.threshold_tokens == 750
    small = [message(MessageRole.USER, "x" * 400)]
    assert manager.should_compact(small) is False
    big = [message(MessageRole.USER, "x" * 4000)]
    assert manager.should_compact(big) is True


def test_the_estimator_self_corrects_against_reported_usage() -> None:
    manager = ContextManager(max_tokens=1000)
    messages = [message(MessageRole.USER, "x" * 400)]
    raw = manager.estimate_tokens(messages)
    manager.observe_usage(messages, raw * 2)
    assert manager.estimate_tokens(messages) == raw * 2


def test_compact_at_must_be_a_fraction() -> None:
    with pytest.raises(ValueError):
        ContextManager(compact_at=0.0)
    with pytest.raises(ValueError):
        ContextManager(compact_at=1.5)


async def test_compaction_preserves_system_and_recent_messages(
    db: Database, event_bus: EventBus
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    turn = await conversations.create_turn(session_id=SESSION_ID)

    manager = ContextManager(conversations=conversations, bus=event_bus, keep_recent=2)
    messages = [
        message(MessageRole.SYSTEM, "you are nomad"),
        *[message(MessageRole.USER, f"message {i}") for i in range(6)],
    ]

    async def summarizer(older: Sequence[ProviderMessage]) -> str:
        return f"summarized {len(older)} messages"

    compacted, record = await manager.compact(
        messages, session_id=SESSION_ID, turn_id=turn.id, summarizer=summarizer
    )

    assert record is not None
    assert record.messages_compacted == 4
    assert record.tokens_after < record.tokens_before
    assert compacted[0].content == "you are nomad"
    assert "summarized 4 messages" in compacted[1].content
    assert [m.content for m in compacted[-2:]] == ["message 4", "message 5"]


async def test_a_compaction_record_is_persisted_not_discarded(
    db: Database, event_bus: EventBus
) -> None:
    """D16: compaction records are durable artifacts."""
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    turn = await conversations.create_turn(session_id=SESSION_ID)
    manager = ContextManager(conversations=conversations, bus=event_bus, keep_recent=1)

    messages = [message(MessageRole.USER, f"m{i}") for i in range(5)]
    _, record = await manager.compact(messages, session_id=SESSION_ID, turn_id=turn.id)
    assert record is not None

    stored = await conversations.get_messages_for_turn(turn.id)
    saved = [m for m in stored if m.role == COMPACTION_ROLE]
    assert len(saved) == 1
    assert saved[0].content["messages_compacted"] == 4
    assert saved[0].content["summary"]


async def test_a_failing_summarizer_falls_back_instead_of_losing_history(
    db: Database, event_bus: EventBus
) -> None:
    conversations = ConversationsRepository(db)
    await conversations.create_session(mode="manual", session_id=SESSION_ID)
    turn = await conversations.create_turn(session_id=SESSION_ID)
    manager = ContextManager(conversations=conversations, bus=event_bus, keep_recent=1)

    async def broken(older: Sequence[ProviderMessage]) -> str:
        raise RuntimeError("model down")

    messages = [message(MessageRole.USER, f"m{i}") for i in range(5)]
    compacted, record = await manager.compact(
        messages, session_id=SESSION_ID, turn_id=turn.id, summarizer=broken
    )
    assert record is not None and record.summarizer_failed is True
    assert "compacted 4 earlier messages" in compacted[0].content


async def test_compaction_is_a_no_op_when_there_is_little_to_compact(
    db: Database, event_bus: EventBus
) -> None:
    manager = ContextManager(keep_recent=8)
    messages = [message(MessageRole.USER, "m")]
    compacted, record = await manager.compact(messages, session_id=SESSION_ID, turn_id=None)
    assert record is None
    assert compacted == messages


async def test_history_stays_compacted_across_a_restart(
    db: Database, event_bus: EventBus, root: Path
) -> None:
    """A reloaded session must not silently re-inflate compacted history."""
    conversations = ConversationsRepository(db)
    provider = ScriptedProvider(ProviderResponse(text="first"), ProviderResponse(text="second"))
    session = make_session(db, event_bus, root, provider)
    await session.start()
    await session.send("an early question")

    turn = await conversations.create_turn(session_id=session.session_id)
    manager = ContextManager(conversations=conversations, bus=event_bus, keep_recent=0)
    await manager.compact(
        [message(MessageRole.USER, "an early question")],
        session_id=session.session_id,
        turn_id=turn.id,
    )
    await conversations.update_turn_status(turn.id, "complete")

    await session.send("a later question")
    sent = provider.requests[-1].messages
    assert sent[0].role is MessageRole.SYSTEM
    assert "Summary of earlier conversation" in sent[0].content
    assert sent[-1].content == "a later question"
    assert not any(m.content == "first" for m in sent)
    await session.stop()


# -- helpers ----------------------------------------------------------------


async def _await_pending(session: AgentSession, attempts: int = 300):
    for _ in range(attempts):
        pending = session.pending_authorizations()
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending authorization appeared")


async def _start_and_report(session: AgentSession):
    await session.start()
    report = session.last_resume_report
    assert report is not None
    return report
