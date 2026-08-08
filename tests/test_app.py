"""The composition root actually assembles a device (chunk F1).

These are the tests that were missing while 443 others passed on a machine
that had never been switched on: that the parts start together in the right
order, that a failed boot leaves nothing running, and that the screen is
reachable.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nomad.app import NomadApp
from nomad.core.config import NomadConfig
from nomad.core.errors import ConfigError, LifecycleError
from nomad.core.lifecycle import ComponentState
from nomad.hardware.errors import HardwareError
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.input.choice import NullChoicePrompter
from nomad.storage.migrations import MIGRATIONS, current_version
from nomad.view.authprompt import AUTH_PROMPT_WRITER


def _config(tmp_path: Path, **overrides: object) -> NomadConfig:
    data: dict[str, object] = {
        "storage": {"path": str(tmp_path / "nomad.db")},
        "workspace": {"root": str(tmp_path / "workspace")},
        # Port 0 so concurrent runs never collide on a fixed port.
        "view": {"enabled": True, "port": 0},
    }
    data.update(overrides)
    return NomadConfig.model_validate(data)


async def test_the_app_starts_and_stops_against_mocks(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.state is ComponentState.STARTED
        states = app.states()
        assert states["database"] is ComponentState.STARTED
        assert states["event_bus"] is ComponentState.STARTED
        assert states["agent_session"] is ComponentState.STARTED
        assert states["view_renderer"] is ComponentState.STARTED
    finally:
        await app.stop()

    assert app.state is ComponentState.STOPPED
    assert app.states()["database"] is ComponentState.STOPPED
    assert app.states()["agent_session"] is ComponentState.STOPPED


async def test_the_database_is_migrated_after_start(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert await current_version(app.db) == len(MIGRATIONS)
        assert app.migrator.version == len(MIGRATIONS)
    finally:
        await app.stop()


async def test_stop_is_safe_before_start(tmp_path: Path) -> None:
    """A device that never got as far as booting still has to power down."""
    app = NomadApp(_config(tmp_path))
    await app.stop()
    assert app.state is ComponentState.STOPPED


async def test_stop_after_a_failed_partial_start_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case on a handheld: something halfway up refuses."""
    app = NomadApp(_config(tmp_path))

    async def boom() -> None:
        raise RuntimeError("no glass")

    monkeypatch.setattr(app.renderer, "start", boom)

    with pytest.raises(LifecycleError):
        await app.start()
    assert app.state is ComponentState.FAILED

    # Rollback already stopped what had started; stopping again must be inert.
    await app.stop()
    assert app.state is ComponentState.STOPPED
    assert app.states()["database"] is ComponentState.STOPPED
    # The session never started, so it was never rolled back either.
    assert app.states()["agent_session"] is ComponentState.NEW


