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


class AgentConfig(BaseModel):
    mode: PermissionMode = PermissionMode.MANUAL
    max_tool_calls_per_turn: int = 25
    compact_at: float = 0.75


class WorkspaceConfig(BaseModel):
    root: str = "var/workspace"
    follow_symlinks_outside_root: bool = False


class ToolsConfig(BaseModel):
    enable_run_command: bool = False
    command_timeout_seconds: int = 120


class CloudConfig(BaseModel):
    model: str = "claude-sonnet-5"
    api_key_env: str = "ANTHROPIC_API_KEY"


class LocalConfig(BaseModel):
    enabled: bool = False
    model_path: str = ""


class AiConfig(BaseModel):
    provider: str = "mock"
    routing: str = "static"
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)


class DisplayConfig(BaseModel):
    driver: str = "mock"
    width: int = 320
    height: int = 240


class UsbHidConfig(BaseModel):
    driver: str = "mock"


class BatteryConfig(BaseModel):
    driver: str = "mock"
    low_threshold: int = 20
    critical_threshold: int = 8


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
    repeat_delay_ms: int = 400
    repeat_interval_ms: int = 120


class InputConfig(BaseModel):
    buttons: InputButtonsConfig = Field(default_factory=InputButtonsConfig)
    joystick: InputJoystickConfig = Field(default_factory=InputJoystickConfig)


class NomadConfig(BaseModel):
    core: CoreConfig = Field(default_factory=CoreConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    usb_hid: UsbHidConfig = Field(default_factory=UsbHidConfig)
    battery: BatteryConfig = Field(default_factory=BatteryConfig)
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
