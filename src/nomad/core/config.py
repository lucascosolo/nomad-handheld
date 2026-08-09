"""Layered TOML configuration (D8).

Load order: `nomad.toml` (committed defaults) -> `nomad.local.toml`
(gitignored, optional, per-device) -> `NOMAD_*` environment variables.
Dicts are deep-merged in that order, then validated into `NomadConfig`.

Env vars use double underscore for nesting, e.g. `NOMAD_API__PORT=9000`
maps to `config["api"]["port"] = 9000`. Values are parsed with a best-effort
scalar coercion (bool/int/float/str) since env vars are always strings.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from nomad.core.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("nomad.toml")
LOCAL_CONFIG_SUFFIX = ".local.toml"
ENV_PREFIX = "NOMAD_"

#: Points at a config file other than `./nomad.toml`. The layered local
#: override and the `NOMAD_*` overrides still apply on top of whatever it
#: names.
CONFIG_PATH_ENV = "NOMAD_CONFIG"

#: `NOMAD_*` variables that select *which* config to read rather than setting a
#: value inside one. Without this, `NOMAD_CONFIG=/etc/nomad.toml` parses as an
#: override of a top-level key named `config` and `extra="forbid"` rejects the
#: whole file — so pointing at a config was a startup failure, and the only
#: reason nobody hit it is that nothing had ever set the variable.
NON_OVERRIDE_ENV_VARS = frozenset({CONFIG_PATH_ENV})


class PermissionMode(StrEnum):
    """Session permission mode (D14)."""

    MANUAL = "manual"
    SESSION = "session"
    SMART = "smart"
    AUTO = "auto"


class CoreConfig(BaseModel):
    name: str = "nomad"
    data_dir: str = "var"
    log_level: str = "INFO"
    log_format: str = "console"


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class StorageConfig(BaseModel):
    path: str = "var/nomad.db"


class AgentBackendKind(StrEnum):
    """Which implementation runs the loop (D24)."""

    MOCK = "mock"
    CLAUDE_CLI = "claude_cli"
    REMOTE_LLM = "remote_llm"


class ClaudeCliConfig(BaseModel):
    """Claude Code headless backend (D19, D20).

    The OAuth token is named here, never stored here.

    The three capability switches below default to *permissive* on purpose.
    Nomad's goal is that the handheld is as effective at answering questions
    and writing code as Claude Code on a laptop, and skills, `CLAUDE.md`,
    plugins and MCP servers are a real part of why the laptop is effective.
    Capability was never the thing to cripple — the broker is (D21).
    """

    cli_path: str = "claude"
    expected_cli_version: str = "2.1.224"
    oauth_token_env: str = "CLAUDE_CODE_OAUTH_TOKEN"
    model: str = "claude-sonnet-5"
    #: Where the CLI reads settings, `CLAUDE.md` and plugins from. Empty means
    #: "nothing but this config", which is fast and predictable and also
    #: switches off most of what makes the tool good.
    setting_sources: list[str] = Field(default_factory=lambda: ["user", "project", "local"])
    #: `"all"`, or an explicit list of skill names. Skills add prompts and
    #: workflows, not ungated tools — they run through the same built-ins the
    #: broker already classifies.
    skills: str | list[str] = "all"
    #: False lets the operator's own MCP servers load. Their tools still have
    #: no declaration in `agent/claude_tools.py`, so the bridge denies them
    #: until one is added — fail-closed survives the relaxation (D21).
    strict_mcp_config: bool = False
    #: Nomad's identity, *appended* to Claude Code's preset rather than
    #: replacing it — the preset is why the laptop version is good at the work
    #: (D19). Empty means the source root's `NOMAD.md`.
    identity_path: str = ""


class RemoteLlmConfig(BaseModel):
    """A model on the tailnet (D24). Not implemented yet."""

    base_url: str = ""
    model: str = ""


class AgentConfig(BaseModel):
    backend: AgentBackendKind = AgentBackendKind.MOCK
    mode: PermissionMode = PermissionMode.MANUAL
    max_tool_calls_per_turn: int = 25
    # Ignored by backends that declare OWN_COMPACTION (D24).
    compact_at: float = 0.75
    claude_cli: ClaudeCliConfig = Field(default_factory=ClaudeCliConfig)
    remote_llm: RemoteLlmConfig = Field(default_factory=RemoteLlmConfig)


class MemoryConfig(BaseModel):
    """The memory Nomad owns, and the ceiling on what it costs per turn.

    Every limit here exists because memory has two failure modes and they pull
    in opposite directions. Forgetting everything at a session boundary was the
    original defect. Remembering everything *into the prompt* is the subtler
    one: an injected block that grows with the store is a tax charged on every
    request in the session, and an overloaded context makes the model write
    worse code and invent things. So injection is bounded by count and by
    characters, only pinned memories are injected, and pins themselves are
    capped — the rest of the store is reached with `recall`.
    """

    enabled: bool = True
    #: Total rows before the store evicts the least valuable unpinned one.
    max_memories: int = 500
    #: The pinned set is the whole of what memory costs per turn, so it is
    #: small and deliberate. Pinning past this is refused, not silently grown.
    max_pinned: int = 12
    #: Hard ceiling on the injected block. Whichever of these two binds first
    #: wins; both are independent of how large the store has grown.
    injection_budget_chars: int = 600
    injection_max_memories: int = 8
    #: Rows one `recall` may return. Recall output lands in context too.
    recall_limit: int = 5
    #: Session rollover thresholds (`memory/rollover.py`). A backend session
    #: resumed continuously for six months is untested and gets slower every
    #: week; memory is what makes starting a fresh one survivable. Zero or
    #: below disables that half of the policy.
    session_max_age_hours: int = 168
    session_max_turns: int = 500


class NotificationsConfig(BaseModel):
    """The durable queue, and the two ceilings that keep it usable.

    Both limits exist because a notification queue fails in one direction far
    more often than the other. Nobody complains that the device forgot to nag
    them; they stop opening the list once it is forty rows of things that
    already happened. So resolved rows are capped and pending rows are not —
    a pending notification is a promise the device made, and the cap may only
    ever fall on rows that already fired.
    """

    enabled: bool = True
    #: Resolved rows kept for audit before the oldest are pruned. Pending rows
    #: are never pruned, whatever this says.
    max_history: int = 200
    #: How many due notifications one poll hands to a sink. A device that was
    #: off for a week should wake up to a screenful, not to a stampede.
    max_deliveries_per_poll: int = 10
    #: Rows one list call returns. This lands in the model's context.
    list_limit: int = 20
    #: Default window on a timer or reminder before it is swept as stale, in
    #: minutes. Showing a tea timer forty minutes late is worse than silence.
    #: Zero disables the default; a caller may always pass its own expiry.
    default_expiry_minutes: int = 240


class UtilitiesConfig(BaseModel):
    """Timers, notes, stopwatches, conversions — the offline tier's seed corpus.

    Nothing here reaches the network or a model, so the only limits worth
    configuring are the ones that stop the device's own storage from becoming
    the problem. `max_timer_hours` is not a safety bound; it is a typo bound.
    """

    enabled: bool = True
    #: Longest timer that is plausibly a timer rather than a slipped decimal
    #: point. Past this, use an alarm with a date.
    max_timer_hours: int = 48
    max_notes: int = 500
    #: Notes returned by one search. Note bodies land in context.
    note_search_limit: int = 10
    #: Characters of a note body shown in a search result, before the full
    #: note has to be asked for by id.
    note_preview_chars: int = 200
    max_stopwatches: int = 8


class OfflineConfig(BaseModel):
    """The offline tier, and the numbers that are not a matching dial (chunk O).

    **Nothing here loosens matching, and nothing here ever will.** The router
    matches exactly or defers to the model, so there is no cutoff, no tolerance
    and no similarity setting to raise — a phrase starts being answered onboard
    by being added to `offline/catalog.py` in a diff, never by a config edit.
    Every number below is about *evidence and pacing*: how much history it takes
    before Nomad asks the operator a question, and how often it looks.

    `promote_after_asks` and `promote_after_days` are two conditions, not one
    with a fudge factor. Frequency alone promotes a phrase somebody said eight
    times in one frustrated afternoon; the day count is what separates a habit
    from an incident, and the operator is being asked to rule on the habit.
    """

    #: Read by the composition root, which builds a responder or does not.
    enabled: bool = True
    #: How many times a phrase must be asked before it can be proposed.
    promote_after_asks: int = 5
    #: ...and on how many distinct days, so an incident is not a habit.
    promote_after_days: int = 3
    #: Bound on the evidence table. A handheld is not a data warehouse, and a
    #: log of every sentence ever said to it is a privacy problem as well as a
    #: size one.
    max_tracked_asks: int = 500
    #: Longest utterance treated as evidence. A paragraph is not a phrase
    #: anybody repeats, so counting it only stores the operator's prose.
    max_ask_chars: int = 200
    #: Proposals waiting at once. An operator staring at a backlog stops
    #: reading them, and a proposal nobody reads trains the habit of dismissing
    #: whatever the device suggests.
    max_open_proposals: int = 5
    #: How often the analyst looks, in seconds. It is opportunistic work (D38)
    #: and yields to any live turn, so this is a floor on cost, not a promise.
    analysis_interval_seconds: float = 900.0


class WorkspaceConfig(BaseModel):
    root: str = "var/workspace"
    follow_symlinks_outside_root: bool = False


class ToolsConfig(BaseModel):
    enable_run_command: bool = False
    command_timeout_seconds: int = 120
    #: Hosts an outbound request may reach without asking, in any mode (D31).
    #: Empty by default: a device that ships trusting somebody else's domain
    #: list is not fail-closed. Subdomains of an entry are covered, so
    #: `"python.org"` also allows `docs.python.org`.
    allowed_network_hosts: list[str] = Field(default_factory=list)


class AppsConfig(BaseModel):
    """Self-authored apps (D25)."""

    root: str = "var/apps"
    smoke_test_seconds: int = 5
    restart_on_crash: bool = False


class SkillsConfig(BaseModel):
    """Progressive disclosure for knowledge (D39).

    `index_budget_chars` is the load-bearing number. The index is injected on
    every turn, so it is the one part of the skill system whose cost scales
    with the size of the library rather than with use — which is exactly the
    tax D33 removed from memory and this must not reintroduce.

    Two roots, because skills have two origins. `root` is where *authored*
    skills go — under `var/`, writable and gitignored (D25). `seed_root` is the
    committed library that ships with the device, so a fresh checkout is not a
    device that has forgotten how to do everything. Seeds load first and
    authored skills load over them, so a name collision resolves in favour of
    the operator without anyone editing the source tree.
    """

    enabled: bool = True
    root: str = "var/skills"
    #: `None` means "the `skills/` directory beside NOMAD.md", resolved by
    #: `nomad.skills.default_seed_root()`. Set it to a path when that guess is
    #: wrong (an installed rather than a checked-out tree), or to `""` to ship
    #: no seeds at all and run purely on authored skills.
    seed_root: str | None = None
    #: Raised from 800 when the seventh seed (`improving-yourself`) landed and
    #: the shipped index stopped fitting. The default and `nomad.toml` have to
    #: move together — `tests/test_skills.py` pins the shipped set against
    #: *this* number, not the file's, so a device with no config still boots
    #: with a whole index rather than a truncated one.
    index_budget_chars: int = 1000


class SettingsConfig(BaseModel):
    """Self-configuration (D26)."""

    overrides_path: str = "nomad.local.toml"
    audit_history: int = 100


class DisplayConfig(BaseModel):
    #: The primary surface — the device's own face.
    driver: str = "mock"
    #: Additional surfaces the same screen state is mirrored onto, in order.
    #: Empty means "one screen", which is the behaviour that predates this and
    #: stays the default. `headless` here is how an HDMI monitor or a phone
    #: gets the same view over HTTP without a second renderer existing (D36
    #: still arbitrates writers; mirroring is beneath it).
    mirror: list[str] = Field(default_factory=list)
    width: int = 320
    height: int = 240

    @model_validator(mode="after")
    def _no_duplicate_surfaces(self) -> DisplayConfig:
        """Two surfaces of the same kind would double every write for no gain,
        and for `esp32` specifically would mean two drivers on one link."""
        names = [self.driver, *self.mirror]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"display surfaces must be distinct; repeated: {sorted(duplicates)}")
        return self

    @property
    def surfaces(self) -> list[str]:
        """Every surface, primary first."""
        return [self.driver, *self.mirror]


class ViewConfig(BaseModel):
    """Serving the headless screen over loopback so a human can watch it (D9).

    Only meaningful while `[display].driver` is a headless one — an ESP32 has
    its own glass and needs no browser. `host` is validated at start and
    refused if it is not a loopback address: the API's auth problem is
    deliberately deferred, and this must not quietly become the network
    service that gets there first.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8081
    #: How often the browser re-fetches. Not a stream; a refresh is enough for
    #: a screen that changes a few times a second at most.
    refresh_seconds: float = 1.0


