"""What Nomad would say if you asked how he is.

A top-level module for the same reason `app.py` is one: a status report reaches
across every package — the session, the queue, the ledger, the drivers — and
the layering table lets exactly two kinds of module do that, the composition
root and a view onto it. `tests/test_layering.py` puts `<root>` at the top on
purpose; this is the second thing to live there.

Three properties are deliberate:

* **Collection never starts anything and never writes.** It reads a constructed
  app. That is what lets `nomad status` report on a device without binding the
  view's port, opening a serial link, or racing a Nomad that is already
  running.
* **One report, three renderings.** The CLI text, the screen card, and the JSON
  are three views of one `StatusReport`, so the screen cannot drift from the
  terminal. A device that says one thing on its glass and another on the wire
  is a device you have to check twice.
* **The backend probe answers "could a turn happen", not "is it configured".**
  `backend = "claude_cli"` in a file is a claim; a CLI on `PATH` that answers
  `--version` and a credential the process can actually see is evidence. The
  gap between those two is exactly the failure this module exists to surface,
  because from the outside it looks identical to a device that is thinking.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from nomad.core.config import ClaudeCliConfig, NomadConfig
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle only exists for type checkers
    from nomad.app import NomadApp

logger = get_logger(__name__)

#: The screen handle the status card draws under. A fifth writer, arbitrated by
#: the same `ScreenOwner` as the other four (D36) — the boot card must never
#: paint over a pending authorization prompt.
STATUS_WRITER = "status"

#: How long the CLI gets to answer `--version` before the probe gives up. A
#: status command that hangs on a wedged binary is worse than one that says the
#: binary is wedged.
VERSION_PROBE_TIMEOUT_S = 10.0

#: Where the CLI keeps subscription credentials on Linux. Checked for presence
#: only — never read, never parsed, never logged. Knowing a file exists is
#: enough to answer "will auth be there", and it is all this module wants to
#: know about a secret.
#:
#: **`~/.claude.json` is deliberately not in this list.** The installer writes
#: it on a machine nobody has ever logged in on, so counting it reported
#: `auth: cli-credentials` and `ready` for a freshly provisioned Pi whose CLI
#: answers `Not logged in · Please run /login` — a device that looks healthy
#: and can do nothing, which is the single failure this module exists to
#: prevent. Only the credential file itself counts.
_CLI_CREDENTIAL_PATHS = (".claude/.credentials.json",)


class BackendHealth(BaseModel):
    """Whether the configured backend could actually run a turn."""

    backend: str
    ready: bool
    detail: str
    version: str | None = None
    #: Where a credential was found, never what it was. `unknown` is honest:
    #: the CLI may hold auth somewhere this probe does not look, so it is not
    #: reported as `none` and does not by itself make the backend unready.
    auth: str = "unknown"


class BatteryStatusReport(BaseModel):
    percent: float
    charging: bool = False
    voltage: float | None = None


class StatusReport(BaseModel):
    """One snapshot of the device. Rendered three ways, never assembled twice."""

    name: str
    state: str
    backend: BackendHealth
    mode: str
    session_id: str
    current_turn_id: str | None = None

    display_driver: str
    surfaces: list[str] = Field(default_factory=list)
    view_url: str | None = None
    prompter: str

    battery: BatteryStatusReport | None = None

    tools: int = 0
    tool_names: list[str] = Field(default_factory=list)
    skills: int = 0
    skill_names: list[str] = Field(default_factory=list)

    notifications_pending: int | None = None
    offline_enabled: bool = False
    offline_proposals: int | None = None

    workloads: dict[str, str] = Field(default_factory=dict)
    components: dict[str, str] = Field(default_factory=dict)

    workspace: str = ""
    database: str = ""
    #: What went wrong while collecting, if anything. A status report that
    #: raises tells the operator nothing; one that says "battery: unreadable"
    #: tells them where to look.
    problems: list[str] = Field(default_factory=list)


async def probe_backend(config: NomadConfig) -> BackendHealth:
    """Can this device begin a turn? Answered by looking, not by reading config.

    Kept free of `NomadApp` so it is callable before anything is constructed —
    a boot that fails on a missing CLI should be diagnosable by the same code
    path that reports a healthy one.
    """
    backend = config.agent.backend
    if backend == "mock":
        return BackendHealth(
            backend=backend,
            ready=True,
            detail="mock backend: no subprocess, no network, no credentials (D9)",
            auth="not-required",
        )
    if backend == "remote_llm":
        base_url = config.agent.remote_llm.base_url
        return BackendHealth(
            backend=backend,
            ready=False,
            detail=(f"remote_llm is not implemented yet (D24); base_url={base_url or 'unset'}"),
            auth="not-required",
        )
    return await _probe_claude_cli(config.agent.claude_cli)


async def _probe_claude_cli(cli: ClaudeCliConfig) -> BackendHealth:
    if not _sdk_installed():
        return BackendHealth(
            backend="claude_cli",
            ready=False,
            detail="the Agent SDK is not installed: pip install 'nomad[agent]'",
        )
    resolved = shutil.which(cli.cli_path) if cli.cli_path else None
    if resolved is None and cli.cli_path and Path(cli.cli_path).is_file():
        resolved = cli.cli_path
    if resolved is None:
        return BackendHealth(
            backend="claude_cli",
            ready=False,
            detail=f"the Claude Code CLI is not on PATH (looked for {cli.cli_path!r})",
        )

    version = await _cli_version(resolved)
    auth = _auth_source(cli)
    # A credentials file on disk is not a working credential. The device
    # reported `auth: cli-credentials` and `ready` while the CLI answered
    # `loggedIn: false` and every turn failed with "OAuth session expired" —
    # the exact shape of failure this probe exists to catch, produced by the
    # probe itself. So ask the CLI rather than the filesystem when there is a
    # CLI to ask.
    if auth == "cli-credentials" and await _cli_logged_in(resolved) is False:
        auth = "expired"
    if version is None:
        return BackendHealth(
            backend="claude_cli",
            ready=False,
            detail=f"{resolved} did not answer --version within {VERSION_PROBE_TIMEOUT_S:.0f}s",
            auth=auth,
        )

    notes = [f"CLI {version} at {resolved}"]
    if cli.expected_cli_version and cli.expected_cli_version not in version:
        # A mismatch logs loudly and does not fail: a newer CLI usually still
        # works, and refusing to start over a version string would make the
        # device brittle against an upgrade nobody asked for (D20).
        notes.append(f"expected {cli.expected_cli_version}")
    if auth in ("none", "expired"):
        notes.append(
            "credentials on disk have expired; re-run `claude auth login` on the device"
            if auth == "expired"
            else f"not logged in; run `claude` and /login, or set {cli.oauth_token_env}"
        )
    return BackendHealth(
        backend="claude_cli",
        ready=auth not in ("none", "expired"),
        detail="; ".join(notes),
        version=version,
        auth=auth,
    )


def _sdk_installed() -> bool:
    """Presence, by spec lookup rather than import.

    Importing it here would put `claude_agent_sdk` in a second module and
    `tests/test_sdk_boundary.py` would be right to fail: the rule is one
    importer (D24), and asking whether a package exists is not importing it.
    """
    import importlib.util

    return importlib.util.find_spec("claude_agent_sdk") is not None


async def _cli_logged_in(path: str) -> bool | None:
    """Ask the CLI whether its credential still works. `None` if it cannot say.

    `None` and `False` are deliberately different. Not being able to ask —
    an old CLI without the subcommand, a timeout — is not evidence of being
    logged out, and downgrading the report on it would make a slow device look
    unauthenticated. Only an explicit `loggedIn: false` counts.
    """
    output = await _run_cli(path, "auth", "status")
    if output is None:
        return None
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    value = parsed.get("loggedIn") if isinstance(parsed, dict) else None
    return bool(value) if isinstance(value, bool) else None


async def _cli_version(path: str) -> str | None:
    return await _run_cli(path, "--version")


async def _run_cli(path: str, *args: str) -> str | None:
    """One short read-only CLI call. `None` for anything that is not an answer.

    Bounded by the same timeout as the version probe, because this runs on the
    boot path and on `nomad status`: a CLI that hangs must cost a few seconds
    and an unknown, never the screen coming up.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("Could not run the CLI", extra={"path": path, "error": str(exc)})
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERSION_PROBE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip() or None


