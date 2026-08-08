"""Chunk O: the offline tier, and the two things it must never do.

Three families of test. The ordinary ones cover routing, evidence and
promotion. The other two are the reason this file is long:

* **It must never mis-read an intent.** A wrong answer with no round trip is
  invisible to the operator, so the router either knows or defers. That is
  asserted behaviourally *and* structurally — there is a test that fails if
  anyone adds a tunable cutoff, because the cutoff is the failure mode.
* **It must never execute without a grant.** "Onboard" describes where a call
  runs, never whether it was allowed (D4, D21). A structural scan asserts that
  nothing in the package can reach a tool except through `ToolExecutor`.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from nomad.core.config import NomadConfig, OfflineConfig, PermissionMode
from nomad.core.events import EventBus
from nomad.core.logging import get_logger
from nomad.mcp.hardware import MockBattery, ReadBatteryTool
from nomad.mcp.offline import (
    OfflineStatusParams,
    OfflineStatusTool,
    OfflineView,
    build_offline_tools,
)
from nomad.mcp.utilities import SetTimerTool, WorldClockTool
from nomad.notifications.queue import NotificationQueue
from nomad.offline import (
    DEFAULT_INTENTS,
    Intent,
    IntentLedger,
    IntentMatch,
    IntentRouter,
    OfflineError,
    OfflineResponder,
    PromotionAnalyst,
    PromotionForm,
    PromotionState,
    default_router,
    normalize,
    render_skill_draft,
)
from nomad.offline.intents import SLOT_TEXT, SLOT_VALUE, SlotKind
from nomad.resources.clock import ManualClock
from nomad.resources.workload import Tier, YieldContext
from nomad.storage.db import Database
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets import LocalTarget, TargetRegistry
from nomad.tools.base import Risk, ToolContext, ToolResult, ToolSpec
from nomad.tools.permissions import PermissionBroker, ToolExecutor
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

SRC = Path(__file__).resolve().parent.parent / "src" / "nomad"
OFFLINE_SOURCES = sorted((SRC / "offline").rglob("*.py")) + [SRC / "mcp" / "offline.py"]

SESSION_ID = "session-offline"


# -- helpers ----------------------------------------------------------------


class Ticker:
    """A wall clock a test moves by hand. No test here waits for real time."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 8, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


class SpyParams(BaseModel):
    text: str = "x"