class UsbHidConfig(BaseModel):
    driver: str = "mock"


class BatteryConfig(BaseModel):
    driver: str = "mock"
    low_threshold: int = 20
    critical_threshold: int = 8


class AudioConfig(BaseModel):
    """Voice drivers (D37). Each category is selected independently, since a
    device can gain a real speaker long before it has a real transcriber."""

    recorder_driver: str = "mock"
    speaker_driver: str = "mock"
    transcriber_driver: str = "mock"
    synthesizer_driver: str = "mock"
    #: Hard ceiling on one `push_to_talk` capture, seconds. A stuck key still
    #: cannot record forever — enforced by the driver, not by the operator
    #: releasing the button (D37).
    max_record_seconds: float = 30.0


class ResourcesConfig(BaseModel):
    """When background work yields the machine, and how long it gets (D38).

    Three numbers, and each one is a different failure being bounded.

    `suspend_deadline_seconds` is how long a workload has to park after being
    asked. Too short kills workloads that were about to cooperate; too long is
    a device that stays laggy for that many seconds after the operator speaks.

    `terminate_grace_seconds` is the window after cancellation before the
    governor gives up and abandons the task. It is short because by this point
    the workload has already broken its contract twice.

    `resume_delay_seconds` is hysteresis, and the only one that is not about
    misbehaviour. Turns arrive in bursts; resuming an indexer in the gap
    between two of them pays a full suspend/resume cycle per turn for a
    fraction of a second of work.
    """

    #: Read by the composition root, which registers workloads or does not.
    enabled: bool = True
    suspend_deadline_seconds: float = 5.0
    terminate_grace_seconds: float = 1.0
    resume_delay_seconds: float = 3.0