def _auth_source(cli: ClaudeCliConfig) -> str:
    """Where a credential was found. Never what it is.

    `ANTHROPIC_API_KEY` deliberately does not count as auth here even when it
    is set, because D20 strips it from the child environment: reporting it as
    the device's credential would describe an authentication that cannot
    happen.
    """
    if os.environ.get(cli.oauth_token_env):
        return "oauth-env"
    home = Path.home()
    for relative in _CLI_CREDENTIAL_PATHS:
        if (home / relative).exists():
            return "cli-credentials"
    return "none"


async def collect_status(app: NomadApp, *, probe: bool = True) -> StatusReport:
    """Read a constructed app. Starts nothing, writes nothing, raises nothing.

    `probe=False` skips the subprocess, which is what the boot path wants: the
    device already knows whether its backend connected, and spawning a CLI to
    ask it a second time would put a several-second stall between the screen
    coming up and the screen saying anything.
    """
    config = app.config
    problems: list[str] = []

    if probe:
        backend = await probe_backend(config)
    else:
        backend = BackendHealth(
            backend=config.agent.backend,
            ready=app.session.state is ComponentState.STARTED,
            detail=f"session {app.session.state.value}",
        )

    report = StatusReport(
        name=config.core.name,
        state=app.state.value,
        backend=backend,
        mode=str(app.session.mode),
        # A session that has not started has no id, and asking for one raises.
        # That is the normal case for `nomad status` — reporting a device is
        # not starting it — so "not started" is a value here, not an error.
        session_id=(
            app.session.session_id
            if app.session.state is ComponentState.STARTED
            else "(not started)"
        ),
        current_turn_id=app.session.current_turn_id,
        display_driver=config.display.driver,
        surfaces=list(config.display.surfaces),
        view_url=app.view_url,
        prompter=type(app.prompter).__name__,
        tools=len(app.agent_tools),
        tool_names=sorted(tool.spec.name for tool in app.agent_tools),
        skills=len(app.skills.names()) if app.skills is not None else 0,
        skill_names=sorted(app.skills.names()) if app.skills is not None else [],
        offline_enabled=app.offline is not None,
        workloads=(
            {entry.name: f"{entry.tier}/{entry.state}" for entry in app.governor.inventory()}
            if app.governor is not None
            else {}
        ),
        components={name: state.value for name, state in app.states().items()},
        workspace=str(config.workspace.root),
        database=str(config.storage.path),
    )

    # Everything below reads a live subsystem and so can fail on a device that
    # is half up. Each one degrades to a named problem rather than taking the
    # whole report down — this command's whole job is being usable when
    # something is broken.
    report.battery = await _read_battery(app, problems)
    report.notifications_pending = await _count(
        app.notifications.pending_count() if app.notifications is not None else None,
        "notifications",
        problems,
    )
    report.offline_proposals = await _count(
        app.intent_ledger.proposals() if app.intent_ledger is not None else None,
        "offline proposals",
        problems,
    )
    # Assigned last, and not passed to the constructor. Pydantic *copies* a
    # list it validates, so a `problems=problems` at construction time would
    # leave the report holding a snapshot taken before a single subsystem had
    # been read — every degradation below silently invisible, which is the one
    # failure mode this field exists to prevent.
    report.problems = problems
    return report


