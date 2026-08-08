"""The device asks, on its own screen, before it refuses (D36).

`tool.authorization_pending` had publishers and no subscribers. So the whole
of the device's observable safety behaviour was: park for five minutes, then
auto-deny, having never shown anything to anybody. D35 named that shape
exactly — a guarantee whose first-boot behaviour is indistinguishable from a
broken device is one the operator turns off. This module is the missing
subscriber, and the answer half has existed since D32 (`input/choice.py`).

**It is not a tool, and that is the design (D36).** The broker exists to gate
*the model's* requests; the model never asked for this prompt, Nomad did, and
routing it through `can_use_tool` would be asking the gatekeeper to approve
its own question. So there is no `ToolSpec` here, nothing registers in
`ToolRegistry`, and no privileged grant kind is invented — a privileged grant
would be strictly worse, because it would put a grant type in the vocabulary
that the model can learn to ask for.

Two rules follow from drawing while holding model-supplied data:

* **Structured fields only.** Tool name, target, scope, risk, and — since the
  third adversarial review — the command, when the call carries one. Each is
  escaped and truncated. `params` at large is still *never* drawn:
  model-authored text as prompt *chrome* is a surface on which the model
  composes its own convincing approval screen, and the operator has no way to
  tell the frame apart from Nomad's own.

  The command is a declared, labelled field and not chrome, which is what makes
  it admissible under the same rule. It is there because a prompt that named a
  `Bash` call on target `local` and showed no command at all was asking the
  operator to approve something they could not see — and the review's example,
  `bash -c "ssh prod rm -rf /"`, was mislabelled `local` at the same time. Two
  properties keep it honest: `field()` collapses whitespace, so no value can
  add a line or forge an option, and the *verdict* fields above it are computed
  from the whole command by `tools/egress.py` — never from the truncated
  prefix. A command padded so its tail falls off the screen therefore still
  reports the target its tail earned it.
* **Silence still denies.** Showing the prompt changes what the operator sees,
  never what a timeout means (D21). Every non-approval below ends in
  `deny()`, and they are *distinguishable in the logs*, because "a human said
  no" and "there is no human" mean opposite things when you are debugging a
  device that has been in a drawer for a day.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from nomad.core.events import Event, EventBus, Unsubscribe
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger
from nomad.input.choice import ChoiceOutcome, ChoiceResult, ShowChoice
from nomad.tools.egress import COMMAND_PARAMS
from nomad.tools.permissions import EVENT_AUTH_PENDING
from nomad.view.screen import ScreenOwner, ScreenView

logger = get_logger(__name__)

#: The name this component draws under. It is the exclusive holder of the
#: screen for as long as a prompt is pending, and nothing else may use it.
AUTH_PROMPT_WRITER = "auth_prompt"

PROMPT_TITLE = "Authorization required"
PROMPT_QUESTION = "Allow this action?"

#: `Deny` is first, so it is the option under the highlight when the prompt
#: appears. An operator who mashes CONFIRM at an unexpected screen must land
#: on the safe answer; fail-closed is not only about timeouts.
OPTION_DENY = "Deny"
OPTION_APPROVE = "Approve once"
OPTION_APPROVE_SESSION = "Approve for this session"

#: The queue mints a standing grant from `scope_to_session=True` (D14), so the
#: third option is real rather than aspirational — it is checked, not assumed.
PROMPT_OPTIONS = [OPTION_DENY, OPTION_APPROVE, OPTION_APPROVE_SESSION]

#: How long the *prompt* waits. Shorter than the queue's own 300s timeout on
#: purpose: when nobody is looking, saying so and denying beats holding the
#: turn open for five minutes to reach the same answer.
DEFAULT_PROMPT_TIMEOUT_S = 60.0

#: Leaves the queue's timeout as the outer bound. If the pending record expires
#: first, the queue denies and this prompt's answer would arrive too late.
_EXPIRY_MARGIN_S = 2.0

#: Long enough to identify a tool or a path fragment, short enough that no
#: single field can push the options off a pocket screen.
_MAX_FIELD_CHARS = 48

#: The command gets more room than a label field, because it is the only field
#: whose content the operator has to *read* rather than recognise. Still one
#: line and still truncated: the options must stay on the glass, and the fields
#: above it — not this string — are what the decision is routed on.
_MAX_COMMAND_CHARS = 96

#: What the operator's answer was, in one word, for the log line. `operator`
#: and `no_operator` are the pair that must never be confused.
_ANSWERED_BY_OPERATOR = "operator"
_ANSWERED_BY_NO_OPERATOR = "no_operator"
_ANSWERED_BY_TIMEOUT = "timeout"

#: outcome -> (why it denied, who denied it, is that abnormal)
_DENIALS: dict[ChoiceOutcome, tuple[str, str, bool]] = {
    ChoiceOutcome.CANCELLED: (
        "operator dismissed the authorization prompt",
        _ANSWERED_BY_OPERATOR,
        False,
    ),
    ChoiceOutcome.TIMED_OUT: (
        "no answer before the authorization prompt expired",
        _ANSWERED_BY_TIMEOUT,
        False,
    ),
    ChoiceOutcome.NO_OPERATOR: (
        "no input device is attached, so nobody could answer",
        _ANSWERED_BY_NO_OPERATOR,
        True,
    ),
}

_DENIED_BY_CHOICE = ("operator chose Deny", _ANSWERED_BY_OPERATOR, False)


class ChoicePrompter(Protocol):
    """The answer half (D32). `InputChoicePrompter` and `NullChoicePrompter`."""

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResult: ...


class AuthorizationResolver(Protocol):
    """The queue's existing resolve path, as much of it as this needs.

    `AgentSession` satisfies it structurally — it already supplies the
    permission mode the queue's `approve()` requires, which is why nothing
    here needs to know what mode the device is in.
    """

    async def approve(self, pending_id: str, *, scope_to_session: bool = False) -> Any: ...

    async def deny(self, pending_id: str, reason: str = ...) -> None: ...


def field(value: object, *, limit: int = _MAX_FIELD_CHARS) -> str:
    """One structured field, made safe to draw.

    Collapses whitespace (so nothing can add lines to the frame), replaces
    non-printables, and truncates. Applied to every field without asking where
    it came from: a value that is trustworthy today is model-supplied after
    the next tool is added.
    """
    text = " ".join(str(value).split())
    text = "".join(char if char.isprintable() else "?" for char in text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "?"


def command_of(payload: dict[str, Any]) -> str | None:
    """The shell command this call carries, if it carries one.

    Read through `egress.COMMAND_PARAMS` rather than a second list of key
    names, so the field the operator is shown is by construction the same one
    the classifier routed on. A non-string value is not drawn: it is not a
    command, and the bridge has already refused to classify it.
    """
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    for key in COMMAND_PARAMS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def compose_question(payload: dict[str, Any]) -> str:
    """The prompt body: structured fields, and nothing else (D36).

    Deliberately no free-form reason and no `params` beyond the one declared
    command field. The operator loses detail — this is the cost D36 accepted —
    and gains a frame the model cannot write a single character of chrome into.
    """
    lines = [
        PROMPT_QUESTION,
        f"tool    {field(payload.get('tool'))}",
        f"target  {field(payload.get('target'))}",
        f"scope   {field(payload.get('scope'))}",
        f"risk    {field(payload.get('risk'))}",
    ]
    command = command_of(payload)
    if command is not None:
        lines.append(f"cmd     {field(command, limit=_MAX_COMMAND_CHARS)}")
    return "\n".join(lines)


def make_show(view: ScreenView) -> ShowChoice:
    """Bind the prompter's `show` to one screen handle.

    `ShowChoice` is a function signature rather than a driver so that `input`
    need not import `hardware` (D32). This is the other end of it, and it uses
    `show_text` alone: every `DisplayDriver` has it, including the ESP32 that
    does not exist yet.
    """

    async def show(question: str, options: list[str], highlighted: int) -> None:
        lines = [question, ""]
        lines.extend(
            f"{'>' if index == highlighted else ' '} {field(option)}"
            for index, option in enumerate(options)
        )
        await view.show_text("\n".join(lines), title=PROMPT_TITLE)

    return show


class AuthorizationPrompter:
    """Draws a pending authorization, waits for the joystick, resolves it."""

    name = "auth_prompt"

    def __init__(
        self,
        *,
        bus: EventBus,
        screen: ScreenOwner,
        prompter: ChoicePrompter,
        resolver: AuthorizationResolver,
        timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._screen = screen
        self._prompter = prompter
        self._resolver = resolver
        self._timeout_s = timeout_s
        self._state = ComponentState.NEW
        self._unsubscribe: Unsubscribe | None = None
        self.prompts_shown = 0

    @property
    def state(self) -> ComponentState:
        return self._state

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        # Subscribed to exactly one event type, not `tool.*`: this component
        # has no business seeing the traffic of every decision the broker
        # makes, and a narrow subscription cannot start drawing one by
        # accident.
        self._unsubscribe = self._bus.subscribe(EVENT_AUTH_PENDING, self.handle)
        self._state = ComponentState.STARTED

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._state = ComponentState.STOPPED

    # -- the subscriber ----------------------------------------------------

    async def handle(self, event: Event) -> None:
        if event.type != EVENT_AUTH_PENDING:
            return
        payload = event.payload
        pending_id = str(payload.get("pending_id") or "")
        if not pending_id:
            # Nothing to resolve, so nothing to ask about. Left to the queue's
            # own timeout, which still denies.
            logger.warning("Authorization event carried no pending id; not prompting")
            return

        # Held for the whole exchange — question, answer *and* resolution —
        # rather than only while drawing. Releasing at the answer would let a
        # streamed chunk land on the screen in the window where the operator
        # has decided but the tool call has not moved yet, which reads as the
        # prompt having been ignored.
        async with self._screen.exclusive(AUTH_PROMPT_WRITER) as view:
            self.prompts_shown += 1
            result = await self._prompter.ask(
                compose_question(payload),
                list(PROMPT_OPTIONS),
                timeout_s=self._deadline(payload),
            )
            await self._resolve(pending_id, result, payload, view)

    async def _resolve(
        self,
        pending_id: str,
        result: ChoiceResult,
        payload: dict[str, Any],
        view: ScreenView,
    ) -> None:
        tool = field(payload.get("tool"))
        approved = result.outcome is ChoiceOutcome.ANSWERED and result.option in (
            OPTION_APPROVE,
            OPTION_APPROVE_SESSION,
        )
        if approved:
            scope_to_session = result.option == OPTION_APPROVE_SESSION
            if await self._apply(
                self._resolver.approve(pending_id, scope_to_session=scope_to_session),
                pending_id=pending_id,
                view=view,
            ):
                logger.info(
                    "Authorization approved on the device",
                    extra={
                        "pending_id": pending_id,
                        "tool": tool,
                        "answered_by": _ANSWERED_BY_OPERATOR,
                        "scope_to_session": scope_to_session,
                    },
                )
                await view.show_text(f"tool    {tool}", title="Approved")
            return

        if result.outcome is ChoiceOutcome.ANSWERED:
            reason, answered_by, abnormal = _DENIED_BY_CHOICE
        else:
            reason, answered_by, abnormal = _DENIALS[result.outcome]
        if not await self._apply(
            self._resolver.deny(pending_id, reason), pending_id=pending_id, view=view
        ):
            return
        # `no_operator` is a warning and the others are not: it is the one that
        # says the device cannot be operated at all, rather than that it was
        # not operated this time.
        log = logger.warning if abnormal else logger.info
        log(
            "Authorization denied on the device",
            extra={
                "pending_id": pending_id,
                "tool": tool,
                "answered_by": answered_by,
                "outcome": str(result.outcome),
                "reason": reason,
            },
        )
        await view.show_text(f"tool    {tool}\n{field(reason, limit=72)}", title="Denied")

    async def _apply(self, action: Any, *, pending_id: str, view: ScreenView) -> bool:
        """Run one resolve call, tolerating a prompt that expired underneath.

        The queue drops its context when its own timeout fires, and `approve`
        and `deny` then raise. That is a race this component loses by design —
        the queue has already denied — so it is reported, never retried.
        """
        try:
            await action
        except Exception as exc:  # noqa: BLE001 - any resolve failure denies
            logger.warning(
                "Authorization could not be resolved from the device prompt; the "
                "queue's own timeout stands",
                extra={"pending_id": pending_id, "error": str(exc)},
            )
            await view.show_text("the prompt expired before it was answered", title="Denied")
            return False
        return True

    def _deadline(self, payload: dict[str, Any]) -> float:
        """Never outlive the pending record this prompt is asking about."""
        raw = payload.get("expires_at")
        if not raw:
            return self._timeout_s
        try:
            expires_at = datetime.fromisoformat(str(raw))
        except ValueError:
            return self._timeout_s
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        remaining = (expires_at - datetime.now(UTC)).total_seconds() - _EXPIRY_MARGIN_S
        return max(0.0, min(self._timeout_s, remaining))