class CameraConfig(BaseModel):
    driver: str = "mock"


class SensorsConfig(BaseModel):
    driver: str = "mock"


class TransportConfig(BaseModel):
    #: `mock` | `loopback` | `serial`. Mock is the default everywhere (D9).
    kind: str = "mock"
    port: str = ""
    baudrate: int = 115200

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        known = ("mock", "loopback", "serial")
        if value not in known:
            raise ValueError(f"transport kind must be one of {known}, got '{value}'")
        return value

    @model_validator(mode="after")
    def _serial_needs_a_port(self) -> TransportConfig:
        """A serial transport with no port is a misconfiguration, not a default.

        Catching it here means the error names the config file at startup,
        rather than surfacing much later as a transport that cannot open.
        """
        if self.kind == "serial" and not self.port:
            raise ValueError("a serial transport requires a non-empty 'port'")
        return self


class TransportsConfig(BaseModel):
    esp32: TransportConfig = Field(
        default_factory=lambda: TransportConfig(port="/dev/ttyACM0", baudrate=921600)
    )
    rp2040: TransportConfig = Field(
        default_factory=lambda: TransportConfig(port="/dev/ttyACM1", baudrate=115200)
    )


class InputButtonsConfig(BaseModel):
    a: str = "CONFIRM"
    b: str = "BACK"
    x: str = "ACTION_1"
    y: str = "ACTION_2"

    model_config = {"extra": "allow"}


