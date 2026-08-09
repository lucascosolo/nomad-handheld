"""The permission pipeline (D4, D14). The most important module in the system.

Four stages, never collapsed into one call::

    ToolRequest  ->  Decision  ->  AuthorizationGrant  ->  ToolResult
                   (broker)      (auto or human)         (executor)

`ToolExecutor.run()` takes a `Grant`, not a request. There is no code path
that executes a tool without one — auto-approval is a policy that *mints* a
grant, not a shortcut around the pipeline.

Two properties are load-bearing and must survive any future edit:

* **`never_auto` runs before mode logic.** `never_auto_reason()` is a single
  function called at the top of `decide()`, so a mode added later cannot
  bypass it by forgetting to check. `mint_grant()` refuses to mint an
  `AUTO`/`MODEL` grant for a `never_auto` action at all, which means the rule
  is enforced twice on independent paths.
* **Scope is recomputed at execution time.** The executor recomputes the scope
  from the request it is about to run and compares it to the grant. A grant
  approved for a path inside the workspace therefore cannot be replayed
  against a path outside it, however the params were mutated in between.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.errors import NomadError, PermissionDenied, TargetError, ToolError
from nomad.core.events import Event, EventBus
from nomad.core.logging import get_logger
from nomad.storage.repositories.grants import (
    GrantRecord,
    GrantsRepository,
    PendingAuthorizationRecord,
)
from nomad.targets.base import Capability, Target, TargetKind, require_capabilities
from nomad.targets.registry import TargetRegistry
from nomad.tools.base import (
    ESCALATING_PERMISSIONS,
    Permission,
    Risk,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from nomad.tools.registry import ToolRegistry
from nomad.tools.workspace import Workspace

logger = get_logger(__name__)

#: Scope values. A grant is keyed on `(tool, target, scope)` (D14), so these
#: strings are part of the security contract, not cosmetic labels.
SCOPE_NONE = "none"
SCOPE_WORKSPACE = "workspace"
SCOPE_OUTSIDE = "outside"
SCOPE_HID = "hid"
#: Nomad's own running source tree (D21). Distinguished from `outside` because
#: it is the one region a write must never reach, however the mode is set.
SCOPE_SOURCE_TREE = "source_tree"
#: Outbound network, keyed by host: `net:example.com` (D31). Per-host, so an
#: approved fetch of one site is not a grant to reach every site.
SCOPE_NETWORK_PREFIX = "net:"
#: A network call whose destination could not be determined. Denied, never
#: given a broad scope — an unparseable URL is not a reason to guess.
SCOPE_NETWORK_UNKNOWN = "net:?"

DEFAULT_GRANT_TTL_SECONDS = 300.0
DEFAULT_AUTHORIZATION_TIMEOUT = 300.0
DEFAULT_CLASSIFIER_TIMEOUT = 5.0
DEFAULT_CLASSIFIER_CONFIDENCE = 0.8

EVENT_REQUESTED = "tool.requested"
EVENT_DECIDED = "tool.decided"
EVENT_GRANTED = "tool.granted"
EVENT_AUTH_PENDING = "tool.authorization_pending"
EVENT_AUTH_RESOLVED = "tool.authorization_resolved"
EVENT_EXECUTING = "tool.executing"
EVENT_RESULT = "tool.result"


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_AUTH = "needs_auth"


class GrantSource(StrEnum):
    """Where a grant came from.

    `SESSION` doubles as the reusability marker: a session grant is the
    standing "this kind, for this session" approval and may be used more than
    once; `AUTO`, `HUMAN` and `MODEL` grants are single-use. Encoding it in
    the source keeps the persisted schema (which has no `reusable` column)
    and the domain model in agreement.
    """

    AUTO = "auto"
    HUMAN = "human"
    SESSION = "session"
    MODEL = "model"


class Resolution(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    TIMEOUT = "timeout"


class ToolRequest(BaseModel):
    """What the model asked for."""

    id: str = Field(default_factory=_new_id)
    tool: str
    target_id: str = "local"
    params: dict[str, Any] = Field(default_factory=dict)
    session_id: str
    turn_id: str | None = None
    #: The provider's own id for this tool call, echoed back with the result.
    call_id: str | None = None


class Decision(BaseModel):
    """The broker's verdict. Persisted as an event for audit (D4)."""

    request_id: str
    tool: str
    target_id: str
    scope: str
    outcome: DecisionOutcome
    reason: str
    risk: Risk | None = None
    never_auto: bool = False
    mode: PermissionMode
    #: For an ALLOW decision: which source the grant should be minted with.
    grant_source: GrantSource | None = None
    #: For an ALLOW decision already covered by a standing session grant: its id.
    existing_grant_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW


class AuthorizationGrant(BaseModel):
    """A real, persisted permission to run one tool call (D4)."""

    id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str | None = None
    tool: str
    target: str
    scope: str
    source: GrantSource
    granted_at: datetime = Field(default_factory=_now)
    expires_at: datetime | None = None
    used_at: datetime | None = None

    @property
    def reusable(self) -> bool:
        return self.source is GrantSource.SESSION

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _now()) >= self.expires_at

    def to_record(self) -> GrantRecord:
        return GrantRecord(
            id=self.id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            tool=self.tool,
            target=self.target,
            scope=self.scope,
            source=self.source.value,  # type: ignore[arg-type]
            granted_at=self.granted_at,
            expires_at=self.expires_at,
            used_at=self.used_at,
        )

    @classmethod
    def from_record(cls, record: GrantRecord) -> AuthorizationGrant:
        return cls(
            id=record.id,
            session_id=record.session_id,
            turn_id=record.turn_id,
            tool=record.tool,
            target=record.target,
            scope=record.scope or SCOPE_NONE,
            source=GrantSource(record.source),
            granted_at=record.granted_at,
            expires_at=record.expires_at,
            used_at=record.used_at,
        )


class PendingAuthorization(BaseModel):
    """A prompt awaiting a human (or a timeout)."""

    id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str | None
    tool: str
    target: str
    scope: str
    params: dict[str, Any]
    risk: Risk
    reason: str
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime | None = None

    def to_record(self) -> PendingAuthorizationRecord:
        return PendingAuthorizationRecord(
            id=self.id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            tool=self.tool,
            target=self.target,
            params=self.params,
            risk=str(self.risk),
            created_at=self.created_at,
            expires_at=self.expires_at,
            resolved_at=None,
            resolution=None,
        )


class Classification(BaseModel):
    """A `smart`-mode classifier's verdict on one specific call."""

    safe: bool
    confidence: float = 0.0
    reason: str = ""


#: Injected so tests (and offline operation) never need a model. Any error,
#: timeout, or low-confidence answer escalates to a prompt — fail closed.
Classifier = Callable[[ToolRequest, ToolSpec], Awaitable[Classification]]


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def nomad_source_root() -> Path:
    """Where Nomad's own running source lives (D21).

    Resolved from this module's location rather than from config, because a
    config value could be edited to point the rule somewhere harmless — and
    the whole purpose of the rule is that the running tree cannot exempt
    itself. If the package sits in a git checkout with a `pyproject.toml` the
    repo root is returned, so `tests/`, `nomad.toml` and `scripts/` are
    protected too; otherwise the installed package directory is the boundary.
    """
    package = Path(__file__).resolve().parent.parent
    repo_root = package.parent.parent
    if (repo_root / "pyproject.toml").is_file() and (repo_root / ".git").exists():
        return repo_root
    return package


def _resolve_unconfined(raw: str, workspace: Workspace) -> Path | None:
    """Normalise a path the way `Workspace.resolve` does, minus the boundary.

    Used only to ask "where did that path *actually* point" after the
    workspace has already rejected it. Returns `None` for anything
    unresolvable, and every caller treats `None` as "assume the worst".
    """
    if "\x00" in raw:
        return None
    try:
        candidate = Path(raw).expanduser()
        joined = candidate if candidate.is_absolute() else workspace.root / candidate
        return Path(os.path.normpath(str(joined))).resolve()
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - defensive
        return None


def _network_scope(spec: ToolSpec, params: dict[str, Any]) -> str:
    """Where an outbound call is going, as a grant scope (D31).

    A tool that names no URL parameter still transmits — a web search sends the
    query to a search engine — so it gets the bare `net:` scope rather than
    `none`. What it must never get is a scope that makes it look local.
    """
    if not spec.url_params:
        return SCOPE_NETWORK_PREFIX
    for name in spec.url_params:
        raw = params.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            return SCOPE_NETWORK_UNKNOWN
        host = urlparse(raw).hostname
        if not host:
            return SCOPE_NETWORK_UNKNOWN
        return f"{SCOPE_NETWORK_PREFIX}{host.lower()}"
    return SCOPE_NETWORK_UNKNOWN