class NeverAutoSpyTool:
    """Declared `never_auto`, and it records any execution that reaches it.

    The point of the recording is that the assertion can be "it never ran"
    rather than "the outcome said it did not run" — an offline fast path that
    executed and then reported honestly would still be the bug.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    spec = ToolSpec(
        name="spy_never_auto",
        description="Test-only tool that may never be auto-approved.",
        params_model=SpyParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
        never_auto=True,
    )

    async def execute(self, params: SpyParams, ctx: ToolContext) -> ToolResult:
        self.calls.append(params.text)
        return ToolResult.success("ran")


class Pipeline:
    def __init__(
        self,
        *,
        broker: PermissionBroker,
        executor: ToolExecutor,
        grants: GrantsRepository,
        tools: ToolRegistry,
        spy: NeverAutoSpyTool,
        queue: NotificationQueue,
    ) -> None:
        self.broker = broker
        self.executor = executor
        self.grants = grants
        self.tools = tools
        self.spy = spy
        self.queue = queue


@pytest.fixture
async def pipeline(db: Database, event_bus: EventBus, tmp_path: Path) -> Pipeline:
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "workspace"
    root.mkdir()
    config = NomadConfig.model_validate({"workspace": {"root": str(root)}})
    workspace = Workspace(root)
    workspace.ensure_exists()

    queue = NotificationQueue(db, clock=Ticker())
    spy = NeverAutoSpyTool()
    tools = ToolRegistry()
    tools.register(ReadBatteryTool(MockBattery()))
    tools.register(WorldClockTool(clock=Ticker()))
    tools.register(SetTimerTool(queue))
    tools.register(spy)

    targets = TargetRegistry()
    targets.register(LocalTarget())
    grants = GrantsRepository(db)
    broker = PermissionBroker(
        tools=tools,
        targets=targets,
        workspace=workspace,
        grants=grants,
        bus=event_bus,
        config=config,
    )
    executor = ToolExecutor(
        tools=tools,
        targets=targets,
        workspace=workspace,
        grants=grants,
        bus=event_bus,
        config=config,
    )
    return Pipeline(
        broker=broker, executor=executor, grants=grants, tools=tools, spy=spy, queue=queue
    )


def responder(pipeline: Pipeline, *, router: IntentRouter | None = None, ledger=None):
    return OfflineResponder(
        router=router or default_router(),
        broker=pipeline.broker,
        executor=pipeline.executor,
        ledger=ledger,
    )


async def ask(pipeline: Pipeline, text: str, **kwargs):
    return await responder(pipeline, **kwargs).handle(
        text, session_id=SESSION_ID, mode=PermissionMode.MANUAL
    )


@pytest.fixture
async def ledger(db: Database) -> IntentLedger:
    return IntentLedger(db, config=OfflineConfig(), clock=Ticker())


# -- normalization and exact matching ---------------------------------------


@pytest.mark.parametrize(
    ("spoken", "intent"),
    [
        ("what's my battery", "battery"),
        ("What's my battery?", "battery"),
        ("WHATS MY BATTERY", "battery"),
        ("  whats   my  battery  ", "battery"),
        ("battery level.", "battery"),
        ("what time is it", "ambient_context"),
        ("what's pending", "pending_notifications"),
        ("start a stopwatch", "start_stopwatch"),
    ],
)
def test_a_phrase_in_the_catalog_routes_exactly(spoken: str, intent: str) -> None:
    match = default_router().match(spoken)
    assert match is not None and match.intent == intent


@pytest.mark.parametrize(
    "spoken",
    [
        "",
        "   ",
        "batery",  # a typo is not a phrase; no edit distance exists to save it
        "battery please",
        "how is the battery doing today",
        "tell me about my battery",
        "what's my battery and then wipe the disk",  # trailing intent, not a suffix
        "and what's my battery",
        "what time is it in",  # a template with an empty slot
        "set a timer",
    ],
)
def test_anything_short_of_the_phrase_goes_to_the_model(spoken: str) -> None:
    """The whole design in one test: not-quite-matching is not matching."""
    assert default_router().match(spoken) is None


@pytest.mark.parametrize(
    ("spoken", "seconds"),
    [
        ("set a timer for ten minutes", 600.0),
        ("set a timer for 10 minutes", 600.0),
        ("timer for 90 seconds", 90.0),
        ("set timer for 1 hour", 3600.0),
        ("wake me in 20 mins", 1200.0),
        ("set a timer for an hour", 3600.0),
    ],
)
def test_a_duration_slot_parses_under_a_total_grammar(spoken: str, seconds: float) -> None:
    match = default_router().match(spoken)
    assert match is not None
    assert match.tool == "set_timer"
    assert match.params["seconds"] == seconds
    assert match.params["label"], "the label carries the spoken duration"


@pytest.mark.parametrize(
    "spoken",
    [
        "set a timer for a while",
        "set a timer for twenty five minutes",  # compounds are deliberately absent
        "set a timer for ten",
        "set a timer for lots of minutes",
        "set a timer for 100 hours",  # past the ceiling the tool itself enforces
        "set a timer for 0 minutes",
    ],
)
def test_an_unparseable_duration_is_a_round_trip_not_a_guess(spoken: str) -> None:
    assert default_router().match(spoken) is None


def test_a_zone_slot_is_passed_through_for_the_tool_to_judge() -> None:
    match = default_router().match("what time is it in tokyo")
    assert match is not None
    assert match.tool == "world_clock"
    assert match.params == {"zone": "tokyo"}


def test_a_zone_slot_that_is_a_sentence_is_refused() -> None:
    assert default_router().match("what time is it in the place i went last year") is None


def test_two_intents_claiming_one_utterance_route_to_neither() -> None:
    """Ambiguity hands over rather than ranking. Ranking is a cutoff in a hat."""
    router = IntentRouter(
        (
            Intent(name="a", tool="tool_a", phrases=("do the thing",)),
            Intent(name="b", tool="tool_b", phrases=("do the thing",)),
        )
    )
    assert router.match("do the thing") is None


def test_a_template_and_a_phrase_that_both_match_route_to_neither() -> None:
    router = IntentRouter(
        (
            Intent(name="exact", tool="tool_a", phrases=("time in london",)),
            Intent(
                name="slotted",
                tool="tool_b",
                templates=("time in {}",),
                slot=SlotKind.ZONE,
                params={"zone": SLOT_TEXT},
            ),
        )
    )
    assert router.match("time in london") is None


def test_normalization_is_the_same_transformation_both_sides() -> None:
    assert normalize("What's  my BATTERY?!") == "whats my battery"
    assert normalize("") == ""


# -- no dial, and nothing that could become one ------------------------------


def test_a_match_carries_no_confidence_to_compare_against_anything() -> None:
    """A number here would need a number elsewhere to be compared to."""
    fields = set(IntentMatch.model_fields)
    assert fields == {"intent", "tool", "params", "matched", "normalized"}


BANNED_SUBSTRINGS = (
    "threshold",
    "confidence",
    "fuzzy",
    "similarity",
    "levenshtein",
    "closest",
    "approximate",
)
BANNED_IMPORTS = {"difflib", "rapidfuzz", "Levenshtein", "fuzzywuzzy", "numpy"}


def test_no_identifier_in_the_offline_package_is_a_matching_dial() -> None:
    """The failure mode this tier exists to avoid is a cutoff someone raises.

    Checked in code position only, so the module docstrings above are free to
    argue the case at length — which is where the argument belongs. Verified by
    adding a `match_threshold` parameter and watching this fail.
    """
    offenders: list[str] = []
    for path in OFFLINE_SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.arg):
                names = [node.arg]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names = [node.name]
            elif isinstance(node, ast.keyword) and node.arg:
                names = [node.arg]
            for name in names:
                lowered = name.lower()
                if any(banned in lowered for banned in BANNED_SUBSTRINGS):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], (
        "The offline router matches exactly or defers to the model. A tunable "
        f"would be turned up the first time a phrase missed. Found: {offenders}"
    )


def test_the_offline_package_imports_nothing_that_measures_similarity() -> None:
    offenders: list[str] = []
    for path in OFFLINE_SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}: {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in BANNED_IMPORTS
                ]
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module.split(".")[0] in BANNED_IMPORTS
            ):
                offenders.append(f"{path.name}: {node.module}")
    assert offenders == [], f"similarity library imported: {offenders}"


# -- the broker is in the path (D4, D21) -------------------------------------


async def test_a_matched_intent_runs_through_the_broker_and_leaves_a_grant(
    pipeline: Pipeline,
) -> None:
    outcome = await ask(pipeline, "what's my battery")
    assert outcome.handled is True
    assert outcome.tool == "read_battery"
    assert "battery" in outcome.content
    assert outcome.grant_id is not None

    persisted = await pipeline.grants.list_grants(SESSION_ID)
    assert [g.tool for g in persisted] == ["read_battery"], (
        "an offline answer must be as auditable as any other tool call"
    )
    assert persisted[0].id == outcome.grant_id


async def test_a_never_auto_intent_is_not_answered_offline_and_never_runs(
    pipeline: Pipeline,
) -> None:
    """The load-bearing one. `never_auto` must survive being reached offline."""
    router = IntentRouter(
        (Intent(name="spy", tool="spy_never_auto", phrases=("do the forbidden thing",)),)
    )
    outcome = await responder(pipeline, router=router).handle(
        "do the forbidden thing", session_id=SESSION_ID, mode=PermissionMode.MANUAL
    )
    assert outcome.handled is False
    assert "not authorized" in outcome.reason
    assert pipeline.spy.calls == [], "the offline path executed a never_auto tool"
    assert await pipeline.grants.list_grants(SESSION_ID) == []


async def test_even_auto_mode_cannot_make_the_offline_path_run_never_auto(
    pipeline: Pipeline,
) -> None:
    router = IntentRouter(
        (Intent(name="spy", tool="spy_never_auto", phrases=("do the forbidden thing",)),)
    )
    outcome = await responder(pipeline, router=router).handle(
        "do the forbidden thing", session_id=SESSION_ID, mode=PermissionMode.AUTO
    )
    assert outcome.handled is False
    assert pipeline.spy.calls == []


async def test_an_intent_for_a_tool_this_device_lacks_degrades_to_a_round_trip(
    pipeline: Pipeline,
) -> None:
    router = IntentRouter(
        (Intent(name="ghost", tool="tool_that_does_not_exist", phrases=("ghost",)),)
    )
    outcome = await responder(pipeline, router=router).handle(
        "ghost", session_id=SESSION_ID, mode=PermissionMode.AUTO
    )
    assert outcome.handled is False
    assert "unknown tool" in outcome.reason


async def test_a_tool_that_ran_and_refused_is_not_a_handled_utterance(
    pipeline: Pipeline,
) -> None:
    """It still minted a grant, because it really was a tool call."""
    outcome = await ask(pipeline, "what time is it in atlantis")
    assert outcome.handled is False
    assert "tool failed" in outcome.reason
    assert outcome.grant_id is not None
    assert len(await pipeline.grants.list_grants(SESSION_ID)) == 1


async def test_a_routed_timer_becomes_a_real_durable_notification(
    pipeline: Pipeline,
) -> None:
    outcome = await ask(pipeline, "set a timer for ten minutes")
    assert outcome.handled is True
    pending = await pipeline.queue.undelivered()
    assert [n.title for n in pending] == ["ten minutes"]


def test_nothing_in_the_offline_package_can_execute_a_tool_itself() -> None:
    """An offline fast path around the broker would undo the whole security layer.

    Structural rather than behavioural: a test that only checks outcomes can be
    satisfied by a second code path nobody exercised. Verified by adding a
    direct `tool.execute(...)` call and watching this fail.
    """
    offenders: list[str] = []
    for path in OFFLINE_SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "execute":
                # `Database.execute` is SQL, not a tool call, and the evidence
                # ledger is nothing but SQL. Every other receiver is suspect.
                receiver = func.value
                name = getattr(receiver, "attr", None) or getattr(receiver, "id", None)
                if name not in ("_db", "db"):
                    offenders.append(f"{path.name}: {name}.execute(")
            if isinstance(func, ast.Name) and func.id == "AuthorizationGrant":
                offenders.append(f"{path.name}: mints its own grant")
            if isinstance(func, ast.Attribute) and func.attr == "AuthorizationGrant":
                offenders.append(f"{path.name}: mints its own grant")
    assert offenders == [], (
        "The offline tier runs tools through ToolExecutor with a broker-minted "
        f"grant, or not at all (D4). Found: {offenders}"
    )


# -- evidence ----------------------------------------------------------------


async def test_two_spellings_of_one_question_are_one_row(ledger: IntentLedger) -> None:
    await ledger.record("What's my battery?", routed_intent="battery")
    record = await ledger.record("whats my battery", routed_intent="battery")
    assert record is not None
    assert record.ask_count == 2
    assert len(await ledger.top_asks()) == 1


async def test_a_paragraph_is_not_evidence(ledger: IntentLedger) -> None:
    assert await ledger.record("word " * 200, routed_intent=None) is None
    assert await ledger.top_asks() == []


async def test_counts_survive_a_restart(db: Database) -> None:
    """A count that dies with the process cannot argue anything about habits."""
    first = IntentLedger(db, clock=Ticker())
    await first.record("how long until the bread is done", routed_intent=None)
    second = IntentLedger(db, clock=Ticker())
    record = await second.record("how long until the bread is done", routed_intent=None)
    assert record is not None and record.ask_count == 2


async def test_a_candidate_must_be_both_frequent_and_stable(db: Database) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)

    for _ in range(6):
        await led.record("read me the news", routed_intent=None)
    assert await led.candidates() == [], "frequent in one afternoon is an incident"

    clock.advance(days=1)
    await led.record("read me the news", routed_intent=None)
    assert await led.candidates() == []

    clock.advance(days=1)
    await led.record("read me the news", routed_intent=None)
    candidates = await led.candidates()
    assert [c.normalized for c in candidates] == ["read me the news"]
    assert candidates[0].distinct_days == 3


async def test_something_already_answered_onboard_is_not_a_candidate(db: Database) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=2, promote_after_days=2)
    led = IntentLedger(db, config=config, clock=clock)
    for _ in range(3):
        await led.record("what's my battery", routed_intent="battery")
        clock.advance(days=1)
    assert await led.candidates() == []


async def test_the_evidence_table_stays_bounded(db: Database) -> None:
    led = IntentLedger(db, config=OfflineConfig(max_tracked_asks=3), clock=Ticker())
    for i in range(10):
        await led.record(f"phrase number {i}", routed_intent=None)
    assert len(await led.top_asks(limit=50)) == 3


async def test_the_responder_records_a_miss_as_a_candidate(
    pipeline: Pipeline, ledger: IntentLedger
) -> None:
    await ask(pipeline, "how long until the bread is done", ledger=ledger)
    record = await ledger.get(normalize("how long until the bread is done"))
    assert record is not None
    assert record.routed_intent is None, "a miss is what promotion is evidence of"


async def test_the_responder_records_a_hit_as_answered_onboard(
    pipeline: Pipeline, ledger: IntentLedger
) -> None:
    await ask(pipeline, "what's my battery", ledger=ledger)
    record = await ledger.get("whats my battery")
    assert record is not None and record.routed_intent == "battery"


# -- promotion: proposed, never applied --------------------------------------


async def _stack_evidence(led: IntentLedger, clock: Ticker, phrase: str, *, days: int = 3) -> None:
    for _ in range(days):
        for _ in range(2):
            await led.record(phrase, routed_intent=None)
        clock.advance(days=1)


async def test_a_promotion_is_proposed_as_a_skill_never_as_a_tool(db: Database) -> None:
    """D39: a skill carries no capability, so it is a far smaller bet.

    What this analyst holds is a phrase and two integers. That can argue "the
    operator asks this a lot"; it cannot argue a risk level and a permission.
    """
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)
    await _stack_evidence(led, clock, "read me the news")

    proposals = await PromotionAnalyst(led, config=config, clock=clock).analyse_once()
    assert len(proposals) == 1
    assert proposals[0].form is PromotionForm.SKILL
    assert proposals[0].evidence_days == 3
    assert "read me the news" in proposals[0].draft
    assert "grants nothing" in proposals[0].draft


async def test_accepting_a_promotion_changes_nothing_the_operator_did_not_change(
    db: Database,
) -> None:
    """The whole reason this is a proposal: acceptance hands over a draft.

    No file is written, no skill is loaded, and the phrase the device was asked
    about still routes exactly where it did before — to the model.
    """
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)
    await _stack_evidence(led, clock, "read me the news")
    (proposal,) = await PromotionAnalyst(led, config=config, clock=clock).analyse_once()

    router = default_router()
    assert router.match("read me the news") is None
    accepted = await led.accept(proposal.id)
    assert accepted.state is PromotionState.ACCEPTED
    assert accepted.draft == proposal.draft
    assert default_router().match("read me the news") is None, "the device promoted itself"
    assert router.phrase_count() == default_router().phrase_count()


async def test_a_rejected_phrase_is_never_proposed_again(db: Database) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)
    analyst = PromotionAnalyst(led, config=config, clock=clock)
    await _stack_evidence(led, clock, "read me the news")
    (proposal,) = await analyst.analyse_once()
    await led.reject(proposal.id)

    await _stack_evidence(led, clock, "read me the news")
    assert await analyst.analyse_once() == []
    assert await led.candidates() == []


async def test_a_decision_cannot_be_made_twice(db: Database) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)
    await _stack_evidence(led, clock, "read me the news")
    (proposal,) = await PromotionAnalyst(led, config=config, clock=clock).analyse_once()
    await led.accept(proposal.id)
    with pytest.raises(OfflineError):
        await led.reject(proposal.id)
    with pytest.raises(OfflineError):
        await led.accept("no-such-proposal")


async def test_the_operator_is_never_shown_a_backlog(db: Database) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=2, promote_after_days=1, max_open_proposals=2)
    led = IntentLedger(db, config=config, clock=clock)
    for i in range(5):
        for _ in range(3):
            await led.record(f"do interesting thing {i}", routed_intent=None)
    proposals = await PromotionAnalyst(led, config=config, clock=clock).analyse_once()
    assert len(proposals) == 2
    assert await PromotionAnalyst(led, config=config, clock=clock).analyse_once() == []


def test_a_draft_is_readable_before_it_is_installed(db: Database) -> None:
    from nomad.offline.ledger import AskRecord

    record = AskRecord(
        id="x",
        normalized="read me the news",
        sample="Read me the news",
        ask_count=7,
        observed_days=["2026-08-06", "2026-08-07", "2026-08-08"],
        first_asked_at=datetime(2026, 8, 6, tzinfo=UTC),
        last_asked_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    draft = render_skill_draft(record)
    assert draft.startswith("---\n")
    assert "name: offline-read-me-the-news" in draft
    description = [line for line in draft.splitlines() if line.startswith("description:")]
    assert len(description) == 1 and len(description[0]) <= 210


# -- promotion analysis is opportunistic work (D38) --------------------------


def test_promotion_analysis_is_registered_as_preemptible_work(db: Database) -> None:
    """It makes the device better *later*, and later is always negotiable."""
    analyst = PromotionAnalyst(IntentLedger(db))
    assert analyst.tier is Tier.OPPORTUNISTIC
    assert hasattr(analyst, "run"), "the governor needs a body to drive"


async def test_the_analyst_parks_when_the_governor_asks_for_the_machine(
    db: Database,
) -> None:
    clock = ManualClock()
    ctx = YieldContext("offline-promotion-analyst", clock)
    analyst = PromotionAnalyst(IntentLedger(db, clock=Ticker()), config=OfflineConfig())
    task = asyncio.ensure_future(analyst.run(ctx))
    try:
        await clock.wait_for_sleepers(1)
        ctx.request_suspend()
        await asyncio.wait_for(ctx.wait_parked(), timeout=5)
        assert ctx.parked is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# -- the tool surface --------------------------------------------------------


@pytest.fixture
def tool_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        target=LocalTarget(),
        workspace=Workspace(tmp_path),
        session_id=SESSION_ID,
        turn_id=None,
        logger=get_logger("test"),
    )


async def test_offline_status_lists_what_answers_without_a_network(tool_ctx) -> None:
    tool = OfflineStatusTool(default_router())
    result = await tool.execute(OfflineStatusParams(view=OfflineView.ROUTES), tool_ctx)
    assert result.ok is True
    assert "battery -> read_battery" in result.content
    assert result.metadata["count"] == len(DEFAULT_INTENTS)


async def test_offline_status_shows_proposals_and_says_who_decides(
    db: Database, tool_ctx
) -> None:
    clock = Ticker()
    config = OfflineConfig(promote_after_asks=5, promote_after_days=3)
    led = IntentLedger(db, config=config, clock=clock)
    await _stack_evidence(led, clock, "read me the news")
    await PromotionAnalyst(led, config=config, clock=clock).analyse_once()

    tool = OfflineStatusTool(default_router(), ledger=led)
    result = await tool.execute(OfflineStatusParams(view=OfflineView.PROPOSALS), tool_ctx)
    assert result.ok is True
    assert "cannot accept these itself" in result.content


async def test_a_device_with_no_ledger_says_so_rather_than_failing_oddly(tool_ctx) -> None:
    tool = OfflineStatusTool(default_router())
    result = await tool.execute(OfflineStatusParams(view=OfflineView.ASKS), tool_ctx)
    assert result.ok is False
    assert "ledger" in result.error


def test_there_is_no_tool_that_accepts_a_promotion() -> None:
    """Same shape as D36's prompt, D37's missing `listen`, D39's missing installer.

    A model that could propose a change to the device and then approve it has
    made "operator approved" mean nothing.
    """
    names = {tool.spec.name for tool in build_offline_tools(default_router())}
    assert names == {"offline_status"}
    for forbidden in ("accept_promotion", "promote", "install_intent", "add_phrase"):
        assert forbidden not in names


def test_a_device_with_no_router_has_no_offline_tool() -> None:
    assert build_offline_tools(None) == []


def test_reading_the_offline_status_needs_no_permission() -> None:
    spec = OfflineStatusTool(default_router()).spec
    assert spec.risk is Risk.READ_ONLY
    assert spec.permissions == frozenset()
    assert spec.never_auto is False
    assert spec.device_local is True


# -- the catalog itself ------------------------------------------------------


def test_every_shipped_intent_is_well_formed() -> None:
    for intent in DEFAULT_INTENTS:
        assert intent.tool, f"{intent.name} names no tool"
        assert intent.summary, f"{intent.name} has no one-line summary"
        assert intent.phrases or intent.templates
        for template in intent.templates:
            assert template.count("{}") == 1, f"{intent.name}: one slot per template"
            assert intent.slot is not None, f"{intent.name}: a template needs a slot kind"
        if intent.slot is not None:
            assert SLOT_TEXT in intent.params.values() or SLOT_VALUE in intent.params.values()


def test_no_two_shipped_intents_claim_the_same_phrase() -> None:
    """An ambiguous catalog is a catalog that quietly answers nothing."""
    seen: dict[str, str] = {}
    for intent in DEFAULT_INTENTS:
        for phrase in intent.phrases:
            key = normalize(phrase)
            assert key not in seen, f"'{phrase}' is claimed by {seen.get(key)} and {intent.name}"
            seen[key] = intent.name
