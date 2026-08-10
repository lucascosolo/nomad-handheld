"""The permission pipeline (D4, D5, D12, D14, D15).

These tests are the contract. If one of them starts failing, the fix is
almost certainly the code, not the test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import PermissionDenied
from nomad.core.events import Event, EventBus
from nomad.core.paths import nomad_source_root
from nomad.storage.db import Database
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.events import EventsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets import Capability, HidTarget, LocalTarget, SshTarget, TargetRegistry
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec
from nomad.tools.builtin import build_default_registry
from nomad.tools.permissions import (
    SCOPE_NONE,
    SCOPE_OUTSIDE,
    SCOPE_SCRATCH,
    SCOPE_SOURCE_TREE,
    SCOPE_WORKSPACE,
    AuthorizationGrant,
    AuthorizationQueue,
    Classification,
    Decision,
    DecisionOutcome,
    GrantSource,
    PermissionBroker,
    Resolution,
    ToolExecutor,
    ToolRequest,
    compute_scope,
    never_auto_reason,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

SESSION_ID = "session-under-test"


# -- extra tools used to exercise rules the built-ins cannot reach ----------


class PathParams(BaseModel):
    path: str


class ProbeWriteTool:
    """A write tool that is NOT workspace-confined.

    The built-ins all confine themselves, which means they can never reach the
    `never_auto` "writes outside the workspace root" rule — the hard boundary
    denies them first. This tool exists so that rule is actually tested.
    """

    spec = ToolSpec(
        name="probe_write",
        description="Test-only unconfined write.",
        params_model=PathParams,
        risk=Risk.MUTATING,
        permissions=frozenset({Permission.FS_WRITE}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=False,
        path_params=("path",),
    )

    async def execute(self, params: PathParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success(f"probed {params.path}")


class DestroyTool:
    spec = ToolSpec(
        name="destroy",
        description="Test-only destructive action.",
        params_model=PathParams,
        risk=Risk.DESTRUCTIVE,
        permissions=frozenset({Permission.FS_WRITE}),
        required_capabilities=frozenset({Capability.FILESYSTEM}),
        workspace_confined=True,
        path_params=("path",),
    )

    async def execute(self, params: PathParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("destroyed")


class TypeTextParams(BaseModel):
    text: str


class TypeTextTool:
    spec = ToolSpec(
        name="type_text",
        description="Test-only HID output.",
        params_model=TypeTextParams,
        risk=Risk.EXTERNAL_DEVICE,
        permissions=frozenset({Permission.HID_OUTPUT}),
        required_capabilities=frozenset({Capability.HID_OUTPUT}),
    )

    async def execute(self, params: TypeTextParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("typed")


class DrawTool:
    """Stands in for `display_card`: mutating, but only to Nomad's own face."""

    spec = ToolSpec(
        name="draw",
        description="Test-only device-local draw.",
        params_model=TypeTextParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
        device_local=True,
    )

    async def execute(self, params: TypeTextParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("drew")


class DeviceLocalHidTool:
    """A deliberately wrong declaration: `device_local` on a tool that types
    on another machine. The point is that the broker does not believe it."""

    spec = ToolSpec(
        name="draw_but_hid",
        description="Test-only mis-declaration.",
        params_model=TypeTextParams,
        risk=Risk.EXTERNAL_DEVICE,
        permissions=frozenset({Permission.HID_OUTPUT}),
        required_capabilities=frozenset({Capability.HID_OUTPUT}),
        device_local=True,
    )

    async def execute(self, params: TypeTextParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("typed")


class DeviceLocalDestroyTool:
    spec = ToolSpec(
        name="wipe_memory",
        description="Test-only destructive device-local action.",
        params_model=TypeTextParams,
        risk=Risk.DESTRUCTIVE,
        permissions=frozenset(),
        required_capabilities=frozenset(),
        device_local=True,
    )

    async def execute(self, params: TypeTextParams, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("wiped")


# -- fixtures ---------------------------------------------------------------


@dataclass
class Pipeline:
    config: NomadConfig
    workspace: Workspace
    tools: ToolRegistry
    targets: TargetRegistry
    grants: GrantsRepository
    broker: PermissionBroker
    executor: ToolExecutor
    bus: EventBus
    db: Database

    def request(self, tool: str, target: str = "local", **params: object) -> ToolRequest:
        return ToolRequest(
            tool=tool, target_id=target, params=params, session_id=SESSION_ID, turn_id=None
        )


def _make_pipeline(
    db: Database,
    bus: EventBus,
    root: Path,
    *,
    enable_run_command: bool = False,
    classifier: object = None,
    allowed_commands: list[str] | None = None,
) -> Pipeline:
    config = NomadConfig.model_validate(
        {
            "workspace": {"root": str(root)},
            "tools": {
                "enable_run_command": enable_run_command,
                "allowed_commands": allowed_commands or [],
            },
        }
    )
    workspace = Workspace(root)
    workspace.ensure_exists()

    tools = build_default_registry(config)
    tools.register(ProbeWriteTool())
    tools.register(DestroyTool())
    tools.register(TypeTextTool())
    tools.register(DrawTool())
    tools.register(DeviceLocalHidTool())
    tools.register(DeviceLocalDestroyTool())

    targets = TargetRegistry()
    targets.register(LocalTarget())
    targets.register(HidTarget())
    targets.register(SshTarget(alias="ws", host="10.0.0.5", user="lucas"))

    grants = GrantsRepository(db)
    broker = PermissionBroker(
        tools=tools,
        targets=targets,
        workspace=workspace,
        grants=grants,
        bus=bus,
        config=config,
        classifier=classifier,  # type: ignore[arg-type]
        classifier_timeout=0.2,
    )
    executor = ToolExecutor(
        tools=tools, targets=targets, workspace=workspace, grants=grants, bus=bus, config=config
    )
    return Pipeline(
        config=config,
        workspace=workspace,
        tools=tools,
        targets=targets,
        grants=grants,
        broker=broker,
        executor=executor,
        bus=bus,
        db=db,
    )


@pytest.fixture
async def pipeline(db: Database, event_bus: EventBus, tmp_path: Path) -> Pipeline:
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("hello\n")
    return _make_pipeline(db, event_bus, root)


# -- capability ordering (D12) ---------------------------------------------


async def test_capability_check_rejects_a_filesystem_tool_at_a_hid_target(
    pipeline: Pipeline,
) -> None:
    """Runs in AUTO mode: proving the refusal is structural, not a mode policy."""
    decision = await pipeline.broker.decide(
        pipeline.request("read_file", target="hid", path="notes.md"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.DENY
    assert "lacks capability" in decision.reason
    # A capability failure is a hard denial, never a prompt a human could clear.
    assert decision.outcome is not DecisionOutcome.NEEDS_AUTH


async def test_capability_check_runs_before_the_workspace_check(pipeline: Pipeline) -> None:
    """A HID target with an escaping path fails on capabilities, not on the path."""
    decision = await pipeline.broker.decide(
        pipeline.request("write_file", target="hid", path="../out.txt", content="x"),
        PermissionMode.AUTO,
    )
    assert decision.outcome is DecisionOutcome.DENY
    assert "lacks capability" in decision.reason


async def test_hid_tool_at_a_local_target_is_also_rejected(pipeline: Pipeline) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("type_text", target="local", text="rm -rf /"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.DENY


# -- unknown / disabled ----------------------------------------------------


async def test_unknown_tool_and_target_are_denied(pipeline: Pipeline) -> None:
    unknown_tool = await pipeline.broker.decide(pipeline.request("nope"), PermissionMode.AUTO)
    assert unknown_tool.outcome is DecisionOutcome.DENY

    unknown_target = await pipeline.broker.decide(
        pipeline.request("read_file", target="ssh:missing", path="notes.md"), PermissionMode.AUTO
    )
    assert unknown_target.outcome is DecisionOutcome.DENY


async def test_invalid_params_are_denied_not_executed(pipeline: Pipeline) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("read_file", wrong="notes.md"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.DENY
    assert "invalid parameters" in decision.reason


async def test_run_command_is_denied_while_disabled(pipeline: Pipeline) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="ls"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.DENY
    assert "disabled in configuration" in decision.reason


# -- never_auto overrides every mode (D14) ---------------------------------


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_run_command_never_auto_runs_in_any_mode(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _make_pipeline(
        db,
        event_bus,
        root,
        enable_run_command=True,
        classifier=_always_safe,
    )
    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="rm -rf /", cwd="."), mode
    )
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True
    assert "never_auto" in decision.reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_any_ssh_action_never_auto_runs_in_any_mode(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("read_file", target="ssh:ws", path="notes.md"), mode
    )
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True
    assert "SSH" in decision.reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_any_hid_output_never_auto_runs_in_any_mode(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("type_text", target="hid", text="hello"), mode
    )
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True
    assert "HID" in decision.reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_destructive_never_auto_runs_in_any_mode(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    decision = await pipeline.broker.decide(pipeline.request("destroy", path="notes.md"), mode)
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_write_outside_the_workspace_never_auto_runs_in_any_mode(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("probe_write", path="/etc/hosts"), mode
    )
    assert decision.scope == SCOPE_OUTSIDE
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


async def test_never_auto_reason_is_a_single_pure_function() -> None:
    """The rule set lives in one place so a new mode cannot route around it."""
    local, hid = LocalTarget(), HidTarget()
    ssh = SshTarget(alias="ws", host="h", user="u")
    assert never_auto_reason(ProbeWriteTool.spec, local, SCOPE_WORKSPACE) is None
    assert never_auto_reason(ProbeWriteTool.spec, local, SCOPE_OUTSIDE) is not None
    assert never_auto_reason(ProbeWriteTool.spec, ssh, SCOPE_WORKSPACE) is not None
    assert never_auto_reason(TypeTextTool.spec, hid, SCOPE_NONE) is not None
    assert never_auto_reason(DestroyTool.spec, local, SCOPE_WORKSPACE) is not None


# -- the hard workspace boundary (D15) -------------------------------------


async def test_confined_tool_writing_outside_the_root_is_denied_outright(
    pipeline: Pipeline,
) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("write_file", path="../escape.txt", content="x"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.DENY
    assert "outside the workspace root" in decision.reason


async def test_read_only_inside_the_workspace_auto_runs_in_manual_mode(
    pipeline: Pipeline,
) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("read_file", path="notes.md"), PermissionMode.MANUAL
    )
    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.scope == SCOPE_WORKSPACE
    assert decision.grant_source is GrantSource.AUTO


async def test_system_info_auto_runs_in_manual_mode(pipeline: Pipeline) -> None:
    """The vertical slice: READ_ONLY, no prompt, in every mode."""
    decision = await pipeline.broker.decide(
        pipeline.request("get_system_info"), PermissionMode.MANUAL
    )
    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.scope == SCOPE_NONE


async def test_mutating_action_prompts_in_manual_mode(pipeline: Pipeline) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("write_file", path="new.txt", content="x"), PermissionMode.MANUAL
    )
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is False


async def test_mutating_action_auto_runs_in_auto_mode(pipeline: Pipeline) -> None:
    decision = await pipeline.broker.decide(
        pipeline.request("write_file", path="new.txt", content="x"), PermissionMode.AUTO
    )
    assert decision.outcome is DecisionOutcome.ALLOW


# -- Nomad's own screen and memory are not the world (D35) -----------------


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_drawing_on_its_own_screen_never_prompts(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    """The deadlock this closes: `display_card` is MUTATING, so in `manual`
    the device parked 300s waiting for a prompt it could only have drawn with
    a display tool. It could not ask permission to ask permission."""
    decision = await pipeline.broker.decide(pipeline.request("draw", text="hi"), mode)
    assert decision.outcome is DecisionOutcome.ALLOW


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_device_local_never_beats_never_auto(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    """A mis-declared `device_local` on an HID tool must not buy anything.
    The flag widens harmless local actions; it is not an override."""
    decision = await pipeline.broker.decide(
        pipeline.request("draw_but_hid", target="hid", text="rm -rf /"), mode
    )
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_device_local_does_not_cover_destructive(
    pipeline: Pipeline, mode: PermissionMode
) -> None:
    decision = await pipeline.broker.decide(pipeline.request("wipe_memory", text="all"), mode)
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


# -- session grants are keyed on (tool, target, scope) (D14) ---------------


async def _grant_session(pipeline: Pipeline, request: ToolRequest) -> AuthorizationGrant:
    decision = await pipeline.broker.decide(request, PermissionMode.SESSION)
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    return await pipeline.broker.mint_grant(request, decision, source=GrantSource.SESSION)


async def test_a_session_grant_stops_the_broker_asking_again(pipeline: Pipeline) -> None:
    first = pipeline.request("write_file", path="a.txt", content="x")
    grant = await _grant_session(pipeline, first)

    second = pipeline.request("write_file", path="b.txt", content="y")
    decision = await pipeline.broker.decide(second, PermissionMode.SESSION)
    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.existing_grant_id == grant.id


async def test_a_workspace_session_grant_does_not_cover_outside_the_workspace(
    pipeline: Pipeline,
) -> None:
    """The load-bearing half of D14's grant key."""
    inside = pipeline.request("probe_write", path="inside.txt")
    await _grant_session(pipeline, inside)

    outside = pipeline.request("probe_write", path="/tmp/outside.txt")
    decision = await pipeline.broker.decide(outside, PermissionMode.SESSION)
    assert decision.scope == SCOPE_OUTSIDE
    assert decision.outcome is DecisionOutcome.NEEDS_AUTH


async def test_a_session_grant_does_not_cover_another_tool_or_target(
    pipeline: Pipeline,
) -> None:
    await _grant_session(pipeline, pipeline.request("write_file", path="a.txt", content="x"))

    other_tool = await pipeline.broker.decide(
        pipeline.request("probe_write", path="a.txt"), PermissionMode.SESSION
    )
    assert other_tool.outcome is DecisionOutcome.NEEDS_AUTH

    other_target = await pipeline.broker.decide(
        pipeline.request("write_file", target="ssh:ws", path="a.txt", content="x"),
        PermissionMode.SESSION,
    )
    assert other_target.outcome is DecisionOutcome.NEEDS_AUTH


# -- minting rules ---------------------------------------------------------


async def test_a_never_auto_action_cannot_be_auto_granted(pipeline: Pipeline) -> None:
    """Second, independent enforcement of D14 — belt and braces."""
    request = pipeline.request("type_text", target="hid", text="x")
    decision = await pipeline.broker.decide(request, PermissionMode.AUTO)
    for source in (GrantSource.AUTO, GrantSource.MODEL):
        with pytest.raises(PermissionDenied, match="never_auto"):
            await pipeline.broker.mint_grant(request, decision, source=source)
    # A human can still approve it — never_auto means never *automatic*.
    grant = await pipeline.broker.mint_grant(request, decision, source=GrantSource.HUMAN)
    assert grant.source is GrantSource.HUMAN


async def test_a_denied_decision_can_never_be_granted(pipeline: Pipeline) -> None:
    request = pipeline.request("write_file", path="../escape.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.AUTO)
    for source in GrantSource:
        with pytest.raises(PermissionDenied, match="denied decision"):
            await pipeline.broker.mint_grant(request, decision, source=source)


async def test_authorize_refuses_anything_that_is_not_an_allow(pipeline: Pipeline) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    with pytest.raises(PermissionDenied):
        await pipeline.broker.authorize(request, decision)


async def test_a_session_grant_is_reused_not_reminted(pipeline: Pipeline) -> None:
    first = pipeline.request("write_file", path="a.txt", content="x")
    grant = await _grant_session(pipeline, first)
    second = pipeline.request("write_file", path="b.txt", content="y")
    decision = await pipeline.broker.decide(second, PermissionMode.SESSION)
    reused = await pipeline.broker.authorize(second, decision)
    assert reused.id == grant.id


# -- smart mode fails closed (D14) -----------------------------------------


async def _always_safe(request: ToolRequest, spec: ToolSpec) -> Classification:
    return Classification(safe=True, confidence=0.99, reason="benign")


async def _always_flag(request: ToolRequest, spec: ToolSpec) -> Classification:
    return Classification(safe=False, confidence=0.99, reason="looks dangerous")


async def _unsure(request: ToolRequest, spec: ToolSpec) -> Classification:
    return Classification(safe=True, confidence=0.3, reason="not sure")


async def _explodes(request: ToolRequest, spec: ToolSpec) -> Classification:
    raise RuntimeError("model unavailable")


async def _hangs(request: ToolRequest, spec: ToolSpec) -> Classification:
    await asyncio.sleep(10)
    raise AssertionError("unreachable")


async def _nonsense(request: ToolRequest, spec: ToolSpec) -> Classification:
    return "yes, fine"  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("classifier", "expected"),
    [
        (_always_safe, DecisionOutcome.ALLOW),
        (_always_flag, DecisionOutcome.NEEDS_AUTH),
        (_unsure, DecisionOutcome.NEEDS_AUTH),
        (_explodes, DecisionOutcome.NEEDS_AUTH),
        (_hangs, DecisionOutcome.NEEDS_AUTH),
        (_nonsense, DecisionOutcome.NEEDS_AUTH),
        (None, DecisionOutcome.NEEDS_AUTH),
    ],
)
async def test_smart_mode_escalates_on_anything_but_a_confident_yes(
    db: Database,
    event_bus: EventBus,
    tmp_path: Path,
    classifier: object,
    expected: DecisionOutcome,
) -> None:
    await ConversationsRepository(db).create_session(mode="smart", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _make_pipeline(db, event_bus, root, classifier=classifier)
    decision = await pipeline.broker.decide(
        pipeline.request("write_file", path="a.txt", content="x"), PermissionMode.SMART
    )
    assert decision.outcome is expected
    if expected is DecisionOutcome.ALLOW:
        assert decision.grant_source is GrantSource.MODEL


# -- the executor takes a grant, never a request (D4) ----------------------


def test_the_executor_exposes_exactly_one_entry_point() -> None:
    """There is deliberately no `execute(request)` convenience method."""
    public = sorted(name for name in vars(ToolExecutor) if not name.startswith("_"))
    assert public == ["run"]


async def _allow_and_grant(
    pipeline: Pipeline, request: ToolRequest, mode: PermissionMode = PermissionMode.AUTO
) -> AuthorizationGrant:
    decision = await pipeline.broker.decide(request, mode)
    assert decision.outcome is DecisionOutcome.ALLOW, decision.reason
    return await pipeline.broker.authorize(request, decision)


async def test_a_granted_call_executes(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", path="notes.md")
    grant = await _allow_and_grant(pipeline, request)
    result = await pipeline.executor.run(grant, request)
    assert result.ok
    assert "hello" in result.content


async def test_execution_without_a_grant_fails(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", path="notes.md")
    with pytest.raises(PermissionDenied, match="requires an AuthorizationGrant"):
        await pipeline.executor.run(None, request)  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied, match="requires an AuthorizationGrant"):
        await pipeline.executor.run({"tool": "read_file"}, request)  # type: ignore[arg-type]


async def test_a_grant_for_another_tool_or_target_is_refused(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", path="notes.md")
    grant = await _allow_and_grant(pipeline, request)

    other_tool = grant.model_copy(update={"tool": "list_dir"})
    with pytest.raises(PermissionDenied, match="different tool"):
        await pipeline.executor.run(other_tool, request)

    other_target = grant.model_copy(update={"target": "ssh:ws"})
    with pytest.raises(PermissionDenied, match="different target"):
        await pipeline.executor.run(other_target, request)

    other_session = grant.model_copy(update={"session_id": "somebody-else"})
    with pytest.raises(PermissionDenied, match="different session"):
        await pipeline.executor.run(other_session, request)


async def test_an_expired_grant_is_refused(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", path="notes.md")
    grant = await _allow_and_grant(pipeline, request)
    stale = grant.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(PermissionDenied, match="expired"):
        await pipeline.executor.run(stale, request)


async def test_a_one_shot_grant_cannot_be_replayed(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", path="notes.md")
    grant = await _allow_and_grant(pipeline, request)
    assert grant.source is GrantSource.AUTO and grant.reusable is False

    assert (await pipeline.executor.run(grant, request)).ok
    with pytest.raises(PermissionDenied, match="already been used"):
        await pipeline.executor.run(grant, request)


async def test_a_session_grant_may_be_used_repeatedly(pipeline: Pipeline) -> None:
    first = pipeline.request("write_file", path="a.txt", content="x")
    grant = await _grant_session(pipeline, first)
    assert grant.reusable is True

    second = pipeline.request("write_file", path="b.txt", content="y")
    assert (await pipeline.executor.run(grant, first)).ok
    assert (await pipeline.executor.run(grant, second)).ok


async def test_a_grant_cannot_be_replayed_against_a_different_scope(
    pipeline: Pipeline,
) -> None:
    """The reason the executor recomputes the scope instead of trusting it.

    Approving a write inside the workspace must not become a write outside it
    just because the parameters changed after approval.
    """
    inside = pipeline.request("probe_write", path="inside.txt")
    grant = await _allow_and_grant(pipeline, inside)
    assert grant.scope == SCOPE_WORKSPACE

    outside = pipeline.request("probe_write", path="/etc/hosts")
    with pytest.raises(PermissionDenied, match="scope changed"):
        await pipeline.executor.run(grant, outside)


async def test_a_hand_built_grant_cannot_defeat_the_capability_check(
    pipeline: Pipeline,
) -> None:
    forged = AuthorizationGrant(
        session_id=SESSION_ID,
        tool="read_file",
        target="hid",
        scope="hid",
        source=GrantSource.HUMAN,
    )
    request = pipeline.request("read_file", target="hid", path="notes.md")
    with pytest.raises(Exception, match="lacks capability"):
        await pipeline.executor.run(forged, request)


async def test_a_failing_tool_returns_a_failed_result_not_an_exception(
    pipeline: Pipeline,
) -> None:
    request = pipeline.request("read_file", path="missing.md")
    grant = await _allow_and_grant(pipeline, request)
    result = await pipeline.executor.run(grant, request)
    assert result.ok is False
    assert "No such file" in (result.error or "")


async def test_a_not_implemented_target_reports_cleanly(pipeline: Pipeline) -> None:
    request = pipeline.request("read_file", target="ssh:ws", path="notes.md")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    grant = await pipeline.broker.mint_grant(request, decision, source=GrantSource.HUMAN)
    result = await pipeline.executor.run(grant, request)
    assert result.ok is False
    assert result.metadata.get("not_implemented") is True


# -- the pending-authorization queue ---------------------------------------


@pytest.fixture
def queue(pipeline: Pipeline) -> AuthorizationQueue:
    return AuthorizationQueue(
        broker=pipeline.broker, grants=pipeline.grants, bus=pipeline.bus, default_timeout=0.2
    )


async def test_an_unanswered_prompt_times_out_into_a_denial(
    pipeline: Pipeline, queue: AuthorizationQueue
) -> None:
    """An unattended pocket device must fail closed."""
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    resolution, grant = await queue.request(
        request, decision, spec=pipeline.tools.get("write_file").spec, timeout=0.05
    )
    assert resolution is Resolution.TIMEOUT
    assert grant is None


async def test_an_approved_prompt_yields_a_usable_grant(
    pipeline: Pipeline, queue: AuthorizationQueue
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)

    task = asyncio.ensure_future(
        queue.request(request, decision, spec=pipeline.tools.get("write_file").spec, timeout=5)
    )
    pending = await _await_pending(queue)
    assert pending.tool == "write_file" and pending.scope == SCOPE_WORKSPACE

    grant = await queue.approve(pending.id, mode=PermissionMode.MANUAL)
    resolution, delivered = await task
    assert resolution is Resolution.APPROVED
    assert delivered is not None and delivered.id == grant.id
    assert grant.source is GrantSource.HUMAN  # one-shot, not a standing grant
    assert (await pipeline.executor.run(grant, request)).ok


async def test_approving_for_the_session_mints_a_standing_grant(
    pipeline: Pipeline, queue: AuthorizationQueue
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    task = asyncio.ensure_future(
        queue.request(request, decision, spec=pipeline.tools.get("write_file").spec, timeout=5)
    )
    pending = await _await_pending(queue)
    grant = await queue.approve(pending.id, scope_to_session=True, mode=PermissionMode.MANUAL)
    await task
    assert grant.source is GrantSource.SESSION

    follow_up = await pipeline.broker.decide(
        pipeline.request("write_file", path="b.txt", content="y"), PermissionMode.MANUAL
    )
    assert follow_up.outcome is DecisionOutcome.ALLOW


async def test_a_denied_prompt_yields_no_grant(
    pipeline: Pipeline, queue: AuthorizationQueue
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    task = asyncio.ensure_future(
        queue.request(request, decision, spec=pipeline.tools.get("write_file").spec, timeout=5)
    )
    pending = await _await_pending(queue)
    await queue.deny(pending.id, "no thanks")
    resolution, grant = await task
    assert resolution is Resolution.DENIED and grant is None


async def test_resolving_an_unknown_prompt_is_refused(queue: AuthorizationQueue) -> None:
    with pytest.raises(PermissionDenied):
        await queue.approve("nope", mode=PermissionMode.MANUAL)
    with pytest.raises(PermissionDenied):
        await queue.deny("nope")


async def _await_pending(queue: AuthorizationQueue, attempts: int = 200):
    for _ in range(attempts):
        pending = queue.list_pending()
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending authorization appeared")


# -- persistence and audit (D4) --------------------------------------------


async def test_every_decision_grant_and_prompt_is_persisted(
    pipeline: Pipeline, queue: AuthorizationQueue, db: Database
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)

    stored = await EventsRepository(db).query(type_prefix="tool.decided")
    assert any(e.payload["request_id"] == request.id for e in stored)

    task = asyncio.ensure_future(
        queue.request(request, decision, spec=pipeline.tools.get("write_file").spec, timeout=5)
    )
    pending = await _await_pending(queue)
    row = await pipeline.grants.get_pending(pending.id)
    assert row is not None and row.resolution is None and row.risk == "mutating"

    grant = await queue.approve(pending.id, mode=PermissionMode.MANUAL)
    await task

    resolved = await pipeline.grants.get_pending(pending.id)
    assert resolved is not None and resolved.resolution == "approved"

    saved = await pipeline.grants.get_grant(grant.id)
    assert saved is not None
    assert saved.tool == "write_file" and saved.scope == SCOPE_WORKSPACE
    assert saved.source == "human"
    assert saved.used_at is None

    await pipeline.executor.run(grant, request)
    used = await pipeline.grants.get_grant(grant.id)
    assert used is not None and used.used_at is not None


async def test_events_are_published_for_each_stage(pipeline: Pipeline) -> None:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    pipeline.bus.subscribe("tool.*", handler)
    request = pipeline.request("read_file", path="notes.md")
    grant = await _allow_and_grant(pipeline, request)
    await pipeline.executor.run(grant, request)
    await asyncio.sleep(0.05)

    types = {event.type for event in seen}
    assert {"tool.requested", "tool.decided", "tool.granted", "tool.executing", "tool.result"} <= (
        types
    )


async def test_unresolved_prompts_are_expired_at_boot(
    pipeline: Pipeline, queue: AuthorizationQueue
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    decision = await pipeline.broker.decide(request, PermissionMode.MANUAL)
    task = asyncio.ensure_future(
        queue.request(request, decision, spec=pipeline.tools.get("write_file").spec, timeout=5)
    )
    pending = await _await_pending(queue)

    assert await queue.expire_all(SESSION_ID) == 1
    row = await pipeline.grants.get_pending(pending.id)
    assert row is not None and row.resolution == "expired"

    await queue.deny(pending.id)
    await task


async def test_revoking_session_grants_makes_the_broker_ask_again(
    pipeline: Pipeline,
) -> None:
    request = pipeline.request("write_file", path="a.txt", content="x")
    await _grant_session(pipeline, request)
    assert (
        await pipeline.broker.decide(request, PermissionMode.SESSION)
    ).outcome is DecisionOutcome.ALLOW

    assert await pipeline.grants.revoke_session_grants(SESSION_ID, now=datetime.now(UTC)) == 1
    again = await pipeline.broker.decide(request, PermissionMode.SESSION)
    assert again.outcome is DecisionOutcome.NEEDS_AUTH


def test_decision_model_is_serializable() -> None:
    decision = Decision(
        request_id="r",
        tool="read_file",
        target_id="local",
        scope=SCOPE_WORKSPACE,
        outcome=DecisionOutcome.ALLOW,
        reason="because",
        mode=PermissionMode.MANUAL,
    )
    assert decision.model_dump(mode="json")["outcome"] == "allow"
    assert decision.allowed is True


# -- the operator's declared command list (D41) -----------------------------
#
# The relaxation of `never_auto`, and therefore the code most worth attacking.
# `test_command_policy.py` covers the matcher; these cover what the *broker*
# does with it — which rules it suppresses, and which it must not.


VERIFICATION_COMMANDS = ["pytest", "ruff check", "git status"]


def _declared_pipeline(db: Database, bus: EventBus, root: Path) -> Pipeline:
    return _make_pipeline(
        db,
        bus,
        root,
        enable_run_command=True,
        allowed_commands=VERIFICATION_COMMANDS,
        classifier=_always_safe,
    )


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_a_declared_command_runs_without_a_prompt_in_every_mode(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    """Including `manual`. The declaration *is* the operator's approval — made
    once, in a file they can read, rather than re-answered on a 320x240 screen
    every time Nomad verifies its own change (D22)."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _declared_pipeline(db, event_bus, root)

    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="pytest -q", cwd="."), mode
    )

    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.never_auto is False
    assert decision.grant_source is GrantSource.AUTO
    # The audit trail names the entry that authorized it.
    assert "pytest" in decision.reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_an_undeclared_command_is_still_never_auto(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    """The list is an allowlist, not a switch. Everything off it is unchanged."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _declared_pipeline(db, event_bus, root)

    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="rm -rf /", cwd="."), mode
    )

    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_a_declared_prefix_cannot_smuggle_a_second_command(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    """The attack this whole feature has to survive."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _declared_pipeline(db, event_bus, root)

    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="pytest -q; rm -rf /", cwd="."), mode
    )

    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_a_declared_command_on_an_ssh_target_is_still_blocked(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    """The rule ordering, asserted rather than assumed: the policy suppresses
    `never_auto` and the workspace-scope rule, and *nothing else*. A command
    that lands on another machine is blocked whatever the list says."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _make_pipeline(
        db,
        event_bus,
        root,
        enable_run_command=True,
        allowed_commands=["pytest"],
        classifier=_always_safe,
    )

    decision = await pipeline.broker.decide(
        ToolRequest(
            tool="run_command",
            target_id="ssh:ws",
            params={"command": "pytest -q", "cwd": "."},
            session_id=SESSION_ID,
        ),
        mode,
    )

    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True
    assert "SSH target" in decision.reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_a_declared_command_never_unlocks_hid_or_destructive_tools(
    db: Database, event_bus: EventBus, tmp_path: Path, mode: PermissionMode
) -> None:
    """A tool that happens to take a `command` param must not inherit the
    relaxation. The policy is consulted only for tools that declare EXEC, and
    the HID and DESTRUCTIVE rules run before it regardless."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _make_pipeline(
        db,
        event_bus,
        root,
        allowed_commands=["pytest", "type_text", "destroy"],
        classifier=_always_safe,
    )

    for tool, target in (("type_text", "hid"), ("destroy", "local")):
        decision = await pipeline.broker.decide(
            ToolRequest(
                tool=tool,
                target_id=target,
                params={"command": "pytest", "text": "x", "path": "p"},
                session_id=SESSION_ID,
            ),
            mode,
        )
        assert decision.outcome is DecisionOutcome.NEEDS_AUTH, tool
        assert decision.never_auto is True, tool


async def test_an_empty_policy_leaves_the_shell_exactly_as_it_was(
    db: Database, event_bus: EventBus, tmp_path: Path
) -> None:
    """The shipped default. Nothing about `Bash` changes until an operator
    writes a list down."""
    await ConversationsRepository(db).create_session(mode="manual", session_id=SESSION_ID)
    root = tmp_path / "ws"
    root.mkdir()
    pipeline = _make_pipeline(db, event_bus, root, enable_run_command=True)

    decision = await pipeline.broker.decide(
        pipeline.request("run_command", command="pytest -q", cwd="."), PermissionMode.AUTO
    )

    assert decision.outcome is DecisionOutcome.NEEDS_AUTH
    assert decision.never_auto is True


# -- D43: the scratch root ---------------------------------------------------
#
# D22 says self-modification happens in a scratch worktree; D14 made every
# write outside the workspace `never_auto`. Together those made D22's path
# unwalkable with nobody watching, and the observed failure was a self-directed
# turn stalling for ten minutes on a prompt no one was there to answer.
#
# The rule that makes the widening safe is the ORDER: the source-tree test runs
# first and returns immediately, so a scratch root that overlaps Nomad's own
# tree cannot make it writable. `test_a_scratch_path_inside_the_source_tree_is_
# still_source_tree` is that rule, and it was watched failing before it was
# trusted — the source-tree branch was removed and it went red.


def _scratch_case(tmp_path: Path, path: str, scratch: Path | None) -> str:
    return compute_scope(
        ProbeWriteTool.spec,
        LocalTarget(),
        {"path": path},
        Workspace(tmp_path / "workspace"),
        scratch,
    )


def test_without_a_declared_scratch_root_nothing_changes(tmp_path: Path) -> None:
    """The shipped default. D43 must be inert until an operator opts in."""
    scope = _scratch_case(tmp_path, str(tmp_path / "elsewhere" / "x.py"), None)
    assert scope == SCOPE_OUTSIDE
    assert never_auto_reason(ProbeWriteTool.spec, LocalTarget(), scope) is not None


def test_a_path_under_the_scratch_root_is_writable_unattended(tmp_path: Path) -> None:
    """The whole point: D22's worktree can now be built with nobody watching."""
    scratch = tmp_path / "scratch"
    scope = _scratch_case(tmp_path, str(scratch / "try" / "src" / "x.py"), scratch)
    assert scope == SCOPE_SCRATCH
    assert never_auto_reason(ProbeWriteTool.spec, LocalTarget(), scope) is None


def test_a_scratch_path_inside_the_source_tree_is_still_source_tree(tmp_path: Path) -> None:
    """The load-bearing ordering. If a scratch root could contain Nomad's own
    code, D43 would silently repeal D21 — so the source-tree test runs first
    and returns before the scratch test is ever reached."""
    source = nomad_source_root()
    scope = _scratch_case(tmp_path, str(source / "src" / "nomad" / "app.py"), source.parent)
    assert scope == SCOPE_SOURCE_TREE
    assert never_auto_reason(ProbeWriteTool.spec, LocalTarget(), scope) is not None


def test_the_strictest_scope_still_wins_across_several_paths(tmp_path: Path) -> None:
    """A call naming a scratch path *and* a genuinely outside one is `outside`.
    Returning on the first escaped path would let the stricter one hide."""

    class TwoPaths:
        spec = ProbeWriteTool.spec.model_copy(update={"path_params": ("path", "other")})

    scratch = tmp_path / "scratch"
    scope = compute_scope(
        TwoPaths.spec,
        LocalTarget(),
        {"path": str(scratch / "ok.py"), "other": str(tmp_path / "elsewhere" / "no.py")},
        Workspace(tmp_path / "workspace"),
        scratch,
    )
    assert scope == SCOPE_OUTSIDE


def test_the_scratch_root_does_not_unlock_ssh_hid_or_destructive(tmp_path: Path) -> None:
    """D43 widens exactly one rule. These are evaluated before it and stay put."""
    scratch = tmp_path / "scratch"
    ssh = SshTarget(alias="ws", host="10.0.0.5", user="lucas")
    assert never_auto_reason(ProbeWriteTool.spec, ssh, SCOPE_SCRATCH) is not None
    assert never_auto_reason(ProbeWriteTool.spec, HidTarget(), SCOPE_SCRATCH) is not None
    assert never_auto_reason(DestroyTool.spec, LocalTarget(), SCOPE_SCRATCH) is not None
    # And the ordinary case is still permitted, so the assertions above are
    # about those rules rather than about the scope being rejected outright.
    assert _scratch_case(tmp_path, str(scratch / "x.py"), scratch) == SCOPE_SCRATCH


def test_an_unresolvable_path_under_scratch_still_fails_closed(tmp_path: Path) -> None:
    """Fail closed survives the widening: an uninterpretable path gets the most
    restricted scope, never the newly permissive one."""
    scope = _scratch_case(tmp_path, "\x00not-a-path", tmp_path / "scratch")
    assert scope == SCOPE_SOURCE_TREE
