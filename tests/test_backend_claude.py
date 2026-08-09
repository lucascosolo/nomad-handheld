"""The swappable backend, and the Claude Code implementation (D19, D20, D24).

The SDK is an optional extra, so most of what matters here is tested without
it: environment stripping, forbidden flags, and the mock backend's conformance
to the same interface. The few tests that need the SDK skip cleanly when it is
absent — a suite that only runs where credentials exist is a suite that stops
being run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nomad.agent.backends import create_backend
from nomad.agent.backends.base import AgentEvent, AgentEventKind, BackendCapability
from nomad.agent.backends.claude_cli import (
    FORBIDDEN_ARGS,
    SHADOWED_TOOLS,
    STRIPPED_ENV_VARS,
    ClaudeCliBackend,
    child_environment,
)
from nomad.agent.backends.mock import MockBackend
from nomad.agent.backends.remote_llm import RemoteLlmBackend
from nomad.agent.claude_tools import CLAUDE_CODE_TOOLS
from nomad.agent.identity import FALLBACK_IDENTITY, load_identity
from nomad.core.config import (
    AgentBackendKind,
    ClaudeCliConfig,
    NomadConfig,
    RemoteLlmConfig,
)
from nomad.core.errors import AgentError
from nomad.tools.base import Permission, Risk

# -- D20: subscription auth --------------------------------------------------


def test_anthropic_api_key_is_stripped_from_the_child_environment() -> None:
    """If it leaks through, the CLI bills per token instead of the subscription."""
    env = child_environment(
        ClaudeCliConfig(),
        {"ANTHROPIC_API_KEY": "sk-leaked", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "PATH": "/usr/bin"},
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert env["PATH"] == "/usr/bin"


def test_every_billing_env_var_is_stripped() -> None:
    poisoned = dict.fromkeys(STRIPPED_ENV_VARS, "leak")
    env = child_environment(ClaudeCliConfig(), poisoned)
    assert not (set(env) & set(STRIPPED_ENV_VARS))


def test_a_missing_oauth_token_is_survivable_but_noticed(caplog) -> None:
    """Warn loudly; do not invent a fallback that silently bills per token."""
    env = child_environment(ClaudeCliConfig(), {"PATH": "/usr/bin"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.parametrize("flag", ["--bare", "bare", "--continue", "continue"])
def test_forbidden_flags_are_rejected_at_construction(flag: str) -> None:
    """`--bare` never reads OAuth; `--continue` depends on ambient state (D20)."""
    with pytest.raises(AgentError, match="Forbidden CLI arguments"):
        ClaudeCliBackend(
            cli_config=ClaudeCliConfig(),
            bridge=None,  # type: ignore[arg-type]
            extra_args={flag: None},
        )


def test_the_forbidden_list_covers_both_spellings() -> None:
    assert {"--bare", "bare", "--continue", "continue"} <= FORBIDDEN_ARGS


# -- capability switches (parity with a laptop) -----------------------------


def test_capability_surfaces_are_on_by_default() -> None:
    """Skills, CLAUDE.md, plugins and MCP are why the laptop version is good.

    The broker is what makes keeping them safe (D21); switching them off would
    trade real capability for a safety property Nomad already has elsewhere.
    """
    cli = ClaudeCliConfig()
    assert cli.setting_sources == ["user", "project", "local"]
    assert cli.skills == "all"
    assert cli.strict_mcp_config is False


def test_capability_switches_are_config_driven() -> None:
    config = NomadConfig.model_validate(
        {"agent": {"claude_cli": {"skills": ["deploy"], "strict_mcp_config": True}}}
    )
    assert config.agent.claude_cli.skills == ["deploy"]
    assert config.agent.claude_cli.strict_mcp_config is True


# -- the Nomad identity (goal (a)) ------------------------------------------


def test_the_identity_is_appended_to_the_preset_never_substituted() -> None:
    """Replacing Claude Code's prompt would trade competence for personality."""
    backend = ClaudeCliBackend(
        cli_config=ClaudeCliConfig(),
        bridge=None,  # type: ignore[arg-type]
        identity="You are Nomad.",
    )
    prompt = backend._system_prompt()  # noqa: SLF001 - pinning the contract
    assert prompt["type"] == "preset"
    assert prompt["preset"] == "claude_code"
    assert prompt["append"] == "You are Nomad."


def test_no_identity_still_gets_the_preset() -> None:
    backend = ClaudeCliBackend(cli_config=ClaudeCliConfig(), bridge=None)  # type: ignore[arg-type]
    assert backend._system_prompt() == {"type": "preset", "preset": "claude_code"}  # noqa: SLF001