async def _read_battery(app: NomadApp, problems: list[str]) -> BatteryStatusReport | None:
    try:
        reading = await app.battery.read()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - an unreadable battery is a line, not a crash
        problems.append(f"battery unreadable: {type(exc).__name__}: {exc}")
        return None
    return BatteryStatusReport(
        percent=float(getattr(reading, "percent", 0.0)),
        charging=bool(getattr(reading, "charging", False)),
        voltage=getattr(reading, "voltage", None),
    )


async def _count(awaitable: Any, label: str, problems: list[str]) -> int | None:
    """Await something that answers a number or a list, or record why it did not."""
    if awaitable is None:
        return None
    try:
        result = await awaitable
    except Exception as exc:  # noqa: BLE001 - same rule as the battery
        problems.append(f"{label} unreadable: {type(exc).__name__}: {exc}")
        return None
    return result if isinstance(result, int) else len(result)


# -- renderings ------------------------------------------------------------


def status_rows(report: StatusReport) -> list[tuple[str, str]]:
    """The card body: short labels, short values, no wrapping.

    Ordered by what an operator standing over a 320x240 panel needs first —
    can it think, who is allowed to say yes, and how long the battery lasts —
    rather than by the order the fields happen to be declared in.
    """
    rows = [
        ("state", report.state),
        ("backend", f"{report.backend.backend} {'ok' if report.backend.ready else 'DOWN'}"),
        ("mode", report.mode),
    ]
    if report.battery is not None:
        charge = "chg" if report.battery.charging else "bat"
        rows.append(("power", f"{report.battery.percent:.0f}% {charge}"))
    rows.append(("tools", str(report.tools)))
    if report.skills:
        rows.append(("skills", str(report.skills)))
    if report.notifications_pending:
        rows.append(("pending", str(report.notifications_pending)))
    if report.offline_proposals:
        rows.append(("proposals", str(report.offline_proposals)))
    if report.problems:
        rows.append(("problems", str(len(report.problems))))
    return rows


