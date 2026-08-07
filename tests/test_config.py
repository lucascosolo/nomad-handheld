from __future__ import annotations

from pathlib import Path

import pytest

from nomad.core.config import NomadConfig, PermissionMode, load_config
from nomad.core.errors import ConfigError


def test_load_real_nomad_toml() -> None:
    """The actual committed nomad.toml must parse and validate cleanly."""
    repo_root = Path(__file__).resolve().parents[1]
    config = load_config(repo_root / "nomad.toml", env={})

    assert isinstance(config, NomadConfig)
    assert config.core.name == "nomad"
    assert config.api.port == 8080
    assert config.agent.mode == PermissionMode.MANUAL
    assert config.transports.esp32.baudrate == 921600
    assert config.input.buttons.a == "CONFIRM"


def test_local_toml_overrides_base(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[core]\nname = "base"\n\n[api]\nport = 8080\n')
    local = tmp_path / "nomad.local.toml"
    local.write_text('[api]\nport = 9999\n')

    config = load_config(base, env={})

    assert config.core.name == "base"
    assert config.api.port == 9999


def test_env_overrides_local_and_base(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[api]\nport = 8080\n')
    local = tmp_path / "nomad.local.toml"
    local.write_text('[api]\nport = 9999\n')

    config = load_config(base, env={"NOMAD_API__PORT": "7000"})

    assert config.api.port == 7000


def test_env_var_nested_creates_sections(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[core]\nname = "base"\n')

    config = load_config(base, env={"NOMAD_STORAGE__PATH": "custom/path.db"})

    assert config.storage.path == "custom/path.db"


def test_env_var_bool_and_int_coercion(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[core]\nname = "base"\n')

    config = load_config(
        base,
        env={
            "NOMAD_TOOLS__ENABLE_RUN_COMMAND": "true",
            "NOMAD_AGENT__MAX_TOOL_CALLS_PER_TURN": "50",
        },
    )

    assert config.tools.enable_run_command is True
    assert config.agent.max_tool_calls_per_turn == 50


def test_missing_base_file_raises_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigError):
        load_config(missing, env={})


def test_invalid_permission_mode_raises_config_error(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[agent]\nmode = "not-a-real-mode"\n')

    with pytest.raises(ConfigError) as exc_info:
        load_config(base, env={})

    assert "agent" in str(exc_info.value).lower() or "mode" in str(exc_info.value).lower()


def test_unknown_top_level_key_raises_config_error(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[totally_unknown_section]\nfoo = "bar"\n')

    with pytest.raises(ConfigError):
        load_config(base, env={})


def test_defaults_used_when_only_minimal_file_present(tmp_path: Path) -> None:
    base = tmp_path / "nomad.toml"
    base.write_text('[core]\nname = "minimal"\n')

    config = load_config(base, env={})

    assert config.core.name == "minimal"
    assert config.display.width == 320
    assert config.battery.low_threshold == 20
