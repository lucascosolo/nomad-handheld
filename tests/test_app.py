"""The composition root actually assembles a device (chunk F1).

These are the tests that were missing while 443 others passed on a machine
that had never been switched on: that the parts start together in the right
order, that a failed boot leaves nothing running, and that the screen is
reachable.
"""

from __future__ import annotations

import asyncio
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomad.app import VIEW_TOKEN_FILE, NomadApp
from nomad.core.config import NomadConfig
from nomad.core.errors import LifecycleError
from nomad.core.lifecycle import ComponentState
from nomad.hardware.headless_display import HeadlessDisplay
from nomad.input.choice import (
    ExternalChoicePrompter,
    InputChoicePrompter,
    NullChoicePrompter,
)
from nomad.input.wake import WAKE_LABEL
from nomad.protocol.messages import InputChoice
from nomad.storage.migrations import MIGRATIONS, current_version
from nomad.view.authprompt import AUTH_PROMPT_WRITER


def _config(tmp_path: Path, **overrides: object) -> NomadConfig:
    data: dict[str, object] = {
        "storage": {"path": str(tmp_path / "nomad.db")},
        "workspace": {"root": str(tmp_path / "workspace")},
        # Port 0 so concurrent runs never collide on a fixed port.
        # Loopback in tests: the shipped default is remote, and a suite that
        # bound every ephemeral view to every interface would be antisocial.
        "view": {"enabled": True, "port": 0, "remote": False},
        # So a generated view token lands in the tmp dir, never in the repo.
        "core": {"data_dir": str(tmp_path / "var")},
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


async def test_with_no_input_hardware_at_all_the_prompter_is_the_honest_one(
    tmp_path: Path,
) -> None:
    """A headless screen and no browser means nothing can answer (D32)."""
    app = NomadApp(_config(tmp_path, view={"enabled": False}))
    assert isinstance(app.prompter, NullChoicePrompter)


async def test_a_browser_view_is_an_input_device(tmp_path: Path) -> None:
    """Chunk F3. `NullChoicePrompter` on a headless build was not a stub, it
    was a permanent denial: `NO_OPERATOR` to every prompt, and `manual` mode
    ships in `nomad.toml`, so every gated tool call was denied by construction.
    """
    app = NomadApp(_config(tmp_path))
    assert isinstance(app.prompter, ExternalChoicePrompter)
    assert app.view is not None and app.view.writable


@pytest.mark.parametrize(
    "display",
    [{"driver": "headless"}, {"driver": "esp32", "mirror": ["headless"]}],
    ids=["headless", "panel"],
)
async def test_exactly_one_consumer_owns_the_input_stream(
    tmp_path: Path, display: dict[str, object]
) -> None:
    """`InputStream.events()` is single-consumer: two readers steal presses.

    Since D44 the consumer is the broker on every build, prompter or not —
    which is stricter than what this test pinned before, because the reader no
    longer comes and goes with whichever prompter the display driver selected.
    """
    app = NomadApp(_config(tmp_path, display=display))
    holders = [
        name
        for name, value in vars(app).items()
        if getattr(value, "_stream", None) is app.input
        # The router *feeds* the stream and never iterates it. Holding it is
        # the opposite end of the pipe from consuming it.
        and name != "input_router"
    ]
    assert holders == ["input_broker"]


async def test_the_screen_is_served_over_loopback(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.view is not None
        await app.display.show_text("battery 41%", title="Nomad")  # type: ignore[attr-defined]
        body = urllib.request.urlopen(app.view_url, timeout=5).read().decode()
        assert "battery 41%" in body
        # No meta refresh on the interactive page: a whole-page reload every
        # second would delete whatever the operator was half-way through
        # typing. It polls `/state` instead.
        assert 'http-equiv="refresh"' not in body
        assert "/state" in body
        assert app.view_url is not None
        assert app.view_url.startswith("http://127.0.0.1:")
    finally:
        await app.stop()

    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(app.view_url, timeout=5)


async def test_the_view_is_on_for_either_headless_driver_name(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path, display={"driver": "headless"}))
    assert app.view is not None


async def test_a_display_with_its_own_glass_gets_a_link(tmp_path: Path) -> None:
    """`esp32` now builds, because the composition root builds the `Link`.

    This test previously asserted the opposite — that the app refused to
    construct — and it was right to, because a device configured for its own
    glass and quietly handed a browser page instead is worse than one that says
    it cannot boot. The fallback is still refused; what changed is that the
    link exists, so there is nothing to fall back *from*.
    """
    app = NomadApp(_config(tmp_path, display={"driver": "esp32"}))
    assert app.esp32_link is not None
    # Default transport kind is `mock`, so this needs no serial port (D9).
    assert app.display is not None
    # Its own glass means no browser copy of the screen.
    assert app.view is None


async def test_no_link_is_built_when_no_surface_needs_one(tmp_path: Path) -> None:
    """A `Link` owns a reader task. Building one nothing talks to would burn a
    wakeup forever on a laptop build."""
    app = NomadApp(_config(tmp_path))
    assert app.esp32_link is None


async def test_the_esp32_surface_can_be_mirrored_to_a_browser(tmp_path: Path) -> None:
    """The multi-monitor path: the panel is the face, and a monitor plugged
    into the Pi sees the same state over HTTP (D36 still arbitrates writers)."""
    app = NomadApp(
        _config(tmp_path, display={"driver": "esp32", "mirror": ["headless"]}),
    )
    assert app.esp32_link is not None
    # A mirror that includes a headless surface is what turns the view back on,
    # even though the *primary* surface has glass of its own.
    assert app.view is not None
    assert app.display.describe() == "esp32, headless"


async def test_mirroring_writes_to_every_surface(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path, display={"driver": "esp32", "mirror": ["headless"]}))
    await app.display.show_text("status", title="Nomad")
    # The headless surface holds the HTML the browser view serves, so proving
    # it received the write proves the fanout reached past the primary.
    headless = app.display.surface("headless")
    assert "status" in str(headless.screen.html)