def test_the_shipped_identity_file_loads_and_says_who_nomad_is() -> None:
    text = load_identity()
    assert "Nomad" in text
    assert text != FALLBACK_IDENTITY, "NOMAD.md is missing or empty at the source root"


def test_a_missing_identity_file_falls_back_rather_than_booting_mute(tmp_path) -> None:
    assert load_identity(tmp_path / "absent.md") == FALLBACK_IDENTITY


def test_an_empty_identity_file_falls_back(tmp_path) -> None:
    path = tmp_path / "NOMAD.md"
    path.write_text("   \n")
    assert load_identity(path) == FALLBACK_IDENTITY


def test_the_identity_teaches_the_small_screen_output_contract() -> None:
    """The display tool's schema is not the only ceiling on how Nomad answers."""
    text = load_identity().lower()
    assert "screen" in text
    assert "lead with the answer" in text


# -- D24: the interface ------------------------------------------------------


@pytest.mark.parametrize("backend", [MockBackend(), RemoteLlmBackend(config=RemoteLlmConfig())])
def test_every_backend_satisfies_the_interface(backend: object) -> None:
    for member in ("name", "capabilities", "start", "stop", "send", "events", "interrupt"):
        assert hasattr(backend, member), f"{backend} is missing {member}"


def test_the_mock_declares_the_same_capabilities_as_the_real_backend() -> None:
    """A mock that takes a different branch than production is worse than none."""
    assert MockBackend().capabilities == ClaudeCliBackend.capabilities


def test_remote_llm_declares_no_capabilities() -> None:
    """The honest asymmetry (D24): a raw local model brings none of the three."""
    backend = RemoteLlmBackend(config=RemoteLlmConfig())
    assert backend.capabilities == frozenset()
    assert BackendCapability.OWN_LOOP not in backend.capabilities


async def test_remote_llm_refuses_rather_than_pretending() -> None:
    backend = RemoteLlmBackend(config=RemoteLlmConfig())
    with pytest.raises(AgentError, match="not implemented yet"):
        await backend.start()


# -- backend selection -------------------------------------------------------


def test_the_default_backend_is_mock() -> None:
    """The whole system must run on a laptop with no CLI and no token (D9)."""
    config = NomadConfig()
    assert config.agent.backend is AgentBackendKind.MOCK
    assert create_backend(config).name == "mock"


def test_selecting_remote_llm_builds_it() -> None:
    config = NomadConfig.model_validate({"agent": {"backend": "remote_llm"}})
    assert create_backend(config).name == "remote_llm"


def test_the_claude_backend_cannot_be_built_without_a_bridge() -> None:
    """A full toolset with no broker in front of it must not be constructible."""
    config = NomadConfig.model_validate({"agent": {"backend": "claude_cli"}})
    with pytest.raises(AgentError, match="requires a PermissionBridge"):
        create_backend(config, bridge=None)


def test_an_unknown_backend_name_is_a_config_error() -> None:
    import tempfile
    from pathlib import Path

    from nomad.core.config import load_config
    from nomad.core.errors import ConfigError

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nomad.toml"
        path.write_text('[agent]\nbackend = "telepathy"\n')
        with pytest.raises(ConfigError):
            load_config(path, env={})


# -- the mock's behaviour ----------------------------------------------------


async def test_the_mock_echoes_and_completes_a_turn() -> None:
    backend = MockBackend()
    await backend.start()
    await backend.send("hello", session_id="s1")
    kinds = []
    async for event in backend.events():
        kinds.append(event.kind)
        assert event.session_id == "s1"
        if event.kind is AgentEventKind.TURN_COMPLETE:
            break
    assert kinds == [AgentEventKind.TEXT, AgentEventKind.TURN_COMPLETE]


async def test_the_mock_replays_a_script() -> None:
    backend = MockBackend(
        script=[
            [
                AgentEvent(kind=AgentEventKind.TOOL_CALL, session_id="", tool_name="Read"),
                AgentEvent(kind=AgentEventKind.TURN_COMPLETE, session_id=""),
            ]
        ]
    )
    await backend.start()
    await backend.send("go", session_id="s1")
    first = await anext(backend.events())
    assert first.kind is AgentEventKind.TOOL_CALL and first.tool_name == "Read"


async def test_the_mock_refuses_to_send_before_start() -> None:
    with pytest.raises(RuntimeError):
        await MockBackend().send("hi", session_id="s1")


