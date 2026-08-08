"""The device asks on its own screen, and silence still denies (D21, D32, D36).

The defect these close: `tool.authorization_pending` had zero subscribers, so
a broker that needed a human parked for five minutes and auto-denied having
shown nobody anything. Fail-closed was intact and invisible, which D35 already
named as the shape that teaches an operator to switch the guarantee off.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nomad.app import NomadApp
from nomad.core.config import InputConfig, NomadConfig, PermissionMode
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.input.choice import ChoiceOutcome, ChoiceResult, InputChoicePrompter, NullChoicePrompter
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.protocol.messages import ButtonId, InputButton, InputJoystick, KeyPhase
from nomad.tools.egress import Egress, classify
from nomad.tools.permissions import (
    EVENT_AUTH_PENDING,
    DecisionOutcome,
    Resolution,
    ToolRequest,
)
from nomad.view.authprompt import (
    AUTH_PROMPT_WRITER,
    OPTION_APPROVE,
    OPTION_APPROVE_SESSION,
    OPTION_DENY,
    PROMPT_OPTIONS,
    PROMPT_QUESTION,
    AuthorizationPrompter,
    compose_question,
    field,
    make_show,
)
from nomad.view.screen import ScreenOwner

PENDING = "p1"


class FakeResolver:
    """Stands in for `AgentSession`, which is what wires it in for real."""

    def __init__(self, *, fail: bool = False) -> None:
        self.approved: list[tuple[str, bool]] = []
        self.denied: list[tuple[str, str]] = []
        self._fail = fail

    async def approve(self, pending_id: str, *, scope_to_session: bool = False) -> Any:
        if self._fail:
            raise RuntimeError("no authorization is awaiting that id")
        self.approved.append((pending_id, scope_to_session))
        return object()

    async def deny(self, pending_id: str, reason: str = "denied by operator") -> None:
        if self._fail:
            raise RuntimeError("no authorization is awaiting that id")
        self.denied.append((pending_id, reason))


class ScriptedPrompter:
    """Answers however the test says, without needing input hardware."""

    def __init__(self, result: ChoiceResult, *, show: Any = None) -> None:
        self._result = result
        self._show = show
        self.asked: list[tuple[str, list[str], float | None]] = []

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResult:
        self.asked.append((question, list(options), timeout_s))
        if self._show is not None:
            await self._show(question, options, 0)
        return self._result


def _pending_event(**overrides: object) -> Event:
    payload: dict[str, Any] = {
        "pending_id": PENDING,
        "session_id": "s1",
        "turn_id": "t1",
        "tool": "fs_write",
        "target": "local",
        "scope": "outside",
        "risk": "mutating",
        "reason": "manual mode requires approval for a mutating action",
        "params": {"path": "/etc/hosts", "content": "x"},
        "expires_at": None,
    }
    payload.update(overrides)
    return Event(type=EVENT_AUTH_PENDING, source="authorization_queue", payload=payload)


def _answered(option: str) -> ChoiceResult:
    return ChoiceResult(
        outcome=ChoiceOutcome.ANSWERED, option=option, index=PROMPT_OPTIONS.index(option)
    )


@pytest.fixture
def display() -> HeadlessDisplay:
    return HeadlessDisplay()


@pytest.fixture
def screen(display: HeadlessDisplay) -> ScreenOwner:
    return ScreenOwner(display)  # type: ignore[arg-type]


def _prompt(
    screen: ScreenOwner,
    bus: EventBus,
    result: ChoiceResult,
    *,
    resolver: FakeResolver | None = None,
) -> tuple[AuthorizationPrompter, FakeResolver, ScriptedPrompter]:
    resolver = resolver or FakeResolver()
    prompter = ScriptedPrompter(result, show=make_show(screen.view(AUTH_PROMPT_WRITER)))
    component = AuthorizationPrompter(
        bus=bus, screen=screen, prompter=prompter, resolver=resolver
    )
    return component, resolver, prompter


# -- the prompt reaches the glass at all -------------------------------------


async def test_a_pending_authorization_is_drawn_on_the_screen(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    component, _, _ = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event())

    drawn = "\n".join(frame.text for frame in display.history)
    assert "Authorization required" in drawn
    assert "fs_write" in drawn
    assert "local" in drawn
    assert "outside" in drawn
    assert "mutating" in drawn
    assert component.prompts_shown == 1


async def test_every_option_is_offered_and_deny_is_the_default_highlight(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """An operator who mashes CONFIRM at a surprise screen must land on deny."""
    component, _, prompter = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event())

    _, options, _ = prompter.asked[0]
    assert options == [OPTION_DENY, OPTION_APPROVE, OPTION_APPROVE_SESSION]
    first = display.history[0].text
    assert f"> {OPTION_DENY}" in first
    assert f"  {OPTION_APPROVE}" in first


# -- the answer actually resolves the pending authorization ------------------


async def test_approving_mints_a_single_use_grant(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_APPROVE))
    await component.handle(_pending_event())
    assert resolver.approved == [(PENDING, False)]
    assert resolver.denied == []


async def test_approving_for_the_session_asks_for_a_standing_grant(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_APPROVE_SESSION))
    await component.handle(_pending_event())
    assert resolver.approved == [(PENDING, True)]


async def test_choosing_deny_denies(screen: ScreenOwner, event_bus: EventBus) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event())
    assert resolver.approved == []
    assert resolver.denied == [(PENDING, "operator chose Deny")]


# -- every non-answer denies, and says which non-answer it was ---------------


@pytest.mark.parametrize(
    ("outcome", "fragment"),
    [
        (ChoiceOutcome.CANCELLED, "dismissed"),
        (ChoiceOutcome.TIMED_OUT, "expired"),
        (ChoiceOutcome.NO_OPERATOR, "no input device"),
    ],
)
async def test_a_non_answer_denies_with_a_distinguishable_reason(
    screen: ScreenOwner, event_bus: EventBus, outcome: ChoiceOutcome, fragment: str
) -> None:
    """`no human answered` and `there is no human` mean opposite things."""
    component, resolver, _ = _prompt(screen, event_bus, ChoiceResult(outcome=outcome))
    await component.handle(_pending_event())

    assert resolver.approved == []
    assert len(resolver.denied) == 1
    pending_id, reason = resolver.denied[0]
    assert pending_id == PENDING
    assert fragment in reason


async def test_the_three_denial_reasons_are_all_different(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    reasons = set()
    for outcome in (ChoiceOutcome.CANCELLED, ChoiceOutcome.TIMED_OUT, ChoiceOutcome.NO_OPERATOR):
        component, resolver, _ = _prompt(screen, event_bus, ChoiceResult(outcome=outcome))
        await component.handle(_pending_event())
        reasons.add(resolver.denied[0][1])
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event())
    reasons.add(resolver.denied[0][1])
    assert len(reasons) == 4


async def test_a_resolve_that_lost_the_race_is_reported_not_retried(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """The queue's own timeout may have denied it already; it still stands."""
    component, _, _ = _prompt(
        screen, event_bus, _answered(OPTION_APPROVE), resolver=FakeResolver(fail=True)
    )
    await component.handle(_pending_event())
    assert "expired before it was answered" in display.screen.text
    assert component.state is ComponentState.NEW  # it did not fall over