def render_status_text(report: StatusReport, *, verbose: bool = False) -> str:
    """The terminal rendering. Plain text, aligned, no colour.

    No colour because this is the output the operator will read over SSH, in a
    log, and pasted into a message — three places where escape codes are noise.
    """
    readiness = "ready" if report.backend.ready else "NOT READY"
    lines = [
        f"{report.name} — {report.state}",
        "",
        _line("backend", f"{report.backend.backend} ({readiness})"),
        _line("", report.backend.detail),
        _line("auth", report.backend.auth),
        _line("mode", report.mode),
        _line("session", report.session_id),
    ]
    if report.current_turn_id:
        lines.append(_line("turn", f"{report.current_turn_id} (in flight)"))
    lines.append(_line("display", f"{report.display_driver} [{', '.join(report.surfaces)}]"))
    lines.append(_line("prompter", report.prompter))
    if report.view_url:
        lines.append(_line("screen", report.view_url))
    if report.battery is not None:
        state = "charging" if report.battery.charging else "on battery"
        lines.append(_line("power", f"{report.battery.percent:.0f}% ({state})"))
    lines.append(_line("tools", str(report.tools)))
    lines.append(_line("skills", str(report.skills)))
    if report.notifications_pending is not None:
        lines.append(_line("pending", str(report.notifications_pending)))
    if report.offline_proposals is not None:
        lines.append(_line("offline", f"on, {report.offline_proposals} proposals waiting"))
    elif not report.offline_enabled:
        lines.append(_line("offline", "off"))
    if report.workloads:
        lines.append(_line("workloads", ", ".join(f"{n}={s}" for n, s in report.workloads.items())))
    lines.append(_line("workspace", report.workspace))

    if verbose:
        lines.append("")
        lines.append("components:")
        lines.extend(f"  {name:<22} {state}" for name, state in report.components.items())
        lines.append("")
        lines.append("tools:")
        lines.extend(f"  {name}" for name in report.tool_names)
        if report.skill_names:
            lines.append("")
            lines.append("skills:")
            lines.extend(f"  {name}" for name in report.skill_names)

    if report.problems:
        lines.append("")
        lines.append("problems:")
        lines.extend(f"  ! {problem}" for problem in report.problems)
    return "\n".join(lines)


def render_status_json(report: StatusReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)


def _line(label: str, value: str) -> str:
    return f"  {label:<10} {value}" if label else f"  {'':<10} {value}"
