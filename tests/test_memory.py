"""The memory Nomad owns (chunk M).

Behaviour, not implementation. The load-bearing invariant is the last one in
this file: the size of what gets injected into the prompt does not depend on
how large the store has grown.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nomad.agent.backends.mock import MockBackend
from nomad.agent.session import AgentSession
from nomad.core.config import MemoryConfig, NomadConfig
from nomad.core.events import EventBus
from nomad.memory.briefing import HEADING, INDEX_PREFIX, compose_briefing
from nomad.memory.errors import MemoryRefused
from nomad.memory.models import MAX_TEXT_CHARS, Memory, MemoryKind, normalize_text
from nomad.memory.redaction import looks_like_secret
from nomad.memory.rollover import should_roll
from nomad.memory.store import MemoryStore
from nomad.storage.db import Database
from nomad.storage.migrations import MIGRATIONS, current_version, migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.registry import TargetRegistry
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace


@pytest.fixture
def store(db: Database) -> MemoryStore:
    return MemoryStore(db)


def _memory(
    *,
    id: str,
    text: str,
    pinned: bool = False,
    recall_count: int = 0,
    updated_at: datetime | None = None,
    kind: MemoryKind = MemoryKind.FACT,
    forgotten_at: datetime | None = None,
) -> Memory:
    moment = updated_at or datetime(2026, 1, 1, tzinfo=UTC)
    return Memory(
        id=id,
        text=text,
        kind=kind,
        pinned=pinned,
        recall_count=recall_count,
        created_at=moment,
        updated_at=moment,
        forgotten_at=forgotten_at,
    )


# --- migration -------------------------------------------------------------


async def test_migration_002_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "memories.db")
    await database.start()
    try:
        first = await migrate(database)
        second = await migrate(database)
        assert first == second == len(MIGRATIONS)
        assert await current_version(database) == second
        row = await database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        )
        assert row is not None
    finally:
        await database.stop()


# --- dedup -----------------------------------------------------------------


async def test_remembering_the_same_fact_updates_rather_than_duplicates(
    store: MemoryStore,
) -> None:
    first = await store.remember("Lucas prefers terse answers", keywords=["answers"])
    second = await store.remember("  lucas Prefers terse answers.  ", keywords=["style"])

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert set(second.keywords) == {"answers", "style"}
    assert await store.count_active() == 1


async def test_re_remembering_never_silently_unpins(store: MemoryStore) -> None:
    pinned = await store.remember("Lucas builds Nomad", pinned=True)
    again = await store.remember("Lucas builds Nomad", pinned=False)
    assert again.id == pinned.id
    assert again.pinned is True


async def test_remembering_a_forgotten_fact_revives_it(store: MemoryStore) -> None:
    memory = await store.remember("The Pi is called nomad.local")
    assert await store.forget(memory.id) is True
    assert await store.recall("nomad.local") == []

    revived = await store.remember("The Pi is called nomad.local")
    assert revived.id == memory.id
    assert revived.forgotten_at is None
    assert [m.id for m in await store.recall("nomad.local")] == [memory.id]


# --- forgetting is soft ----------------------------------------------------


async def test_forget_keeps_the_row_but_drops_it_from_recall_and_injection(
    store: MemoryStore,
) -> None:
    memory = await store.remember("Ship on Fridays", pinned=True)
    assert await store.forget(memory.id) is True

    kept = await store.get(memory.id)
    assert kept is not None
    assert kept.text == "Ship on Fridays"
    assert kept.forgotten_at is not None

    assert await store.recall("fridays") == []
    assert await store.for_briefing() == []
    assert await store.forget(memory.id) is False


# --- the cap ---------------------------------------------------------------


async def test_cap_prunes_the_least_valuable_and_never_a_pinned_row(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(max_memories=3, max_pinned=2))
    keeper = await store.remember("pinned and load bearing", pinned=True)
    stale = await store.remember("never recalled")
    used = await store.remember("often recalled")
    await store.recall("often")

    await store.remember("the newcomer")

    assert await store.get(stale.id) is None
    assert await store.get(keeper.id) is not None
    assert await store.get(used.id) is not None


async def test_cap_prefers_already_forgotten_rows_when_pruning(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(max_memories=3))
    forgotten = await store.remember("wrong about the battery")
    never_used = await store.remember("never recalled at all")
    await store.remember("third fact")
    await store.forget(forgotten.id)

    await store.remember("the newcomer")

    assert await store.get(forgotten.id) is None
    assert await store.get(never_used.id) is not None


async def test_overflow_with_everything_pinned_refuses(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(max_memories=2, max_pinned=4))
    await store.remember("first", pinned=True)
    await store.remember("second", pinned=True)

    with pytest.raises(MemoryRefused, match="every memory is pinned"):
        await store.remember("third")


async def test_pinning_past_the_pin_cap_refuses_and_names_a_victim(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(max_pinned=2))
    weakest = await store.remember("least used pin", pinned=True)
    await store.remember("well used pin", pinned=True)
    await store.recall("well used")

    with pytest.raises(MemoryRefused) as excinfo:
        await store.remember("a third pin", pinned=True)
    assert weakest.id in str(excinfo.value)

    # ...but storing it unpinned is still fine.
    stored = await store.remember("a third pin")
    assert stored.pinned is False


async def test_promoting_an_existing_memory_past_the_pin_cap_refuses(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(max_pinned=1))
    await store.remember("the one pin", pinned=True)
    await store.remember("a plain fact")

    with pytest.raises(MemoryRefused, match="pinned memories"):
        await store.remember("a plain fact", pinned=True)


# --- length ----------------------------------------------------------------


async def test_a_memory_longer_than_the_cap_is_refused_with_advice(store: MemoryStore) -> None:
    with pytest.raises(MemoryRefused, match="Split it"):
        await store.remember("x" * (MAX_TEXT_CHARS + 1))


# --- recall ----------------------------------------------------------------


async def test_keyword_match_outranks_a_substring_match(store: MemoryStore) -> None:
    substring = await store.remember("The deploy script lives in scripts/deploy.sh")
    keyword = await store.remember("Releases go out on Fridays", keywords=["deploy"])

    results = await store.recall("deploy")
    assert [m.id for m in results] == [keyword.id, substring.id]


async def test_recall_bumps_the_recall_counters(store: MemoryStore) -> None:
    memory = await store.remember("Nomad runs on a Pi 4", keywords=["pi"])
    assert memory.recall_count == 0

    returned = await store.recall("pi")
    assert returned[0].recall_count == 1

    stored = await store.get(memory.id)
    assert stored is not None
    assert stored.recall_count == 1
    assert stored.last_recalled_at is not None


async def test_recall_with_an_empty_query_returns_recent_memories_not_an_error(
    store: MemoryStore,
) -> None:
    await store.remember("first fact")
    await store.remember("second fact")
    assert len(await store.recall("")) == 2


async def test_recall_respects_the_kind_filter_and_the_limit(db: Database) -> None:
    store = MemoryStore(db, config=MemoryConfig(recall_limit=2))
    await store.remember("prefers vim", kind=MemoryKind.PREFERENCE)
    await store.remember("prefers tabs", kind=MemoryKind.PREFERENCE)
    await store.remember("prefers coffee", kind=MemoryKind.PREFERENCE)
    await store.remember("the project is Nomad", kind=MemoryKind.PROJECT)

    assert len(await store.recall("prefers")) == 2
    only_project = await store.recall("", kind=MemoryKind.PROJECT)
    assert [m.kind for m in only_project] == [MemoryKind.PROJECT]


async def test_recall_for_something_unknown_returns_nothing(store: MemoryStore) -> None:
    await store.remember("Nomad runs on a Pi 4")
    assert await store.recall("kubernetes") == []


# --- secrets ---------------------------------------------------------------


#: Credential *shapes*, assembled at import rather than written out.
#:
#: None of these was ever a real secret — they are the patterns
#: `looks_like_secret` must refuse. But a file full of literals in exactly the
#: shape a scanner hunts for is a liability whatever its contents: GitHub's
#: push protection rejected this repository's entire history over the Slack-
#: shaped one, and every future scanner will reach the same conclusion for the
#: same reason. Joining the prefix to the body keeps the test exercising the
#: same input while leaving nothing in the file that pattern-matches a token.
_SHAPES = [
    "the key is " + "sk-ant-" + "api03-" + "AAAABBBBCCCCDDDDEEEEFFFF1234",
    "github token " + "ghp_" + "AbCdEf0123456789AbCdEf0123456789",
    "slack bot " + "xoxb-" + "1234567890-ABCDEFGHIJKLMNOP",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "password: hunter2000",
    "api key = Zx91qQwErTy0",
    "signature 4f9c2b7e1a8d0c3f5e6b2a9d7c4f1e8b0a3d6c9f",
    "token aB3dEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEf",
]


@pytest.mark.parametrize("text", _SHAPES)
async def test_credential_shapes_are_refused(store: MemoryStore, text: str) -> None:
    assert looks_like_secret(text) is not None
    with pytest.raises(MemoryRefused, match="refusing to remember"):
        await store.remember(text)


@pytest.mark.parametrize(
    "text",
    [
        "My password manager is 1Password and I use it for everything",
        "The API key lives in the environment, never in a config file",
        "Lucas prefers terse answers and hates preamble",
        "The deploy script is at /home/lucas/workspace/nomad-handheld/scripts/deploy.sh",
        "nomad.core.config.NomadConfig is where the layered settings are validated",
        "secret: shhh",
    ],
)
async def test_innocuous_text_is_accepted(store: MemoryStore, text: str) -> None:
    assert looks_like_secret(text) is None
    assert (await store.remember(text)).id


# --- briefing --------------------------------------------------------------


def test_briefing_is_deterministic_whatever_order_it_is_given() -> None:
    memories = [
        _memory(
            id=f"{i:04d}",
            text=f"fact number {i}",
            pinned=True,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i % 3),
        )
        for i in range(8)
    ]
    counts = {"project": 4, "fact": 2}
    first = compose_briefing(memories, budget_chars=600, max_memories=8, unpinned_counts=counts)
    shuffled = list(memories)
    random.Random(7).shuffle(shuffled)
    second = compose_briefing(shuffled, budget_chars=600, max_memories=8, unpinned_counts=counts)
    assert first == second
    assert first.startswith(HEADING)


def test_briefing_truncates_at_a_whole_memory_boundary() -> None:
    memories = [
        _memory(id=f"{i:04d}", text=f"memory {i} " + "y" * 60, pinned=True) for i in range(10)
    ]
    block = compose_briefing(memories, budget_chars=260, max_memories=8)

    assert len(block) <= 260
    lines = block.splitlines()
    assert lines[0] == HEADING
    # Every emitted line is a complete memory, not a truncated one.
    rendered = {f"- ({m.kind}) {m.text}" for m in memories}
    assert all(line in rendered for line in lines[1:])


def test_briefing_honours_the_count_cap_before_the_char_budget() -> None:
    memories = [_memory(id=f"{i:04d}", text=f"short {i}", pinned=True) for i in range(20)]
    block = compose_briefing(memories, budget_chars=5000, max_memories=3)
    assert len(block.splitlines()) == 1 + 3


def test_briefing_drops_forgotten_and_unpinned_memories() -> None:
    memories = [
        _memory(id="a", text="pinned and live", pinned=True),
        _memory(id="b", text="pinned but forgotten", pinned=True, forgotten_at=datetime.now(UTC)),
        _memory(id="c", text="merely stored"),
    ]
    block = compose_briefing(memories, budget_chars=600, max_memories=8)
    assert "pinned and live" in block
    assert "forgotten" not in block
    assert "merely stored" not in block


def test_briefing_points_at_the_rest_of_the_store_without_listing_it() -> None:
    block = compose_briefing(
        [_memory(id="a", text="pinned fact", pinned=True)],
        budget_chars=600,
        max_memories=8,
        unpinned_counts={"project": 34, "preference": 12, "fact": 8},
    )
    assert INDEX_PREFIX in block
    assert "34 project" in block
    assert "recall" in block


def test_empty_memory_produces_no_block() -> None:
    assert compose_briefing([], budget_chars=600, max_memories=8) == ""


async def test_briefing_size_is_independent_of_store_size(db: Database) -> None:
    """The invariant this whole design exists for.

    500 memories, 12 of them pinned, and what reaches the prompt is still a
    handful of lines under the character budget. A briefing that grew with the
    store would be a context tax charged on every turn of the session.
    """
    config = MemoryConfig(
        max_memories=520, max_pinned=12, injection_budget_chars=600, injection_max_memories=8
    )
    store = MemoryStore(db, config=config)
    kinds = list(MemoryKind)
    for i in range(500):
        await store.remember(
            f"stored fact number {i} about the way this operator works",
            kind=kinds[i % len(kinds)],
            pinned=i < 12,
        )
    assert await store.count_active() == 500

    small = compose_briefing(
        await store.for_briefing(),
        budget_chars=config.injection_budget_chars,
        max_memories=config.injection_max_memories,
        unpinned_counts=await store.unpinned_counts_by_kind(),
    )
    assert len(small) <= config.injection_budget_chars
    # heading + at most 8 memories + one index line
    assert len(small.splitlines()) <= 1 + config.injection_max_memories + 1
    assert INDEX_PREFIX in small


# --- rollover --------------------------------------------------------------


def test_rollover_fires_at_the_turn_threshold_and_not_just_under() -> None:
    config = MemoryConfig(session_max_turns=500, session_max_age_hours=168)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    started = now - timedelta(hours=1)

    assert not should_roll(started_at=started, turn_count=499, now=now, config=config).roll
    assert should_roll(started_at=started, turn_count=500, now=now, config=config).roll


def test_rollover_fires_at_the_age_threshold_and_not_just_under() -> None:
    config = MemoryConfig(session_max_turns=500, session_max_age_hours=168)
    now = datetime(2026, 6, 1, tzinfo=UTC)

    just_under = now - timedelta(hours=168) + timedelta(minutes=1)
    assert not should_roll(started_at=just_under, turn_count=1, now=now, config=config).roll

    at_limit = now - timedelta(hours=168)
    decision = should_roll(started_at=at_limit, turn_count=1, now=now, config=config)
    assert decision.roll
    assert "168" in decision.reason


def test_rollover_can_be_switched_off_entirely() -> None:
    config = MemoryConfig(session_max_turns=0, session_max_age_hours=0)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    started = now - timedelta(days=365)
    assert not should_roll(started_at=started, turn_count=10_000, now=now, config=config).roll


# --- wiring ----------------------------------------------------------------


async def _session(
    db: Database,
    tmp_path: Path,
    bus: EventBus,
    store: MemoryStore,
    config: NomadConfig,
) -> AgentSession:
    return AgentSession(
        config=config,
        bus=bus,
        conversations=ConversationsRepository(db),
        grants=GrantsRepository(db),
        targets=TargetRegistry(),
        tools=ToolRegistry(),
        workspace=Workspace(tmp_path / "workspace"),
        memory=store,
        resume_pending=False,
    )


async def test_a_roll_seeds_the_new_backend_with_a_briefing_and_no_resume(
    db: Database,
    tmp_path: Path,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def recording_create_backend(config, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return MockBackend()

    monkeypatch.setattr("nomad.agent.session.create_backend", recording_create_backend)

    store = MemoryStore(db)
    await store.remember("Lucas is building Nomad", kind=MemoryKind.PROJECT, pinned=True)

    config = NomadConfig()
    # Roll on every turn boundary after the first.
    config.memory.session_max_turns = 1

    session = await _session(db, tmp_path, event_bus, store, config)
    await session.start()
    try:
        assert calls[-1]["briefing"].startswith(HEADING)

        await session.send("first")
        before = len(calls)
        await session.send("second")
        assert len(calls) == before + 1

        rolled = calls[-1]
        assert rolled["resume_session_id"] is None
        assert rolled["briefing"] != ""
        assert "Lucas is building Nomad" in rolled["briefing"]
    finally:
        await session.stop()


async def test_a_session_without_memory_gets_no_briefing_and_never_rolls(
    db: Database,
    tmp_path: Path,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def recording_create_backend(config, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return MockBackend()

    monkeypatch.setattr("nomad.agent.session.create_backend", recording_create_backend)

    config = NomadConfig()
    config.memory.session_max_turns = 1
    session = AgentSession(
        config=config,
        bus=event_bus,
        conversations=ConversationsRepository(db),
        grants=GrantsRepository(db),
        targets=TargetRegistry(),
        tools=ToolRegistry(),
        workspace=Workspace(tmp_path / "workspace"),
        resume_pending=False,
    )
    await session.start()
    try:
        await session.send("one")
        await session.send("two")
        assert len(calls) == 1
        assert calls[0]["briefing"] == ""
    finally:
        await session.stop()


def test_the_briefing_reaches_the_claude_backend_identity_append() -> None:
    """The briefing must land in the system prompt, not merely in a variable."""
    sdk = pytest.importorskip("claude_agent_sdk")
    assert sdk is not None

    from nomad.agent.backends import create_backend
    from nomad.core.config import AgentBackendKind

    config = NomadConfig()
    config.agent.backend = AgentBackendKind.CLAUDE_CLI

    class _Bridge:
        async def can_use_tool(self, *args, **kwargs):  # pragma: no cover - never called
            raise AssertionError

    briefing = compose_briefing(
        [_memory(id="a", text="Lucas prefers terse answers", pinned=True)],
        budget_chars=600,
        max_memories=8,
    )
    backend = create_backend(config, bridge=_Bridge(), briefing=briefing)
    prompt = backend._system_prompt()  # type: ignore[attr-defined]
    assert prompt["preset"] == "claude_code"
    assert "Lucas prefers terse answers" in prompt["append"]
    assert prompt["append"].index("You are Nomad") < prompt["append"].index(HEADING)


# --- normalization ---------------------------------------------------------


def test_normalize_text_folds_case_whitespace_and_trailing_punctuation() -> None:
    assert normalize_text("  He   Prefers  vim!! ") == "he prefers vim"
    assert normalize_text("") == ""
