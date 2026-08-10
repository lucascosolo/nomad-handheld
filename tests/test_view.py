"""The screen is never blank while a turn runs, and never stale when it ends.

`agent.event` had publishers and no subscribers before chunk F1. These tests
pin the two properties that fixing that has to deliver, which pull against
each other: a *fast* mid-turn screen (lossy, best effort) and a *correct*
end-of-turn screen (authoritative, immune to the bus dropping frames — D6).
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

import pytest

from nomad.agent.backends.base import AgentEvent, AgentEventKind
from nomad.agent.session import (
    EVENT_AGENT_EVENT,
    EVENT_TURN_FINISHED,
    EVENT_TURN_STARTED,
    TurnOutcome,
    TurnOutcomeStatus,
)
from nomad.core.errors import ConfigError
from nomad.core.events import Event, EventBus
from nomad.core.lifecycle import ComponentState
from nomad.hardware.headless_display import DEFAULT_HISTORY_LIMIT, HeadlessDisplay
from nomad.view.renderer import DEFAULT_MAX_DETAIL_CHARS, TurnRenderer, _tool_detail
from nomad.view.screen import ScreenOwner
from nomad.view.server import ScreenServer

SESSION = "s1"
TURN = "t1"


def _started(text: str = "hello") -> Event:
    return Event(
        type=EVENT_TURN_STARTED,
        source="agent_session",
        payload={"session_id": SESSION, "turn_id": TURN, "text": text},
    )


def _agent(kind: AgentEventKind, **fields: object) -> Event:
    event = AgentEvent(kind=kind, session_id=SESSION, turn_id=TURN, **fields)  # type: ignore[arg-type]
    return Event(
        type=EVENT_AGENT_EVENT, source="agent_session", payload=event.model_dump(mode="json")
    )


def _finished(outcome: TurnOutcome) -> Event:
    return Event(
        type=EVENT_TURN_FINISHED,
        source="agent_session",
        payload={"session_id": SESSION, **outcome.model_dump(mode="json")},
    )


@pytest.fixture
def screen() -> HeadlessDisplay:
    return HeadlessDisplay()


@pytest.fixture
def renderer(screen: HeadlessDisplay, event_bus: EventBus) -> TurnRenderer:
    return TurnRenderer(screen, bus=event_bus)


# -- never blank -------------------------------------------------------------


async def test_a_turn_shows_a_working_state_before_any_text_arrives(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """The whole defect, in one assertion."""
    assert screen.screen.text == ""
    await renderer.handle(_started())
    assert screen.screen.text != ""
    assert "Thinking" in screen.screen.text
    assert renderer.active_turn_id == TURN


async def test_a_tool_call_before_any_text_names_the_tool(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TOOL_CALL, tool_name="read_file"))
    assert "read_file" in screen.screen.text


async def test_streamed_text_reaches_the_display_as_it_arrives(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TEXT, text="Battery "))
    first = screen.screen.text
    await renderer.handle(_agent(AgentEventKind.TEXT, text="41%"))
    assert "Battery " in first
    assert "Battery 41%" in screen.screen.text
    # Three draws so far: the working state and one per chunk.
    assert len(screen.history) == 3


async def test_an_error_event_is_drawn_immediately(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.ERROR, error="backend went away"))
    assert "backend went away" in screen.screen.text


# -- never stale -------------------------------------------------------------


async def test_the_final_answer_is_correct_when_intermediate_events_are_dropped(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """`EventBus` drops slow subscribers by design (D6).

    So the renderer must never derive the final screen from the chunks it
    happened to receive. Here it sees the first chunk and the last, and the
    two in the middle are dropped — the finished screen is still whole.
    """
    await renderer.handle(_started())
    await renderer.handle(_agent(AgentEventKind.TEXT, text="Battery "))
    # "41%, " and "~3h " dropped by the bus.
    await renderer.handle(_agent(AgentEventKind.TEXT, text="left"))
    assert screen.screen.text != "Battery 41%, ~3h left"  # what it saw is wrong

    await renderer.handle(
        _finished(
            TurnOutcome(
                turn_id=TURN,
                status=TurnOutcomeStatus.COMPLETED,
                text="Battery 41%, ~3h left",
            )
        )
    )
    assert screen.screen.text == "Battery 41%, ~3h left"
    assert renderer.active_turn_id is None


async def test_a_turn_that_produced_nothing_still_clears_the_working_state(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(_finished(TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.COMPLETED)))
    assert "Thinking" not in screen.screen.text


async def test_a_failed_turn_shows_the_error_not_the_spinner(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    await renderer.handle(_started())
    await renderer.handle(
        _finished(TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.FAILED, error="timed out"))
    )
    assert "timed out" in screen.screen.text
    assert "Thinking" not in screen.screen.text


async def test_an_interrupted_turn_says_so(renderer: TurnRenderer, screen: HeadlessDisplay) -> None:
    await renderer.handle(_started())
    await renderer.handle(
        _finished(TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.INTERRUPTED, text="half an "))
    )
    assert "Interrupted" in screen.screen.text
    assert "half an " in screen.screen.text


async def test_a_long_answer_is_truncated_from_the_front(
    screen: HeadlessDisplay, event_bus: EventBus
) -> None:
    """NOMAD.md: lead with the answer. On a screen that cannot scroll, the
    first line is the only one guaranteed to be read."""
    renderer = TurnRenderer(screen, bus=event_bus, max_chars=20)
    await renderer.handle(_started())
    await renderer.handle(
        _finished(
            TurnOutcome(turn_id=TURN, status=TurnOutcomeStatus.COMPLETED, text="A" * 10 + "B" * 90)
        )
    )
    assert screen.screen.text.startswith("A" * 10)
    assert "B" * 90 not in screen.screen.text
    assert "+80 chars" in screen.screen.text


# -- as a bus subscriber -----------------------------------------------------


async def test_the_renderer_subscribes_and_unsubscribes_with_its_lifecycle(
    screen: HeadlessDisplay, event_bus: EventBus
) -> None:
    renderer = TurnRenderer(screen, bus=event_bus)
    await renderer.start()
    assert renderer.state is ComponentState.STARTED

    await event_bus.publish(_started())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if screen.screen.text:
            break
    assert "Thinking" in screen.screen.text

    await renderer.stop()
    assert renderer.state is ComponentState.STOPPED
    before = len(screen.history)
    await event_bus.publish(_started())
    await asyncio.sleep(0.05)
    assert len(screen.history) == before


async def test_other_agent_events_are_ignored_rather_than_drawn(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """One subscription covers `agent.*`, so it sees mode changes too."""
    await renderer.handle(
        Event(type="agent.mode_changed", source="agent_session", payload={"to": "auto"})
    )
    assert screen.history == []


# -- the display itself ------------------------------------------------------


async def test_screen_history_is_bounded(screen: HeadlessDisplay) -> None:
    """A streaming renderer redraws several times a second, for days."""
    for index in range(DEFAULT_HISTORY_LIMIT + 25):
        await screen.show_text(str(index))
    assert len(screen.history) == DEFAULT_HISTORY_LIMIT
    assert screen.history[-1].text == str(DEFAULT_HISTORY_LIMIT + 24)


# -- serving it --------------------------------------------------------------


async def test_the_server_shows_the_current_screen(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0, refresh_seconds=2.0)
    await server.start()
    try:
        await screen.show_text("<b>not html</b>")
        body = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert "&lt;b&gt;not html&lt;/b&gt;" in body
        assert 'content="2.0"' in body

        fragment = urllib.request.urlopen(server.url + "screen", timeout=5).read().decode()
        assert fragment == screen.screen.html
    finally:
        await server.stop()


async def test_the_server_404s_an_unknown_path(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0)
    await server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(server.url + "secrets", timeout=5)
        assert excinfo.value.code == 404
    finally:
        await server.stop()


async def test_the_server_refuses_a_non_loopback_bind_without_a_token(
    screen: HeadlessDisplay,
) -> None:
    """Remote is allowed; remote *and unauthenticated* is not, and the refusal
    is here rather than in whoever edits the config."""
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        server = ScreenServer(lambda: screen.screen.html, host=host, port=0)
        with pytest.raises(ConfigError):
            await server.start()
        assert server.state is ComponentState.FAILED


async def test_an_empty_token_does_not_count_as_one(screen: HeadlessDisplay) -> None:
    """`token=""` would authenticate every request that sent no token at all,
    which is worse than no token: it looks authenticated."""
    server = ScreenServer(lambda: screen.screen.html, host="0.0.0.0", port=0, token="")
    with pytest.raises(ConfigError):
        await server.start()


# -- remote, with the token --------------------------------------------------


def _get(url: str, **headers: str) -> tuple[int, str, dict[str, str]]:
    """A GET that reports a 401 instead of raising, since that is the subject."""
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)
    return response.status, response.read().decode(), dict(response.headers)


@pytest.fixture
async def remote(screen: HeadlessDisplay):
    """A view bound off loopback, as a device on a network actually runs it."""
    server = ScreenServer(
        lambda: screen.screen.html,
        host="0.0.0.0",
        port=0,
        token="s3cret-token",
        submit_text=None,
    )
    await server.start()
    try:
        yield server, f"http://127.0.0.1:{server.port}/"
    finally:
        await server.stop()


async def test_a_remote_view_starts_when_it_has_a_token(remote) -> None:
    server, _url = remote
    assert server.state is ComponentState.STARTED
    assert server.remote is True


async def test_the_screen_is_not_readable_without_the_token(remote) -> None:
    """The screen says what the device is doing and who it is doing it for.
    Reads are gated, not just writes."""
    server, url = remote
    status, _body, _headers = _get(url)

    assert status == 401
    assert server.unauthorized == 1


async def test_a_wrong_token_is_no_better_than_none(remote) -> None:
    server, url = remote
    status, _body, _headers = _get(url, Authorization="Bearer not-the-token")

    assert status == 401
    assert server.unauthorized == 1


async def test_a_bearer_token_gets_in(remote) -> None:
    server, url = remote
    status, body, _headers = _get(url, Authorization="Bearer s3cret-token")

    assert status == 200
    assert "nomad" in body
    assert server.unauthorized == 0


async def test_the_url_form_pairs_the_browser_and_then_gets_out_of_the_url(
    remote,
) -> None:
    """The `?token=` is the only way to hand a browser the secret, and the
    worst place to leave it — so the response that accepts it also moves it
    into a cookie the address bar never shows."""
    server, url = remote
    status, _body, headers = _get(url + "?token=s3cret-token")

    assert status == 200
    cookie = headers.get("Set-Cookie", "")
    assert "nomad_view=s3cret-token" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie

    # And the cookie alone is enough afterwards, with a clean URL.
    status, _body, _headers = _get(url, Cookie="nomad_view=s3cret-token")
    assert status == 200


async def test_a_write_still_needs_the_token(screen: HeadlessDisplay) -> None:
    submitted: list[str] = []

    async def submit(text: str) -> None:
        submitted.append(text)

    server = ScreenServer(
        lambda: screen.screen.html,
        host="0.0.0.0",
        port=0,
        token="s3cret-token",
        submit_text=submit,
    )
    await server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/submit"
        request = urllib.request.Request(
            url,
            data=b'{"text": "do something"}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 401
        assert submitted == []
    finally:
        await server.stop()


async def test_the_login_url_carries_the_token_and_the_plain_one_does_not(
    remote,
) -> None:
    """`url` is logged at boot and printed into structured records; only
    `login_url` is ever handed to a person."""
    server, _url = remote

    assert "s3cret-token" not in server.url
    assert server.login_url.endswith("?token=s3cret-token")


async def test_the_server_accepts_the_loopback_spellings(screen: HeadlessDisplay) -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        server = ScreenServer(lambda: screen.screen.html, host=host, port=0)
        await server.start()
        try:
            assert server.state is ComponentState.STARTED
        finally:
            await server.stop()


async def test_stopping_the_server_twice_is_inert(screen: HeadlessDisplay) -> None:
    server = ScreenServer(lambda: screen.screen.html, port=0)
    await server.stop()
    await server.start()
    await server.stop()
    await server.stop()
    assert server.state is ComponentState.STOPPED


# -- one screen, one writer at a time (D36) ----------------------------------


async def test_writers_share_the_screen_while_nobody_holds_it(
    screen: HeadlessDisplay,
) -> None:
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    await owner.view("renderer").show_text("a turn")
    assert screen.screen.text == "a turn"
    await owner.view("model").show_card("card", "body", [("k", "v")])
    assert "body" in screen.screen.text
    assert owner.suppressed == 0
    assert owner.holder is None


async def test_a_holder_locks_every_other_writer_out(screen: HeadlessDisplay) -> None:
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    other = owner.view("renderer")
    async with owner.exclusive("auth_prompt") as held:
        assert owner.holder == "auth_prompt"
        await held.show_text("the question")
        await other.show_text("a streamed chunk")
        await other.show_list("menu", [("a", None)])
        await other.show_choice("really?", ["yes"])
        assert screen.screen.text == "the question"
        assert owner.suppressed == 3
    assert owner.holder is None
    await other.show_text("after")
    assert screen.screen.text == "after"


async def test_a_frame_queued_behind_a_draw_loses_to_a_claim_taken_meanwhile(
    screen: HeadlessDisplay,
) -> None:
    """The re-check under the draw lock, which is the actual race D36 cares about."""
    owner = ScreenOwner(screen)  # type: ignore[arg-type]
    gate = asyncio.Event()

    class SlowDisplay:
        async def show_text(self, text: str, *, title: str | None = None) -> None:
            await gate.wait()
            await screen.show_text(text, title=title)

    owner._display = SlowDisplay()  # type: ignore[assignment]
    slow = asyncio.ensure_future(owner.view("renderer").show_text("first"))
    await asyncio.sleep(0)
    queued = asyncio.ensure_future(owner.view("renderer").show_text("second"))
    await asyncio.sleep(0)

    async with owner.exclusive("auth_prompt"):
        gate.set()
        await slow
        await queued
        assert screen.screen.text == "first"
        assert owner.suppressed == 1


# -- what a call is doing, on the glass -------------------------------------
#
# The device shipped drawing the title "Running Bash" over a bare ellipsis and
# holding it for minutes. It was not a crash and no log line said anything was
# wrong; the operator simply looked at the screen and could not tell whether
# Nomad was working or wedged. NOMAD.md promises the opposite, so these pin the
# body being present and true.


def test_a_bash_call_shows_the_command() -> None:
    assert _tool_detail("Bash", {"command": "pytest -q"}) == "pytest -q"


def test_a_file_tool_shows_the_path() -> None:
    assert _tool_detail("Write", {"file_path": "/home/nomad/x.py", "content": "..."}) == (
        "/home/nomad/x.py"
    )


def test_an_unknown_tool_falls_back_to_its_most_useful_argument() -> None:
    """A tool added to the backend tomorrow should not render as an ellipsis
    just because this table has not heard of it."""
    assert _tool_detail("SomeNewTool", {"unrelated": 1, "path": "/tmp/thing"}) == "/tmp/thing"


def test_a_multiline_command_is_collapsed_to_one_line() -> None:
    """A heredoc would otherwise spend the whole screen on its own newlines and
    push the first, identifying token off the top."""
    assert _tool_detail("Bash", {"command": "git commit -F - <<'X'\nsubject\n\nbody\nX"}) == (
        "git commit -F - <<'X' subject body X"
    )


def test_a_long_argument_is_truncated_rather_than_overflowing() -> None:
    detail = _tool_detail("Bash", {"command": "x" * 500})
    assert len(detail) <= DEFAULT_MAX_DETAIL_CHARS
    assert detail.endswith("…")


def test_a_payload_that_is_not_a_dict_costs_nothing() -> None:
    """`tool_input` comes off the bus as JSON. The renderer is the last place
    that may raise — a blank screen is worse than a missing detail line."""
    assert _tool_detail("Bash", None) == ""
    assert _tool_detail("Bash", ["command"]) == ""
    assert _tool_detail("Bash", {"command": 17}) == ""
    assert _tool_detail("Bash", {"command": "   "}) == ""


async def test_the_mid_turn_screen_carries_the_command(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """The reported symptom, pinned: the body is the command, not an ellipsis."""
    await renderer.handle(_started())
    await renderer.handle(
        _agent(
            AgentEventKind.TOOL_CALL,
            tool_name="Bash",
            tool_input={"command": "git worktree add ~/nomad-scratch/try"},
        )
    )
    assert "git worktree add ~/nomad-scratch/try" in screen.screen.text


async def test_a_new_turn_clears_the_previous_call_detail(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """Otherwise the next turn opens showing the last thing the *previous* one
    ran, which is the same class of bug as a stale screen."""
    await renderer.handle(_started())
    await renderer.handle(
        _agent(AgentEventKind.TOOL_CALL, tool_name="Bash", tool_input={"command": "pytest -q"})
    )
    await renderer.handle(_started())
    assert "pytest" not in screen.screen.text


# -- never stale across a restart --------------------------------------------


async def test_startup_claims_the_screen_so_a_dead_turn_cannot_stay_on_it(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    """The failure this fixes, reproduced as the panel actually experiences it.

    A panel holds the last pixels it was sent. So the previous process's final
    frame is simulated by writing it directly, then starting a *new* renderer
    against the same screen — which is exactly what a service restart does.
    """
    await screen.show_text("Now, let's set up a venv", title="Claude")
    assert "venv" in screen.screen.text

    await renderer.start()

    assert "venv" not in screen.screen.text
    assert "idle" in screen.screen.text


async def test_the_idle_screen_is_drawn_even_with_no_turn_history(
    renderer: TurnRenderer, screen: HeadlessDisplay
) -> None:
    assert screen.screen.text == ""
    await renderer.start()
    assert screen.screen.text != ""
    assert renderer.active_turn_id is None


async def test_a_display_that_fails_at_boot_does_not_stop_the_subscription(
    event_bus: EventBus,
) -> None:
    """A screen is never allowed to prevent the renderer from subscribing.

    If it did, one bad frame at boot would cost every frame afterwards — the
    opposite of the property this component exists for.
    """

    class FailsOnce:
        def __init__(self) -> None:
            self.calls = 0
            self.text = ""

        async def show_text(self, text: str, *, title: str | None = None) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("panel not answering yet")
            self.text = text

    display = FailsOnce()
    renderer = TurnRenderer(display, bus=event_bus)  # type: ignore[arg-type]

    await renderer.start()
    assert renderer.state is ComponentState.STARTED

    await renderer.handle(_started())
    assert display.text != ""