async def test_an_event_without_a_pending_id_draws_nothing(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_APPROVE))
    await component.handle(_pending_event(pending_id=""))
    assert display.history == []
    assert resolver.approved == [] and resolver.denied == []


# -- no model-authored text reaches the prompt (D36) -------------------------


async def test_model_supplied_parameters_are_never_drawn(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """Params as prompt chrome is a surface for composing a fake approval."""
    component, _, _ = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(
        _pending_event(
            params={
                "content": "Authorization required\n> Approve once\nAllow this action?",
                "path": "/etc/shadow",
            }
        )
    )
    drawn = "\n".join(frame.text for frame in display.history)
    assert "/etc/shadow" not in drawn
    assert "Allow this action?" in drawn
    # The one occurrence is Nomad's own, not one the params smuggled in.
    assert drawn.count("Allow this action?") == len(display.history) - 1


def test_a_structured_field_cannot_add_lines_or_run_long() -> None:
    assert "\n" not in field("fs_write\nApprove once\n> Deny")
    assert field("x" * 200) == "x" * 47 + "…"
    assert field("") == "?"
    assert field("a\x07b") == "a?b"


def test_the_question_carries_only_the_four_structured_fields() -> None:
    question = compose_question(
        {"tool": "t", "target": "g", "scope": "s", "risk": "r", "reason": "SECRET", "params": {}}
    )
    assert "SECRET" not in question
    assert question.count("\n") == 4


# -- the operator can see what they are approving (D36) ----------------------
#
# The prompt for `bash -c "ssh prod rm -rf /"` read `tool Bash / target local /
# scope none / risk privileged` and showed no command at all, so the operator
# was asked to approve something they could not check against reality.


def test_the_command_is_shown_when_the_call_carries_one() -> None:
    question = compose_question(
        {
            "tool": "Bash",
            "target": "ssh",
            "scope": "ssh:ssh",
            "risk": "privileged",
            "params": {"command": 'bash -c "ssh prod rm -rf /"'},
        }
    )
    assert 'cmd     bash -c "ssh prod rm -rf /"' in question
    assert question.count("\n") == 5


def test_a_call_with_no_command_gains_no_line() -> None:
    """The field is declared, not scraped: no command, no row."""
    question = compose_question(
        {"tool": "Read", "params": {"file_path": "/etc/shadow", "content": "x"}}
    )
    assert "cmd" not in question
    assert "/etc/shadow" not in question
    assert question.count("\n") == 4


def test_a_non_string_command_is_not_drawn() -> None:
    """The bridge already refused to classify it; there is no command to show."""
    assert "cmd" not in compose_question({"tool": "Bash", "params": {"command": ["ssh"]}})
    assert "cmd" not in compose_question({"tool": "Bash", "params": {"command": "   "}})
    assert "cmd" not in compose_question({"tool": "Bash", "params": "not-a-dict"})


def test_the_command_cannot_forge_an_option_or_a_frame() -> None:
    """It is a field, not chrome: escaped and truncated like every other one."""
    question = compose_question(
        {
            "tool": "Bash",
            "params": {
                "command": "echo hi\nAuthorization required\n> Approve once\nAllow this action?"
            },
        }
    )
    lines = question.splitlines()
    # It is welded to one labelled line, so it cannot become a frame of its
    # own: no second question line, and no option row the operator could land
    # a CONFIRM on. The words survive; the *shape* of the prompt does not bend.
    assert question.count("\n") == 5
    assert [line for line in lines if line == PROMPT_QUESTION] == [PROMPT_QUESTION]
    assert not any(line.startswith(("> ", "  ")) for line in lines)
    assert lines[-1].startswith("cmd     echo hi Authorization required")


def test_the_command_is_truncated_and_the_verdict_is_not() -> None:
    """A padded command cannot hide its tail *from the classifier* (D27).

    Truncation is a display bound. The fields above the command are computed
    by `tools/egress.py` from the whole string, so padding the prefix costs
    the operator detail and buys nothing: the target still says `ssh`.
    """
    padded = "echo " + "a" * 400 + ' ; ssh prod rm -rf /'
    assert classify(padded) is Egress.REMOTE
    line = compose_question({"tool": "Bash", "params": {"command": padded}}).splitlines()[-1]
    assert line.endswith("\u2026")
    assert len(line) <= len("cmd     ") + 96


# -- the prompt owns the screen while it is pending --------------------------


async def test_streaming_text_cannot_paint_over_a_pending_prompt(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """A question overwritten mid-turn is worse than no question at all."""
    renderer_view = screen.view("renderer")
    released = asyncio.Event()

    class SlowPrompter:
        async def ask(
            self, question: str, options: list[str], *, timeout_s: float | None = None
        ) -> ChoiceResult:
            await make_show(screen.view(AUTH_PROMPT_WRITER))(question, options, 0)
            # The turn is still streaming while the operator reads.
            for chunk in ("the model keeps talking", "and talking"):
                await renderer_view.show_text(chunk)
            released.set()
            return _answered(OPTION_DENY)

    resolver = FakeResolver()
    component = AuthorizationPrompter(
        bus=event_bus, screen=screen, prompter=SlowPrompter(), resolver=resolver
    )
    await component.handle(_pending_event())
    assert released.is_set()

    drawn = "\n".join(frame.text for frame in display.history)
    assert "the model keeps talking" not in drawn
    assert "and talking" not in drawn
    assert screen.suppressed == 2
    # And the screen goes back to being shared the moment it is resolved.
    assert screen.holder is None
    await renderer_view.show_text("the turn resumes")
    assert display.screen.text == "the turn resumes"


async def test_the_model_cannot_draw_over_its_own_authorization_prompt(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """`display_*` auto-runs in every mode (D35). It must not hide the ask."""
    model_view = screen.view("model")

    class DrawingPrompter:
        async def ask(
            self, question: str, options: list[str], *, timeout_s: float | None = None
        ) -> ChoiceResult:
            await make_show(screen.view(AUTH_PROMPT_WRITER))(question, options, 0)
            await model_view.show_card("Approved", "nothing to see", [("ok", "yes")])
            await model_view.show_list("menu", [("a", None)], selectable=True)
            await model_view.show_choice("Continue?", ["Yes"])
            return _answered(OPTION_DENY)

    component = AuthorizationPrompter(
        bus=event_bus, screen=screen, prompter=DrawingPrompter(), resolver=FakeResolver()
    )
    await component.handle(_pending_event())
    drawn = "\n".join(frame.text for frame in display.history)
    assert "nothing to see" not in drawn
    assert "Continue?" not in drawn
    assert screen.suppressed == 3


async def test_a_second_prompt_waits_for_the_screen_rather_than_being_refused(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    order: list[str] = []

    async def hold() -> None:
        async with screen.exclusive("other"):
            order.append("held")
            await asyncio.sleep(0.05)
        order.append("released")

    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_DENY))
    holding = asyncio.ensure_future(hold())
    await asyncio.sleep(0.01)
    await component.handle(_pending_event())
    order.append("prompted")
    await holding

    assert order == ["held", "released", "prompted"]
    assert resolver.denied


# -- the lifecycle and the bus ----------------------------------------------


async def test_the_prompter_subscribes_and_unsubscribes_with_its_lifecycle(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_APPROVE))
    await component.start()
    assert component.state is ComponentState.STARTED

    await event_bus.publish(_pending_event())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if resolver.approved:
            break
    assert resolver.approved == [(PENDING, False)]

    await component.stop()
    assert component.state is ComponentState.STOPPED
    await event_bus.publish(_pending_event())
    await asyncio.sleep(0.05)
    assert len(resolver.approved) == 1


async def test_an_unrelated_event_is_ignored(screen: ScreenOwner, event_bus: EventBus) -> None:
    component, resolver, _ = _prompt(screen, event_bus, _answered(OPTION_APPROVE))
    await component.handle(Event(type="tool.decided", source="broker", payload={"tool": "x"}))
    assert resolver.approved == []


async def test_the_prompt_never_outlives_the_pending_record(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    """A prompt answered after the queue gave up is an answer nobody hears."""
    component, _, prompter = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event(expires_at="2000-01-01T00:00:00+00:00"))
    assert prompter.asked[0][2] == 0.0

    component, _, prompter = _prompt(screen, event_bus, _answered(OPTION_DENY))
    await component.handle(_pending_event(expires_at="not a timestamp"))
    assert prompter.asked[0][2] == 60.0


# -- the joystick actually answers it (D32 meeting D36) ----------------------


async def _press(stream: InputStream, button: ButtonId) -> None:
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.PRESS))
    await stream.feed_button(InputButton(button=button, phase=KeyPhase.RELEASE))