class InputJoystickConfig(BaseModel):
    deadzone: float = 0.25
    #: Fraction of `deadzone` a stick must fall back below before it counts as
    #: centred again. Without this a stick resting on the threshold chatters
    #: between "centred" and "up" and a menu scrolls on its own.
    hysteresis: float = Field(default=0.7, gt=0.0, le=1.0)


class InputRepeatConfig(BaseModel):
    """Hold-to-repeat timing, shared by buttons and the stick.

    Deliberately not under `[input.joystick]`, where it started: a held *button*
    repeats too, and reading button timing out of the joystick's config is the
    kind of thing that is merely odd until someone wants the two to differ and
    finds they cannot. Neither field mentions the stick; only its location did.
    """

    delay_ms: int = 400
    interval_ms: int = 120


class InputConfig(BaseModel):
    # Registered in addition to the core action set, which is always present
    # and cannot be removed (D13, D26).
    extra_actions: list[str] = Field(default_factory=list)
    buttons: InputButtonsConfig = Field(default_factory=InputButtonsConfig)
    joystick: InputJoystickConfig = Field(default_factory=InputJoystickConfig)
    repeat: InputRepeatConfig = Field(default_factory=InputRepeatConfig)


class NomadConfig(BaseModel):
    core: CoreConfig = Field(default_factory=CoreConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    utilities: UtilitiesConfig = Field(default_factory=UtilitiesConfig)
    offline: OfflineConfig = Field(default_factory=OfflineConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    view: ViewConfig = Field(default_factory=ViewConfig)
    usb_hid: UsbHidConfig = Field(default_factory=UsbHidConfig)
    battery: BatteryConfig = Field(default_factory=BatteryConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    transports: TransportsConfig = Field(default_factory=TransportsConfig)
    input: InputConfig = Field(default_factory=InputConfig)

    model_config = {"extra": "forbid"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce_scalar(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn NOMAD_FOO__BAR=1 into {"foo": {"bar": 1}}."""
    overrides: dict[str, Any] = {}
    for key, raw_value in env.items():
        if not key.startswith(ENV_PREFIX) or key in NON_OVERRIDE_ENV_VARS:
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        if not path or path == [""]:
            continue
        cursor = overrides
        for segment in path[:-1]:
            cursor = cursor.setdefault(segment, {})
            if not isinstance(cursor, dict):
                # A scalar was set where a nesting level is now required;
                # env var layout is ambiguous, skip it silently rather than crash.
                break
        else:
            cursor[path[-1]] = _coerce_scalar(raw_value)
    return overrides


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse TOML file {path}", {"path": str(path)}) from exc
    except OSError as exc:
        raise ConfigError(f"Failed to read config file {path}", {"path": str(path)}) from exc


def load_config(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> NomadConfig:
    """Load layered config: `path` -> `<path stem>.local.toml` -> env vars.

    `env` is injected explicitly so tests never need to touch `os.environ`
    globally. Defaults to an empty mapping (no env overrides), not the real
    process environment — callers that want real env vars must pass
    `os.environ` explicitly.
    """
    base_path = path or DEFAULT_CONFIG_PATH
    env = env or {}

    merged: dict[str, Any] = {}
    if base_path.exists():
        merged = _load_toml(base_path)
    elif path is not None:
        raise ConfigError(f"Config file not found: {base_path}", {"path": str(base_path)})

    local_path = base_path.parent / f"{base_path.stem}.local.toml"
    if local_path.exists():
        merged = _deep_merge(merged, _load_toml(local_path))

    merged = _deep_merge(merged, _env_overrides(env))

    try:
        return NomadConfig.model_validate(merged)
    except ValidationError as exc:
        offending = ", ".join(
            ".".join(str(p) for p in error["loc"]) for error in exc.errors()
        )
        raise ConfigError(
            f"Config validation failed for key(s): {offending}",
            {"errors": exc.errors()},
        ) from exc