async def test_a_failed_start_leaves_no_socket_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view binds a real port; a later failure must give it back."""
    app = NomadApp(_config(tmp_path))
    assert app.view is not None

    async def boom() -> None:
        raise RuntimeError("session refused")

    monkeypatch.setattr(app.session, "start", boom)

    with pytest.raises(LifecycleError):
        await app.start()
    assert app.states()["view_server"] is ComponentState.STOPPED


async def test_the_model_and_the_renderer_share_one_screen(tmp_path: Path) -> None:
    """Two screens would be worse than none: the operator would see the wrong one.

    They now share it *through* one `ScreenOwner` rather than by holding the
    driver each — same single screen, plus an arbiter that can hand it to an
    authorization prompt (D36).
    """
    app = NomadApp(_config(tmp_path))
    assert isinstance(app.display, HeadlessDisplay)
    display_tool = next(t for t in app.hardware_tools if t.spec.name == "display_text")
    assert display_tool._display._owner is app.screen  # type: ignore[attr-defined]
    assert app.renderer._display._owner is app.screen  # type: ignore[attr-defined]
    assert app.screen.display is app.display


async def test_the_authorization_prompt_is_not_reachable_as_a_tool(tmp_path: Path) -> None:
    """D36: the broker must never be asked to approve its own question.

    So the prompt is not in the registry, has no spec, and no registered tool
    holds either the prompter or its privileged screen handle.
    """
    app = NomadApp(_config(tmp_path))
    registered = [app.tools.get(name) for name in app.tools.names(enabled_only=False)]
    registered += list(app.hardware_tools)

    assert app.authprompt not in registered
    assert app.prompter not in registered
    assert not hasattr(app.authprompt, "spec")
    assert AUTH_PROMPT_WRITER not in app.tools.names(enabled_only=False)
    for tool in registered:
        held = list(vars(tool).values())
        assert app.authprompt not in held
        assert app.prompter not in held
        # A tool may hold a `ScreenView` — never the prompt's one.
        for value in held:
            assert getattr(value, "writer", None) != AUTH_PROMPT_WRITER


async def test_the_prompt_component_starts_with_the_device(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.states()["auth_prompt"] is ComponentState.STARTED
    finally:
        await app.stop()
    assert app.states()["auth_prompt"] is ComponentState.STOPPED


async def test_with_no_input_hardware_the_prompter_is_the_honest_one(tmp_path: Path) -> None:
    """A headless screen means nothing feeds the joystick (D32)."""
    app = NomadApp(_config(tmp_path))
    assert isinstance(app.prompter, NullChoicePrompter)


async def test_exactly_one_consumer_owns_the_input_stream(tmp_path: Path) -> None:
    """`InputStream.events()` is single-consumer: two readers steal presses."""
    app = NomadApp(_config(tmp_path))
    holders = [
        name
        for name, value in vars(app).items()
        if getattr(value, "_stream", None) is app.input
    ]
    assert holders in ([], ["prompter"])


async def test_the_screen_is_served_over_loopback(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.view is not None
        await app.display.show_text("battery 41%", title="Nomad")  # type: ignore[attr-defined]
        body = urllib.request.urlopen(app.view_url, timeout=5).read().decode()
        assert "battery 41%" in body
        assert 'http-equiv="refresh"' in body
        assert app.view_url is not None
        assert app.view_url.startswith("http://127.0.0.1:")
    finally:
        await app.stop()

    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(app.view_url, timeout=5)


async def test_the_view_is_on_for_either_headless_driver_name(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path, display={"driver": "headless"}))
    assert app.view is not None


async def test_a_display_with_its_own_glass_is_not_wired_yet(tmp_path: Path) -> None:
    """`esp32` needs a `Link` the composition root does not build yet.

    Recorded as a failure rather than a silent fallback to the headless
    screen: a device configured for its own glass and quietly given a browser
    page instead is worse than one that says it cannot boot.
    """
    with pytest.raises(HardwareError):
        NomadApp(_config(tmp_path, display={"driver": "esp32"}))


async def test_the_view_can_be_switched_off(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path, view={"enabled": False}))
    assert app.view is None


async def test_a_non_loopback_view_host_is_refused(tmp_path: Path) -> None:
    """The API's deferred auth problem must not arrive through this door."""
    app = NomadApp(_config(tmp_path, view={"enabled": True, "port": 0, "host": "0.0.0.0"}))
    with pytest.raises(LifecycleError) as excinfo:
        await app.start()
    assert isinstance(excinfo.value.__cause__, ConfigError)
    await app.stop()


async def test_a_turn_reaches_the_screen_through_the_real_app(tmp_path: Path) -> None:
    """End to end, with nothing stubbed: send a message, read the glass."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        outcome = await app.session.send("hello")
        assert outcome.text == "mock: hello"
        await _settle(app)
        assert app.display.screen.text == "mock: hello"  # type: ignore[attr-defined]
    finally:
        await app.stop()


async def _settle(app: NomadApp) -> None:
    """Let the bus's subscriber task drain. The bus is fire-and-forget (D6)."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        if app.renderer.active_turn_id is None and app.display.screen.text:  # type: ignore[attr-defined]
            return