def compute_scope(
    spec: ToolSpec, target: Target, params: dict[str, Any], workspace: Workspace
) -> str:  # noqa: C901 - one branch per scope kind reads better than a dispatch table
    """Derive the grant scope for one call (D14).

    Scope answers "what part of the world does this touch", coarsely enough
    that a session grant is useful and finely enough that it cannot leak:
    `workspace` never covers `outside`, and neither covers an SSH host.

    Both the broker and the executor call this, and the executor compares its
    result against the grant — so this function is the thing that makes a
    grant un-replayable against different params.
    """
    if target.kind is TargetKind.HID or Capability.HID_OUTPUT in target.capabilities:
        return SCOPE_HID
    if target.kind is TargetKind.SSH:
        # Never conflated with anything local. **Not yet per-host**: a real
        # `SshTarget` carries its own alias, but a `Bash("ssh prod ...")`
        # classified by `tools/egress.py` routes to one generic `ssh` target
        # id, so every remote command shares the scope `ssh:ssh`. That is safe
        # only because `never_auto_reason` blocks anything on an SSH target
        # before a standing grant is ever consulted — a single approval cannot
        # currently be replayed against a second host because there are no
        # standing approvals here at all.
        #
        # It stops being safe the moment either rule is relaxed. Parsing the
        # host out of the command and denying when it cannot be determined is
        # the fix, and it belongs with the real SSH target implementation.
        return f"ssh:{target.id}"
    if Permission.NETWORK in spec.permissions:
        # Transmitting is not reading (D31), and it is scoped by destination:
        # approving a fetch of one host must not authorize reaching another.
        return _network_scope(spec, params)
    if not spec.path_params:
        return SCOPE_NONE

    # Every path param is examined before settling, because `source_tree` is
    # stricter than `outside` and must win when a call touches both. Returning
    # on the first rejected path would let a second, source-tree path hide
    # behind it.
    source_root = nomad_source_root()
    escaped = False
    for name in spec.path_params:
        value = params.get(name)
        if value is None:
            continue
        try:
            workspace.resolve(str(value))
        except PermissionDenied:
            escaped = True
            resolved = _resolve_unconfined(str(value), workspace)
            if resolved is None or resolved == source_root or resolved.is_relative_to(source_root):
                # `None` means the path would not normalise at all; treat an
                # uninterpretable path as the most restricted scope, never the
                # least. Fail closed.
                return SCOPE_SOURCE_TREE
    return SCOPE_OUTSIDE if escaped else SCOPE_WORKSPACE


def _host_allowed(scope: str, allowed_hosts: frozenset[str]) -> bool:
    """Is this destination one the operator declared safe to reach unattended?

    Matches the host exactly or as a subdomain of an allowed entry, so
    `docs.python.org` is covered by `python.org`. `net:?` never matches — an
    unknown destination cannot be on a list of known ones.
    """
    if not allowed_hosts or not scope.startswith(SCOPE_NETWORK_PREFIX):
        return False
    host = scope[len(SCOPE_NETWORK_PREFIX) :]
    if not host or host == "?":
        return False
    return any(host == entry or host.endswith(f".{entry}") for entry in allowed_hosts)


def never_auto_reason(
    spec: ToolSpec,
    target: Target,
    scope: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
) -> str | None:
    """The `never_auto` rules (D14), in one place, checked before mode logic.

    Returns a human-readable reason, or `None` if the action may be
    auto-approved by a mode. Adding a mode cannot bypass this; adding a rule
    means adding a line here and nowhere else.

    `allowed_hosts` is the one relaxation, and it is data rather than a code
    path: hosts the operator has declared safe to reach unattended (D31).
    Empty by default, because a device that ships trusting somebody else's
    domain list is not fail-closed.
    """
    if spec.never_auto:
        return f"'{spec.name}' is declared never_auto"
    if target.kind is TargetKind.SSH:
        return f"any action on an SSH target ({target.id})"
    if (
        target.kind is TargetKind.HID
        or Capability.HID_OUTPUT in spec.required_capabilities
        or Permission.HID_OUTPUT in spec.permissions
    ):
        return "any HID output"
    if spec.risk is Risk.DESTRUCTIVE:
        return "the action is DESTRUCTIVE"
    if Permission.NETWORK in spec.permissions and not _host_allowed(scope, allowed_hosts):
        # D31. This sits with HID and SSH rather than with the mode logic for
        # the same reason they do: it is an effect on the world outside this
        # device, and a mode switch must not be able to unlock it. The device
        # sits in a pocket with the screen off, the model can read every file
        # on it, and a URL's query string is a fine place to put a secret —
        # so `auto` must not mean "and may post them anywhere".
        #
        # The designed relaxation is the host allowlist below, not deleting
        # this line. It is the same shape as the `CommandPolicy` planned for
        # `Bash`: narrow, declared, reviewable.
        return f"an outbound request to an unapproved host (scope={scope})"
    if scope == SCOPE_SOURCE_TREE and spec.permissions & ESCALATING_PERMISSIONS:
        # Stated separately from the general `outside` rule below, which would
        # also catch it today. D21 names this rule specifically, and it must
        # keep holding if the general one is ever relaxed.
        return (
            "writing to or executing in Nomad's own running source tree "
            "(use D22's self-update path)"
        )
    if spec.permissions & ESCALATING_PERMISSIONS and scope != SCOPE_WORKSPACE:
        return f"writes or exec outside the workspace root (scope={scope})"
    return None


# ---------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------