async def test_a_panel_gets_a_keeper_and_it_is_a_started_component(tmp_path: Path) -> None:
    """The keeper was written, tested and — like six subsystems before it —
    would have been inert until the composition root built one. This asserts
    the join, not the behaviour."""
    app = NomadApp(_config(tmp_path, display={"driver": "esp32", "mirror": ["headless"]}))
    assert app.panel_keeper is not None
    await app.start()
    try:
        assert app.states()["panel_keeper"] is ComponentState.STARTED
    finally:
        await app.stop()
    assert app.states()["panel_keeper"] is ComponentState.STOPPED


async def test_the_self_improve_trigger_is_built_and_started_when_enabled(tmp_path: Path) -> None:
    """Chunk P's join. Six subsystems shipped green and inert because nothing
    constructed them; this asserts the composition root does, and that the
    trigger is a started component rather than a built one."""
    app = NomadApp(
        _config(tmp_path, triggers={"self_improve": {"enabled": True, "interval_seconds": 3600.0}})
    )
    assert app.self_improve is not None
    # After the session, because it drives it.
    order = [c.name for c in app._ordered_components()]
    assert order.index("self_improve_trigger") > order.index("agent_session")
    await app.start()
    try:
        assert app.states()["self_improve_trigger"] is ComponentState.STARTED
    finally:
        await app.stop()
    assert app.states()["self_improve_trigger"] is ComponentState.STOPPED


async def test_the_self_improve_trigger_is_off_by_default(tmp_path: Path) -> None:
    """A device that starts spending tokens unasked is an operator decision."""
    assert NomadConfig().triggers.self_improve.enabled is False
    app = NomadApp(_config(tmp_path))
    assert app.self_improve is None
    assert "self_improve_trigger" not in [c.name for c in app._ordered_components()]


async def test_a_device_with_no_panel_has_no_keeper(tmp_path: Path) -> None:
    """Nothing to repair on an in-process surface that cannot miss a write."""
    app = NomadApp(_config(tmp_path, display={"driver": "headless"}))
    assert app.panel_keeper is None


async def test_the_repaint_tick_can_be_switched_off(tmp_path: Path) -> None:
    app = NomadApp(
        _config(
            tmp_path,
            display={"driver": "esp32", "mirror": ["headless"], "repaint_interval_s": 0},
        )
    )
    assert app.panel_keeper is None


async def test_a_surface_cannot_be_listed_twice(tmp_path: Path) -> None:
    """Two surfaces of one kind double every write, and for `esp32` would mean
    two drivers on one link."""
    with pytest.raises(ValidationError):
        _config(tmp_path, display={"driver": "headless", "mirror": ["headless"]})


async def test_the_view_can_be_switched_off(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path, view={"enabled": False}))
    assert app.view is None


async def test_a_remote_view_mints_a_token_rather_than_going_open(tmp_path: Path) -> None:
    """The composition root is what makes `remote = true` safe to default on.

    `ScreenServer` refuses a non-loopback bind with no token; this is the
    other half — the app never *hands* it a non-loopback host without one, so
    the refusal is a backstop rather than something an operator trips over.
    """
    app = NomadApp(
        _config(
            tmp_path,
            view={"enabled": True, "port": 0, "host": "0.0.0.0"},
            core={"data_dir": str(tmp_path / "var")},
        )
    )
    await app.start()
    try:
        assert app.view is not None
        assert app.view.remote is True
        token_file = tmp_path / "var" / VIEW_TOKEN_FILE
        assert token_file.exists()
        # 0600 at creation, not a chmod afterwards: a secret that is briefly
        # world-readable was briefly readable by the world.
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert app.view_login_url is not None
        assert token_file.read_text().strip() in app.view_login_url
        # And never in the URL that gets logged.
        assert app.view_url is not None
        assert token_file.read_text().strip() not in app.view_url
    finally:
        await app.stop()


