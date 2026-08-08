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

from pydantic import BaseModel, Field, ValidationError

from nomad.core.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("nomad.toml")
LOCAL_CONFIG_SUFFIX = ".local.toml"
ENV_PREFIX = "NOMAD_"


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


class SettingsConfig(BaseModel):
    """Self-configuration (D26)."""

    overrides_path: str = "nomad.local.toml"
    audit_history: int = 100


class DisplayConfig(BaseModel):
    driver: str = "mock"
    width: int = 320
    height: int = 240


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
    kind: str = "mock"
    port: str = ""
    baudrate: int = 115200


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
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
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
        if not key.startswith(ENV_PREFIX):
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