class PermissionBroker:
    """Turns a `ToolRequest` into a `Decision`, and a `Decision` into a grant."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        targets: TargetRegistry,
        workspace: Workspace,
        grants: GrantsRepository,
        bus: EventBus,
        config: NomadConfig,
        classifier: Classifier | None = None,
        classifier_timeout: float = DEFAULT_CLASSIFIER_TIMEOUT,
        classifier_confidence: float = DEFAULT_CLASSIFIER_CONFIDENCE,
        grant_ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
    ) -> None:
        self._tools = tools
        self._targets = targets
        self._workspace = workspace
        self._grants = grants
        self._bus = bus
        self._config = config
        self._classifier = classifier
        self._classifier_timeout = classifier_timeout
        self._classifier_confidence = classifier_confidence
        self._grant_ttl_seconds = grant_ttl_seconds
        self._allowed_network_hosts = frozenset(
            host.lower().lstrip(".") for host in config.tools.allowed_network_hosts if host.strip()
        )

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    async def decide(self, request: ToolRequest, mode: PermissionMode) -> Decision:
        """Evaluate one request. Never raises for a bad request — it denies."""
        await self._publish(
            EVENT_REQUESTED,
            {
                "request_id": request.id,
                "tool": request.tool,
                "target": request.target_id,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
            },
        )

        tool = self._tools.try_get(request.tool)
        if tool is None:
            return await self._finalize(
                request, mode, SCOPE_NONE, DecisionOutcome.DENY, "unknown tool"
            )
        spec = tool.spec

        if not self._tools.is_enabled(spec.name):
            return await self._finalize(
                request,
                mode,
                SCOPE_NONE,
                DecisionOutcome.DENY,
                f"'{spec.name}' is disabled in configuration",
                spec=spec,
            )

        target = self._targets.try_get(request.target_id)
        if target is None:
            return await self._finalize(
                request,
                mode,
                SCOPE_NONE,
                DecisionOutcome.DENY,
                f"unknown target '{request.target_id}'",
                spec=spec,
            )

        # --- capability check, BEFORE any permission logic (D12) -----------
        try:
            require_capabilities(target, spec.required_capabilities)
        except TargetError as exc:
            return await self._finalize(
                request, mode, SCOPE_NONE, DecisionOutcome.DENY, exc.message, spec=spec
            )

        try:
            spec.params_model.model_validate(request.params)
        except ValidationError as exc:
            return await self._finalize(
                request,
                mode,
                SCOPE_NONE,
                DecisionOutcome.DENY,
                f"invalid parameters for '{spec.name}': {exc.error_count()} error(s)",
                spec=spec,
            )

        scope = compute_scope(spec, target, request.params, self._workspace)

        # --- hard workspace boundary (D15) ---------------------------------
        if spec.workspace_confined and scope == SCOPE_OUTSIDE:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.DENY,
                "path resolves outside the workspace root",
                spec=spec,
            )

        # --- never_auto, before mode logic, in every mode (D14) ------------
        blocked = never_auto_reason(spec, target, scope, allowed_hosts=self._allowed_network_hosts)
        if blocked is not None:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.NEEDS_AUTH,
                f"never_auto: {blocked}",
                spec=spec,
                never_auto=True,
            )

        # --- a network call whose destination is unknown cannot be scoped ---
        if scope == SCOPE_NETWORK_UNKNOWN:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.DENY,
                "network destination could not be determined",
                spec=spec,
            )

        # --- Nomad's own screen and memory are not the world (D35) ---------
        # This sits *after* never_auto and after the network check, so it can
        # only ever widen the set of harmless local actions.
        #
        # The deadlock it breaks: `display_card` is MUTATING, so in the
        # default `manual` mode the model's first attempt to draw on its own
        # screen parked for 300s and auto-denied. The prompt it was waiting on
        # is itself drawn with a display tool, so the device could not ask for
        # permission to ask for permission. A safety model whose observable
        # behaviour on a stock boot is "hang, then refuse, having never asked"
        # is indistinguishable from a broken device, and it teaches the
        # operator to switch to `auto` — which is how a real guarantee dies.
        if spec.device_local and spec.risk is not Risk.DESTRUCTIVE:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.ALLOW,
                "confined to Nomad's own screen and memory",
                spec=spec,
                grant_source=GrantSource.AUTO,
            )

        # --- read-only inside the workspace auto-runs in all modes (D15) ---
        # `source_tree` is included: D21 forbids *writing* to Nomad's own
        # source, not reading it, and a device that has to be asked before it
        # may read its own code cannot usefully reason about itself (D22).
        #
        # **`NETWORK` is excluded, and that exclusion is load-bearing (D31).**
        # `READ_ONLY` conflates "reads local state" with "transmits", and a
        # tool that transmits is the one thing an auto-allow must not cover:
        # reading a file was already auto-allowed, so a model that could also
        # fetch a URL it chose could put anything it read into that URL and
        # send it out with the operator never seeing a prompt — in `manual`
        # mode. Read-only describes the effect on *this* machine, not on the
        # world.
        if (
            spec.risk is Risk.READ_ONLY
            and Permission.NETWORK not in spec.permissions
            and scope in (SCOPE_WORKSPACE, SCOPE_NONE, SCOPE_SOURCE_TREE)
        ):
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.ALLOW,
                "read-only action inside the workspace",
                spec=spec,
                grant_source=GrantSource.AUTO,
            )

        # --- a standing session grant already covers (tool, target, scope) --
        existing = await self._grants.find_session_grants(
            session_id=request.session_id,
            tool=spec.name,
            target=target.id,
            scope=scope,
            now=_now(),
        )
        if existing:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.ALLOW,
                "covered by a standing session grant",
                spec=spec,
                grant_source=GrantSource.SESSION,
                existing_grant_id=existing[0].id,
            )

        if mode is PermissionMode.AUTO:
            return await self._finalize(
                request,
                mode,
                scope,
                DecisionOutcome.ALLOW,
                "auto mode",
                spec=spec,
                grant_source=GrantSource.AUTO,
            )

        if mode is PermissionMode.SMART:
            outcome, reason = await self._classify(request, spec)
            return await self._finalize(
                request,
                mode,
                scope,
                outcome,
                reason,
                spec=spec,
                grant_source=GrantSource.MODEL if outcome is DecisionOutcome.ALLOW else None,
            )

        # manual and session both prompt; they differ only in the grant the
        # approval mints (see `default_grant_source`).
        return await self._finalize(
            request,
            mode,
            scope,
            DecisionOutcome.NEEDS_AUTH,
            f"{mode} mode requires approval for a {spec.risk} action",
            spec=spec,
        )

    async def _classify(self, request: ToolRequest, spec: ToolSpec) -> tuple[DecisionOutcome, str]:
        """`smart` mode. Fail closed on every unhappy path."""
        if self._classifier is None:
            return DecisionOutcome.NEEDS_AUTH, "smart mode: no classifier configured"
        try:
            verdict = await asyncio.wait_for(
                self._classifier(request, spec), timeout=self._classifier_timeout
            )
        except TimeoutError:
            return DecisionOutcome.NEEDS_AUTH, "smart mode: classifier timed out"
        except asyncio.CancelledError:  # pragma: no cover - propagate shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - any classifier failure escalates
            logger.warning("Classifier failed; escalating", extra={"error": str(exc)})
            return DecisionOutcome.NEEDS_AUTH, f"smart mode: classifier error ({exc})"

        if not isinstance(verdict, Classification):
            return DecisionOutcome.NEEDS_AUTH, "smart mode: classifier returned an unusable verdict"
        if not verdict.safe:
            return (
                DecisionOutcome.NEEDS_AUTH,
                f"smart mode: flagged by classifier ({verdict.reason})",
            )
        if verdict.confidence < self._classifier_confidence:
            return (
                DecisionOutcome.NEEDS_AUTH,
                f"smart mode: confidence {verdict.confidence:.2f} below "
                f"{self._classifier_confidence:.2f}",
            )
        return DecisionOutcome.ALLOW, f"smart mode: classified safe ({verdict.reason})"

    async def _finalize(
        self,
        request: ToolRequest,
        mode: PermissionMode,
        scope: str,
        outcome: DecisionOutcome,
        reason: str,
        *,
        spec: ToolSpec | None = None,
        never_auto: bool = False,
        grant_source: GrantSource | None = None,
        existing_grant_id: str | None = None,
    ) -> Decision:
        decision = Decision(
            request_id=request.id,
            tool=request.tool,
            target_id=request.target_id,
            scope=scope,
            outcome=outcome,
            reason=reason,
            risk=spec.risk if spec else None,
            never_auto=never_auto,
            mode=mode,
            grant_source=grant_source,
            existing_grant_id=existing_grant_id,
        )
        event = Event(
            type=EVENT_DECIDED,
            source="permission_broker",
            payload={
                "request_id": decision.request_id,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "tool": decision.tool,
                "target": decision.target_id,
                "scope": decision.scope,
                "outcome": str(decision.outcome),
                "reason": decision.reason,
                "risk": str(decision.risk) if decision.risk else None,
                "never_auto": decision.never_auto,
                "mode": str(decision.mode),
            },
        )
        await self._bus.publish(event)
        await self._grants.record_decision(event)
        logger.info(
            "Permission decision",
            extra={
                "tool": decision.tool,
                "target": decision.target_id,
                "scope": decision.scope,
                "outcome": str(decision.outcome),
                "mode": str(decision.mode),
            },
        )
        return decision

    @staticmethod
    def default_grant_source(mode: PermissionMode, *, scope_to_session: bool) -> GrantSource:
        """Which grant a human approval mints.

        `manual` mints a one-shot `HUMAN` grant unless the operator explicitly
        chose "this kind for the session"; `session` accumulates standing
        grants by default (D14).
        """
        if scope_to_session or mode is PermissionMode.SESSION:
            return GrantSource.SESSION
        return GrantSource.HUMAN

    async def authorize(self, request: ToolRequest, decision: Decision) -> AuthorizationGrant:
        """Produce the grant for an already-ALLOWED decision.

        Either reuses the standing session grant that allowed it — the grant
        really is the same object, not a copy — or mints a fresh one with the
        source the decision recorded. Refuses anything not ALLOW, so the loop
        cannot accidentally turn a prompt into a grant.
        """
        if decision.outcome is not DecisionOutcome.ALLOW:
            raise PermissionDenied(
                f"Cannot authorize a '{decision.outcome}' decision: {decision.reason}",
                {"tool": decision.tool, "target": decision.target_id},
            )
        if decision.existing_grant_id is not None:
            record = await self._grants.get_grant(decision.existing_grant_id)
            if record is not None:
                return AuthorizationGrant.from_record(record)
        source = decision.grant_source or GrantSource.AUTO
        return await self.mint_grant(request, decision, source=source)

    async def mint_grant(
        self,
        request: ToolRequest,
        decision: Decision,
        *,
        source: GrantSource,
        expires_in: float | None = None,
    ) -> AuthorizationGrant:
        """Mint and persist a grant for `decision`.

        Refuses to mint at all for a denied decision, and refuses to mint an
        automatic grant for a `never_auto` action — the second, independent
        enforcement of D14.
        """
        if decision.outcome is DecisionOutcome.DENY:
            raise PermissionDenied(
                f"Cannot mint a grant for a denied decision: {decision.reason}",
                {"tool": decision.tool, "target": decision.target_id},
            )
        if decision.never_auto and source in (GrantSource.AUTO, GrantSource.MODEL):
            raise PermissionDenied(
                f"never_auto action cannot be granted by '{source}': {decision.reason}",
                {"tool": decision.tool, "target": decision.target_id, "source": str(source)},
            )
        if decision.outcome is DecisionOutcome.NEEDS_AUTH and source is GrantSource.AUTO:
            raise PermissionDenied(
                "An action needing authorization cannot be auto-granted",
                {"tool": decision.tool, "target": decision.target_id},
            )

        ttl = self._grant_ttl_seconds if expires_in is None else expires_in
        expires_at: datetime | None
        if source is GrantSource.SESSION:
            expires_at = None if expires_in is None else _now() + timedelta(seconds=expires_in)
        else:
            expires_at = _now() + timedelta(seconds=ttl)

        grant = AuthorizationGrant(
            session_id=request.session_id,
            turn_id=request.turn_id,
            tool=decision.tool,
            target=decision.target_id,
            scope=decision.scope,
            source=source,
            expires_at=expires_at,
        )
        await self._grants.save_grant(grant.to_record())
        await self._publish(
            EVENT_GRANTED,
            {
                "grant_id": grant.id,
                "request_id": request.id,
                "session_id": grant.session_id,
                "turn_id": grant.turn_id,
                "tool": grant.tool,
                "target": grant.target,
                "scope": grant.scope,
                "source": str(grant.source),
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            },
        )
        return grant

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(type=event_type, source="permission_broker", payload=payload))


# ---------------------------------------------------------------------------
# pending authorization queue
# ---------------------------------------------------------------------------


class AuthorizationQueue:
    """Human-in-the-loop half of the pipeline.

    The agent loop parks on `request()`; a UI resolves it with `approve()` or
    `deny()`. A prompt nobody answers times out and auto-**denies** — the
    failure mode of an unattended pocket device must be "did nothing", never
    "did it anyway".
    """

    def __init__(
        self,
        *,
        broker: PermissionBroker,
        grants: GrantsRepository,
        bus: EventBus,
        default_timeout: float = DEFAULT_AUTHORIZATION_TIMEOUT,
    ) -> None:
        self._broker = broker
        self._grants = grants
        self._bus = bus
        self._default_timeout = default_timeout
        self._pending: dict[str, PendingAuthorization] = {}
        self._waiters: dict[str, asyncio.Future[tuple[Resolution, AuthorizationGrant | None]]] = {}
        self._context: dict[str, tuple[ToolRequest, Decision]] = {}

    def list_pending(self) -> list[PendingAuthorization]:
        return list(self._pending.values())

    async def request(
        self,
        request: ToolRequest,
        decision: Decision,
        *,
        spec: ToolSpec,
        timeout: float | None = None,
    ) -> tuple[Resolution, AuthorizationGrant | None]:
        """Park until a human resolves this, or the timeout auto-denies it."""
        wait_for = self._default_timeout if timeout is None else timeout
        pending = PendingAuthorization(
            session_id=request.session_id,
            turn_id=request.turn_id,
            tool=request.tool,
            target=request.target_id,
            scope=decision.scope,
            params=request.params,
            risk=spec.risk,
            reason=decision.reason,
            expires_at=_now() + timedelta(seconds=wait_for),
        )
        await self._grants.create_pending(pending.to_record())
        self._pending[pending.id] = pending
        self._context[pending.id] = (request, decision)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[Resolution, AuthorizationGrant | None]] = loop.create_future()
        self._waiters[pending.id] = future

        await self._publish(
            EVENT_AUTH_PENDING,
            {
                "pending_id": pending.id,
                "session_id": pending.session_id,
                "turn_id": pending.turn_id,
                "tool": pending.tool,
                "target": pending.target,
                "scope": pending.scope,
                "risk": str(pending.risk),
                "reason": pending.reason,
                "params": pending.params,
                "expires_at": pending.expires_at.isoformat() if pending.expires_at else None,
            },
        )

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=wait_for)
        except TimeoutError:
            await self._settle(pending.id, Resolution.TIMEOUT, "authorization timed out")
            return Resolution.TIMEOUT, None
        finally:
            self._pending.pop(pending.id, None)
            self._waiters.pop(pending.id, None)
            self._context.pop(pending.id, None)

    async def approve(
        self, pending_id: str, *, scope_to_session: bool = False, mode: PermissionMode
    ) -> AuthorizationGrant:
        request, decision = self._require_context(pending_id)
        source = PermissionBroker.default_grant_source(mode, scope_to_session=scope_to_session)
        grant = await self._broker.mint_grant(request, decision, source=source)
        await self._settle(pending_id, Resolution.APPROVED, "approved", grant=grant)
        return grant

    async def deny(self, pending_id: str, reason: str = "denied by operator") -> None:
        self._require_context(pending_id)
        await self._settle(pending_id, Resolution.DENIED, reason)

    def _require_context(self, pending_id: str) -> tuple[ToolRequest, Decision]:
        context = self._context.get(pending_id)
        if context is None:
            raise PermissionDenied(
                f"No authorization is awaiting id '{pending_id}'", {"pending_id": pending_id}
            )
        return context

    async def _settle(
        self,
        pending_id: str,
        resolution: Resolution,
        reason: str,
        *,
        grant: AuthorizationGrant | None = None,
    ) -> None:
        await self._grants.resolve_pending(pending_id, resolution.value, _now())  # type: ignore[arg-type]
        future = self._waiters.get(pending_id)
        if future is not None and not future.done():
            future.set_result((resolution, grant))
        await self._publish(
            EVENT_AUTH_RESOLVED,
            {
                "pending_id": pending_id,
                "resolution": str(resolution),
                "reason": reason,
                "grant_id": grant.id if grant else None,
            },
        )

    async def expire_all(self, session_id: str) -> int:
        """Mark every persisted unresolved prompt as expired (used at boot)."""
        rows = await self._grants.list_unresolved(session_id)
        for row in rows:
            await self._grants.resolve_pending(row.id, "expired", _now())
        return len(rows)

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(
            Event(type=event_type, source="authorization_queue", payload=payload)
        )


# ---------------------------------------------------------------------------
# grant vault
# ---------------------------------------------------------------------------

#: How long a stashed grant stays claimable. Long enough to cover a round trip
#: out to the backend and back, short enough that it is not a replay window.
DEFAULT_VAULT_TTL = 120.0


def canonical_key(tool: str, params: dict[str, Any]) -> str:
    """Stable key for one (tool, arguments) pair.

    `sort_keys` plus `default=str` makes the key stable across dict ordering
    and tolerant of non-JSON values, rather than raising on them — a key
    function that can throw would turn an odd argument into an outage.
    """
    body = json.dumps(params, sort_keys=True, default=str)
    return f"{tool}:{hashlib.sha256(body.encode()).hexdigest()}"


class GrantVault:
    """Short-lived, single-use holding area for grants awaiting execution.

    Exists because approval and execution can be separated by a process
    boundary: when the model's tool call is authorized here but executed after
    a round trip through an MCP server, something has to carry the grant
    across, or `ToolExecutor.run(grant, ...)` would have to be relaxed into
    `run(request)` — which is precisely the collapse D4 forbids.

    Single-use and TTL-bounded, both load-bearing: a grant that could be popped
    twice, or popped an hour later, is a replay window straight through the
    pipeline. Keyed on the *arguments* as well as the tool, so a call whose
    params changed after approval finds nothing.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_VAULT_TTL) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[AuthorizationGrant, ToolRequest, datetime]] = {}

    def stash(self, grant: AuthorizationGrant, request: ToolRequest) -> str:
        key = canonical_key(request.tool, request.params)
        self._entries[key] = (grant, request, _now() + timedelta(seconds=self._ttl))
        return key

    def pop(
        self, tool: str, params: dict[str, Any]
    ) -> tuple[AuthorizationGrant, ToolRequest] | None:
        self._expire()
        entry = self._entries.pop(canonical_key(tool, params), None)
        if entry is None:
            return None
        grant, request, _ = entry
        return grant, request

    def _expire(self) -> None:
        now = _now()
        for key in [k for k, (_, _, expires) in self._entries.items() if expires <= now]:
            del self._entries[key]

    def __len__(self) -> int:
        self._expire()
        return len(self._entries)


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Runs a tool. Takes a `Grant`, never a bare request (D4).

    `run()` is the only public entry point and its first positional parameter
    is the grant. There is deliberately no `execute(request)` convenience
    method — the absence is the design.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        targets: TargetRegistry,
        workspace: Workspace,
        grants: GrantsRepository,
        bus: EventBus,
        config: NomadConfig,
    ) -> None:
        self._tools = tools
        self._targets = targets
        self._workspace = workspace
        self._grants = grants
        self._bus = bus
        self._config = config

    async def run(self, grant: AuthorizationGrant, request: ToolRequest) -> ToolResult:
        """Validate the grant against the request, then execute.

        Raises `PermissionDenied` if the grant does not authorize exactly this
        call. A tool that fails *during* execution returns a failed
        `ToolResult` instead — a broken tool is not a permission problem.
        """
        spec, target = self._validate(grant, request)

        await self._grants.mark_grant_used(grant.id, _now())
        grant.used_at = grant.used_at or _now()

        await self._publish(
            EVENT_EXECUTING,
            {
                "grant_id": grant.id,
                "request_id": request.id,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "tool": spec.name,
                "target": target.id,
                "scope": grant.scope,
            },
        )

        tool = self._tools.get(spec.name)
        ctx = ToolContext(
            target=target,
            workspace=self._workspace,
            session_id=request.session_id,
            turn_id=request.turn_id,
            logger=get_logger(
                f"nomad.tools.{spec.name}",
                session_id=request.session_id,
                turn_id=request.turn_id,
            ),
            timeout_seconds=float(self._config.tools.command_timeout_seconds),
        )

        try:
            params = spec.params_model.model_validate(request.params)
            result = await tool.execute(params, ctx)
        except ValidationError as exc:
            result = ToolResult.failure(f"invalid parameters: {exc.error_count()} error(s)")
        except NotImplementedError as exc:
            result = ToolResult.failure(str(exc), not_implemented=True)
        except NomadError as exc:
            result = ToolResult.failure(exc.message, **exc.details)
        except asyncio.CancelledError:  # pragma: no cover - propagate shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - a broken tool must not kill the loop
            logger.error(
                "Tool raised an unexpected exception",
                extra={"tool": spec.name, "error": str(exc)},
            )
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")

        await self._publish(
            EVENT_RESULT,
            {
                "grant_id": grant.id,
                "request_id": request.id,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "tool": spec.name,
                "target": target.id,
                "ok": result.ok,
                "error": result.error,
            },
        )
        return result

    def _validate(self, grant: AuthorizationGrant, request: ToolRequest) -> tuple[ToolSpec, Target]:
        if not isinstance(grant, AuthorizationGrant):
            raise PermissionDenied(
                "ToolExecutor.run requires an AuthorizationGrant",
                {"got": type(grant).__name__},
            )

        tool = self._tools.try_get(request.tool)
        if tool is None:
            raise ToolError(f"Unknown tool '{request.tool}'", {"tool": request.tool})
        spec = tool.spec

        if not self._tools.is_enabled(spec.name):
            raise PermissionDenied(
                f"'{spec.name}' is disabled in configuration", {"tool": spec.name}
            )

        target = self._targets.get(request.target_id)
        # Capability check again: cheap, and it means a hand-built grant can
        # never be used to point a filesystem tool at a HID device (D12).
        require_capabilities(target, spec.required_capabilities)

        now = _now()
        checks: list[tuple[bool, str]] = [
            (grant.tool == request.tool, "grant is for a different tool"),
            (grant.target == request.target_id, "grant is for a different target"),
            (grant.session_id == request.session_id, "grant belongs to a different session"),
            (
                grant.turn_id is None or grant.turn_id == request.turn_id,
                "grant belongs to a different turn",
            ),
            (not grant.is_expired(now), "grant has expired"),
            (grant.used_at is None or grant.reusable, "grant has already been used"),
        ]
        for ok, why in checks:
            if not ok:
                raise PermissionDenied(
                    f"Grant does not authorize this call: {why}",
                    {"grant_id": grant.id, "tool": request.tool, "target": request.target_id},
                )

        scope = compute_scope(spec, target, request.params, self._workspace)
        if scope != grant.scope:
            raise PermissionDenied(
                "Grant does not authorize this call: scope changed since it was granted",
                {"grant_id": grant.id, "granted_scope": grant.scope, "request_scope": scope},
            )
        return spec, target

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(type=event_type, source="tool_executor", payload=payload))