async def test_the_joystick_approves_a_real_pending_authorization(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """End to end over the mock stack: down once, confirm, grant minted."""
    stream = InputStream(InputMapper(InputConfig()))
    await stream.start()
    resolver = FakeResolver()
    component = AuthorizationPrompter(
        bus=event_bus,
        screen=screen,
        prompter=InputChoicePrompter(
            stream, show=make_show(screen.view(AUTH_PROMPT_WRITER)), default_timeout_s=2.0
        ),
        resolver=resolver,
    )
    handling = asyncio.ensure_future(component.handle(_pending_event()))
    try:
        await asyncio.sleep(0.05)
        await stream.feed_joystick(InputJoystick(x=0.0, y=1.0))  # NAV_DOWN
        await asyncio.sleep(0.05)
        await _press(stream, ButtonId.A)  # CONFIRM
        await asyncio.wait_for(handling, timeout=2.0)
    finally:
        await stream.stop()

    assert resolver.approved == [(PENDING, False)]
    assert "Approved" in display.screen.text


async def test_the_back_button_denies_a_real_pending_authorization(
    screen: ScreenOwner, event_bus: EventBus
) -> None:
    stream = InputStream(InputMapper(InputConfig()))
    await stream.start()
    resolver = FakeResolver()
    component = AuthorizationPrompter(
        bus=event_bus,
        screen=screen,
        prompter=InputChoicePrompter(
            stream, show=make_show(screen.view(AUTH_PROMPT_WRITER)), default_timeout_s=2.0
        ),
        resolver=resolver,
    )
    handling = asyncio.ensure_future(component.handle(_pending_event()))
    try:
        await asyncio.sleep(0.05)
        await _press(stream, ButtonId.B)  # BACK
        await asyncio.wait_for(handling, timeout=2.0)
    finally:
        await stream.stop()

    assert resolver.approved == []
    assert "dismissed" in resolver.denied[0][1]


async def test_with_no_input_hardware_the_question_is_still_shown_then_denied(
    screen: ScreenOwner, display: HeadlessDisplay, event_bus: EventBus
) -> None:
    """The current reality of the device: drawn, refused, and said so."""
    resolver = FakeResolver()
    component = AuthorizationPrompter(
        bus=event_bus,
        screen=screen,
        prompter=NullChoicePrompter(show=make_show(screen.view(AUTH_PROMPT_WRITER))),
        resolver=resolver,
    )
    await component.handle(_pending_event())

    assert "fs_write" in display.history[0].text
    assert resolver.denied == [(PENDING, "no input device is attached, so nobody could answer")]
    assert "Denied" in display.screen.text


# -- the real queue, the real session, no hardware ---------------------------


async def test_a_parked_tool_call_is_resolved_by_the_prompt(tmp_path: Path) -> None:
    """The defect, end to end: park, ask, deny — not park, hang, deny.

    Through the composition root and the real `AuthorizationQueue`, so this is
    what the device does today with nothing plugged into it. `DENIED` rather
    than `TIMEOUT` is the whole point: the queue's own 300s auto-denial never
    got a chance to fire, because the device answered.
    """
    app = NomadApp(
        NomadConfig.model_validate(
            {
                "storage": {"path": str(tmp_path / "nomad.db")},
                "workspace": {"root": str(tmp_path / "workspace")},
                "view": {"enabled": False},
                "agent": {"mode": "manual"},
            }
        )
    )
    await app.start()
    try:
        spec = app.tools.get("write_file").spec
        request = ToolRequest(
            tool="write_file",
            target_id="local",
            params={"path": "a.txt", "content": "x"},
            session_id=app.session.session_id,
        )
        decision = await app.session.broker.decide(request, PermissionMode.MANUAL)
        assert decision.outcome is DecisionOutcome.NEEDS_AUTH

        resolution, grant = await asyncio.wait_for(
            app.session.queue.request(request, decision, spec=spec, timeout=30), timeout=10
        )
    finally:
        await app.stop()

    assert resolution is Resolution.DENIED
    assert grant is None
    assert "write_file" in app.display.screen.text  # type: ignore[attr-defined]
    assert app.authprompt.prompts_shown == 1