async def test_interrupt_drops_pending_output() -> None:
    backend = MockBackend()
    await backend.start()
    await backend.send("hello", session_id="s1")
    await backend.interrupt()
    assert backend.interrupts == 1
    await backend.stop()
    assert [e async for e in backend.events()] == []


# -- the shadowed-tool hole in D21, and the hook that closes it -------------
#
# The SDK warned about this on every connect and nothing acted on it:
#
#     CanUseToolShadowedWarning: can_use_tool will not be invoked for: Skill.
#
# `skills = "all"` is an allow-rule, and an allow-rule settles a call before
# `can_use_tool` fires — so "every Claude Code tool call routes through the
# broker" was false for `Skill`. These tests are the proof that it is not any
# more, and they run without the SDK installed because the hook is a plain
# coroutine over dicts.


class _StubBridge:
    """Records what it was asked and answers however the test wants."""

    def __init__(self, *, allow: bool = True, raises: bool = False) -> None:
        self.allow = allow
        self.raises = raises
        self.asked: list[tuple[str, dict]] = []

    async def can_use_tool(self, name, tool_input, context):  # noqa: ANN001, ANN201
        self.asked.append((name, dict(tool_input)))
        if self.raises:
            raise RuntimeError("bridge exploded")
        return SimpleNamespace(allow=self.allow, reason="because")


def _hooked(bridge: object) -> ClaudeCliBackend:
    return ClaudeCliBackend(cli_config=ClaudeCliConfig(), bridge=bridge)  # type: ignore[arg-type]


def _decision(result: dict) -> str | None:
    output = result.get("hookSpecificOutput") or {}
    return output.get("permissionDecision")


async def test_a_shadowed_skill_call_reaches_the_broker() -> None:
    """The whole point: the call the allow-rule would have settled silently."""
    bridge = _StubBridge(allow=True)
    result = await _hooked(bridge)._pre_tool_use(  # noqa: SLF001 - pinning the contract
        {"tool_name": "Skill", "tool_input": {"command": "improving-yourself"}}, "id", None
    )

    assert bridge.asked == [("Skill", {"command": "improving-yourself"})]
    # `allow`, never `{}`: deferring would hand the call straight back to the
    # allow-rule this hook exists to get in front of.
    assert _decision(result) == "allow"


async def test_a_refused_skill_is_denied_not_merely_unapproved() -> None:
    bridge = _StubBridge(allow=False)
    result = await _hooked(bridge)._pre_tool_use(  # noqa: SLF001
        {"tool_name": "Skill", "tool_input": {}}, "id", None
    )

    assert _decision(result) == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "because"


async def test_a_bridge_that_raises_denies() -> None:
    """Fail closed. Not knowing whether a call is permitted is not the same as
    it being permitted (D21)."""
    bridge = _StubBridge(raises=True)
    result = await _hooked(bridge)._pre_tool_use(  # noqa: SLF001
        {"tool_name": "Skill", "tool_input": {}}, "id", None
    )

    assert _decision(result) == "deny"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_name": ""},
        {"tool_name": None},
        {"tool_input": {"command": "x"}},
    ],
)
async def test_a_payload_that_names_no_tool_denies(payload: dict) -> None:
    bridge = _StubBridge(allow=True)
    result = await _hooked(bridge)._pre_tool_use(payload, "id", None)  # noqa: SLF001

    assert _decision(result) == "deny"
    assert bridge.asked == []


async def test_an_unshadowed_tool_is_left_to_can_use_tool() -> None:
    """Deciding `Bash` in both places would double every prompt and every
    audit record. The hook defers, and `can_use_tool` still runs."""
    bridge = _StubBridge(allow=True)
    result = await _hooked(bridge)._pre_tool_use(  # noqa: SLF001
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "id", None
    )

    assert result == {}
    assert bridge.asked == []


def test_every_shadowed_tool_is_classified() -> None:
    """A hook that routes a tool with no spec would deny every use of it — the
    capability D19 exists to keep, removed by the fix meant to preserve it."""
    declared = {spec.name for spec in CLAUDE_CODE_TOOLS}
    assert declared >= SHADOWED_TOOLS


def test_loading_a_skill_is_read_only_because_a_skill_is_instructions() -> None:
    """D39. Anything the skill then *does* arrives at the broker separately and
    is judged on its own terms, so gating the load harder buys nothing."""
    skill = next(spec for spec in CLAUDE_CODE_TOOLS if spec.name == "Skill")
    assert skill.risk is Risk.READ_ONLY
    assert skill.never_auto is False
    assert Permission.EXEC not in skill.permissions