async def test_the_view_token_survives_a_restart(tmp_path: Path) -> None:
    """A token regenerated on every boot is a browser that has to be re-paired
    every boot, which is how an operator ends up disabling the whole thing."""
    config = _config(
        tmp_path,
        view={"enabled": True, "port": 0, "host": "0.0.0.0"},
        core={"data_dir": str(tmp_path / "var")},
    )
    token_file = tmp_path / "var" / VIEW_TOKEN_FILE

    first = NomadApp(config)
    await first.start()
    first_token = token_file.read_text()
    await first.stop()

    second = NomadApp(config)
    await second.start()
    try:
        # The token, not the URL: the port is 0 here, so the URL legitimately
        # differs between boots.
        assert token_file.read_text() == first_token
        assert first_token.strip() in (second.view_login_url or "")
    finally:
        await second.stop()


async def test_a_loopback_view_stays_open_without_ceremony(tmp_path: Path) -> None:
    """Remote is the interesting case; `remote = false` must not have quietly
    grown a token the operator now has to find."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.view is not None
        assert app.view.remote is False
        assert app.view_login_url == app.view_url
        assert not (tmp_path / "var" / VIEW_TOKEN_FILE).exists()
    finally:
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


# -- the wake button (D44) --------------------------------------------------


def _panel_with_trigger(tmp_path: Path) -> NomadConfig:
    """A device shaped like the real one: its own glass, and a schedule."""
    return _config(
        tmp_path,
        display={"driver": "esp32", "mirror": ["headless"]},
        triggers={"self_improve": {"enabled": True, "interval_seconds": 21600.0}},
    )


async def test_the_wake_button_is_wired_end_to_end(tmp_path: Path) -> None:
    """The join that makes the button real: a label on the screen, an
    affordance behind it, and the broker handing it idle presses."""
    app = NomadApp(_panel_with_trigger(tmp_path))
    assert app.wake is not None
    assert app.wake.label == WAKE_LABEL
    await app.start()
    try:
        assert app.states()["input_broker"] is ComponentState.STARTED
        headless = app.display.surface("headless")
        # The boot frame the operator is looking at, with something to tap.
        assert WAKE_LABEL in str(headless.screen.text)

        # The frame the panel would send when a finger lands on that row.
        await app.input.feed_choice(InputChoice(index=0, option=WAKE_LABEL))
        for _ in range(200):
            if app.wake.woke or app.wake.ignored:
                break
            await asyncio.sleep(0.01)
        assert app.wake.woke == 1
    finally:
        await app.stop()


async def test_no_button_is_drawn_without_a_trigger_to_fire(tmp_path: Path) -> None:
    """A drawn control that cannot work is the inert-subsystem failure this
    file exists to stop."""
    app = NomadApp(_config(tmp_path, display={"driver": "esp32", "mirror": ["headless"]}))
    assert app.self_improve is None
    assert app.wake is None
    assert app._wake_label() is None


async def test_no_button_is_drawn_on_a_device_with_no_panel(tmp_path: Path) -> None:
    """A headless build answers over HTTP and never feeds `InputStream`, so
    nothing could report the tap."""
    app = NomadApp(
        _config(
            tmp_path,
            triggers={"self_improve": {"enabled": True, "interval_seconds": 21600.0}},
        )
    )
    assert app.self_improve is not None
    assert app.wake is None
    assert app._wake_label() is None


async def test_the_broker_starts_before_the_router_that_feeds_it(tmp_path: Path) -> None:
    app = NomadApp(_panel_with_trigger(tmp_path))
    order = [c.name for c in app._ordered_components()]
    assert order.index("input_broker") < order.index("input_router")
    assert order.index("input_broker") > order.index("input_stream")


async def test_the_browser_can_answer_a_prompt_on_a_device_with_a_panel(tmp_path: Path) -> None:
    """D47. The page used to be a window onto the glass and not a control: the
    answer callables were wired for the headless prompter only, so on the real
    device the operator could read an authorization prompt and answer it
    nowhere."""
    app = NomadApp(_config(tmp_path, display={"driver": "esp32", "mirror": ["headless"]}))
    assert isinstance(app.prompter, InputChoicePrompter)
    assert app.view is not None
    assert app.view.writable
    # The two callables the page's Approve/Deny buttons are mounted on.
    assert app.view._pending_choice is not None
    assert app.view._answer_choice is not None
