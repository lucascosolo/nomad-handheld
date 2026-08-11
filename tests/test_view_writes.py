"""The browser page can start a turn and answer a prompt (chunk F3).

Before this, `AgentSession.send()` had one caller in `src/` — crash recovery —
and a headless build answered every authorization prompt `NO_OPERATOR`. So
these tests pin two things that had no way of being true, and one that must
not become false: an unauthenticated write endpoint on loopback is only safe
while it refuses requests that another *page* made on the operator's behalf.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from nomad.app import OFFLINE_TITLE, NomadApp
from nomad.core.config import NomadConfig
from nomad.core.errors import ConfigError
from nomad.core.events import Event
from nomad.input.choice import ChoiceOutcome, ExternalChoicePrompter
from nomad.tools.permissions import EVENT_AUTH_PENDING
from nomad.view.server import DISMISS_INDEX, ScreenServer


def _config(tmp_path: Path, **overrides: object) -> NomadConfig:
    data: dict[str, object] = {
        "storage": {"path": str(tmp_path / "nomad.db")},
        "workspace": {"root": str(tmp_path / "workspace")},
        # Loopback in tests: the shipped default is remote, and a suite that
        # bound every ephemeral view to every interface would be antisocial.
        "view": {"enabled": True, "port": 0, "remote": False},
        # So a generated view token lands in the tmp dir, never in the repo.
        "core": {"data_dir": str(tmp_path / "var")},
    }
    data.update(overrides)
    return NomadConfig.model_validate(data)


def _post(
    url: str,
    payload: dict[str, Any],
    *,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": content_type, **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())


def _json(raw: bytes) -> dict[str, Any]:
    """`send_error` answers in HTML, so a 404 body is not JSON."""
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


# -- the prompter ---------------------------------------------------------


async def test_an_unanswered_browser_prompt_times_out_rather_than_claiming_no_operator() -> None:
    """D32's distinction, and it is not cosmetic.

    `NO_OPERATOR` means retrying can never help. With a browser attached it
    can, so reporting it would tell the model to stop asking on the one device
    configuration where asking works.
    """
    prompter = ExternalChoicePrompter()
    result = await prompter.ask("go?", ["Deny", "Allow"], timeout_s=0.01)
    assert result.outcome is ChoiceOutcome.TIMED_OUT
    assert prompter.pending is None


async def test_an_answer_names_the_question_it_was_given() -> None:
    prompter = ExternalChoicePrompter()
    task = asyncio.ensure_future(prompter.ask("go?", ["Deny", "Allow"], timeout_s=5))
    await asyncio.sleep(0)
    pending = prompter.pending
    assert pending is not None and pending.options == ("Deny", "Allow")

    assert await prompter.answer("not-the-token", 1) is False
    assert await prompter.answer(pending.token, 1) is True
    result = await task
    assert result.outcome is ChoiceOutcome.ANSWERED
    assert result.option == "Allow"


async def test_a_stale_page_cannot_answer_the_prompt_that_replaced_the_one_it_shows() -> None:
    """The property the token exists for: approving what you read, not what
    arrived while you were reading."""
    prompter = ExternalChoicePrompter()
    first = asyncio.ensure_future(prompter.ask("first?", ["Deny", "Allow"], timeout_s=0.01))
    await asyncio.sleep(0)
    stale = prompter.pending
    assert stale is not None
    assert (await first).outcome is ChoiceOutcome.TIMED_OUT

    second = asyncio.ensure_future(prompter.ask("second?", ["Deny", "Allow"], timeout_s=5))
    await asyncio.sleep(0)
    assert prompter.pending is not None and prompter.pending.token != stale.token

    assert await prompter.answer(stale.token, 1) is False
    assert await prompter.answer(prompter.pending.token, 0) is True
    assert (await second).option == "Deny"


async def test_a_non_ascii_token_is_refused_and_not_a_crash() -> None:
    """It arrives from an HTTP body; `compare_digest` raises on non-ASCII."""
    prompter = ExternalChoicePrompter()
    task = asyncio.ensure_future(prompter.ask("go?", ["Deny"], timeout_s=5))
    await asyncio.sleep(0)
    assert await prompter.answer("tökén", 0) is False
    pending = prompter.pending
    assert pending is not None
    assert await prompter.cancel(pending.token) is True
    assert (await task).outcome is ChoiceOutcome.CANCELLED


async def test_an_out_of_range_option_resolves_nothing() -> None:
    prompter = ExternalChoicePrompter()
    task = asyncio.ensure_future(prompter.ask("go?", ["Deny", "Allow"], timeout_s=5))
    await asyncio.sleep(0)
    pending = prompter.pending
    assert pending is not None
    assert await prompter.answer(pending.token, 7) is False
    assert await prompter.answer(pending.token, -1) is False
    assert await prompter.answer(pending.token, 0) is True
    assert (await task).option == "Deny"


async def test_a_second_concurrent_question_is_refused_not_substituted() -> None:
    """One authorization UI (D36). Replacing a live prompt would swap the
    question under an operator who is looking at it."""
    prompter = ExternalChoicePrompter()
    first = asyncio.ensure_future(prompter.ask("first?", ["Deny"], timeout_s=5))
    await asyncio.sleep(0)
    second = await prompter.ask("second?", ["Deny"], timeout_s=5)
    assert second.outcome is ChoiceOutcome.NO_OPERATOR
    pending = prompter.pending
    assert pending is not None and pending.question == "first?"
    await prompter.cancel(pending.token)
    await first


# -- the write surface, in isolation --------------------------------------


class _Rig:
    """A `ScreenServer` with fake callables, so these tests are about HTTP."""

    def __init__(self, **kwargs: Any) -> None:
        self.submitted: list[str] = []
        self.answered: list[tuple[str, int]] = []
        self.release = asyncio.Event()
        self.pending: Any = None
        self.server = ScreenServer(
            lambda: "<pre>screen</pre>",
            port=0,
            refresh_seconds=0.05,
            submit_text=self._submit,
            pending_choice=lambda: self.pending,
            answer_choice=self._answer,
            **kwargs,
        )

    async def _submit(self, text: str) -> None:
        self.submitted.append(text)
        await self.release.wait()

    async def _answer(self, token: str, index: int) -> bool:
        self.answered.append((token, index))
        return token == "good"


@pytest.fixture
async def rig() -> Any:
    rig = _Rig()
    await rig.server.start()
    try:
        yield rig
    finally:
        rig.release.set()
        await rig.server.stop()


async def test_submitted_text_reaches_the_callable(rig: _Rig) -> None:
    status, body = _post(rig.server.url + "submit", {"text": "  hello nomad  "})
    assert status == 202 and body == {"submitted": True}
    await asyncio.sleep(0.05)
    assert rig.submitted == ["hello nomad"]


async def test_submitting_does_not_block_the_http_thread_on_the_turn(rig: _Rig) -> None:
    """The whole reason this is dispatched and not awaited: `_submit` above
    never returns until `release` is set, and the request must not wait."""
    status, _ = _post(rig.server.url + "submit", {"text": "slow"})
    assert status == 202
    await asyncio.sleep(0.05)
    assert rig.submitted == ["slow"]  # running, and the response already came back


async def test_a_second_turn_is_refused_while_one_is_in_flight(rig: _Rig) -> None:
    """`AgentSession` serializes turns behind a lock, so without this an
    unauthenticated endpoint could queue coroutines without limit."""
    assert _post(rig.server.url + "submit", {"text": "one"})[0] == 202
    await asyncio.sleep(0.05)
    status, body = _post(rig.server.url + "submit", {"text": "two"})
    assert status == 409 and "already running" in body["error"]

    rig.release.set()
    await asyncio.sleep(0.05)
    assert _post(rig.server.url + "submit", {"text": "three"})[0] == 202


async def test_empty_text_is_rejected(rig: _Rig) -> None:
    assert _post(rig.server.url + "submit", {"text": "   "})[0] == 400
    assert _post(rig.server.url + "submit", {"text": 7})[0] == 400
    assert _post(rig.server.url + "submit", {})[0] == 400
    assert rig.submitted == []


async def test_an_answer_reaches_the_prompter(rig: _Rig) -> None:
    status, body = _post(rig.server.url + "answer", {"token": "good", "index": 1})
    assert status == 202 and body == {"submitted": True}
    await asyncio.sleep(0.05)
    assert rig.answered == [("good", 1)]


async def test_a_boolean_is_not_an_option_index(rig: _Rig) -> None:
    """`isinstance(True, int)` is True, and `choices[True]` is `choices[1]`.

    An adversarial review posted `{"index": true}` at a live authorization
    prompt and it selected the second option — "Approve once" — returning
    `allow=True`. The token was still required, so it was never a bypass, but
    it approved something on an answer the operator did not give, which is the
    one thing this endpoint must never do.
    """
    for forged in (True, False):
        status, _ = _post(rig.server.url + "answer", {"token": "good", "index": forged})
        assert status == 400, f"a bool was accepted as an option index: {forged!r}"
    await asyncio.sleep(0.05)
    assert rig.answered == [], "a boolean resolved a prompt"


async def test_dismiss_is_an_index_the_page_can_send(rig: _Rig) -> None:
    _post(rig.server.url + "answer", {"token": "good", "index": DISMISS_INDEX})
    await asyncio.sleep(0.05)
    assert rig.answered == [("good", DISMISS_INDEX)]


async def test_the_pending_prompt_is_published_to_the_page(rig: _Rig) -> None:
    assert _get(rig.server.url + "state")["prompt"] is None

    class _Pending:
        token, question, options = "tok", "Allow this action?", ("Deny", "Approve once")

    rig.pending = _Pending()
    state = _get(rig.server.url + "state")
    assert state["prompt"] == {
        "token": "tok",
        "question": "Allow this action?",
        "options": ["Deny", "Approve once"],
    }
    assert state["screen"] == "<pre>screen</pre>"


# -- the write surface is not an origin boundary --------------------------


async def test_a_cross_origin_page_cannot_start_a_turn(rig: _Rig) -> None:
    """Loopback is not an origin boundary. Any page in any browser on this
    machine can POST here, and a form post needs no preflight — so an endpoint
    that accepted one would let `evil.example` run tools on the device."""
    status, body = _post(
        rig.server.url + "submit", {"text": "rm -rf"}, headers={"Origin": "http://evil.example"}
    )
    assert status == 403 and "cross-origin" in body["error"]
    assert rig.submitted == []


async def test_another_service_on_loopback_is_just_as_foreign(rig: _Rig) -> None:
    """'It came from 127.0.0.1' is not a statement about who sent it."""
    other = f"http://127.0.0.1:{rig.server.port + 1}"
    status, _ = _post(rig.server.url + "submit", {"text": "x"}, headers={"Origin": other})
    assert status == 403
    assert rig.submitted == []


async def test_our_own_origin_is_allowed(rig: _Rig) -> None:
    origin = rig.server.url.rstrip("/")
    assert _post(rig.server.url + "submit", {"text": "x"}, headers={"Origin": origin})[0] == 202


async def test_a_form_post_is_refused_because_it_needs_no_preflight(rig: _Rig) -> None:
    """The second half of the defence: requiring JSON is what makes a browser
    ask permission before sending at all."""
    for content_type in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "text/plain",
        "",
    ):
        status, _ = _post(rig.server.url + "submit", {"text": "x"}, content_type=content_type)
        assert status == 403, content_type
    assert rig.submitted == []


async def test_a_cross_site_fetch_metadata_header_is_refused(rig: _Rig) -> None:
    status, _ = _post(
        rig.server.url + "submit", {"text": "x"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert status == 403
    assert rig.submitted == []


async def test_an_oversized_body_is_refused_before_it_is_read(rig: _Rig) -> None:
    status, _ = _post(rig.server.url + "submit", {"text": "x" * 20_000})
    assert status == 413
    assert rig.submitted == []


async def test_a_malformed_body_is_a_400_not_a_500(rig: _Rig) -> None:
    request = urllib.request.Request(
        rig.server.url + "submit",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 400


async def test_a_read_only_view_mounts_no_write_routes() -> None:
    """A view with no callables is the chunk F1 window, unchanged."""
    server = ScreenServer(lambda: "<pre>x</pre>", port=0)
    await server.start()
    try:
        assert server.writable is False
        assert _post(server.url + "submit", {"text": "x"})[0] == 404
        assert _post(server.url + "answer", {"token": "t", "index": 0})[0] == 404
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(server.url + "state", timeout=5)
        assert caught.value.code == 404
        page = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert 'http-equiv="refresh"' in page
    finally:
        await server.stop()


async def test_a_writable_view_still_refuses_a_non_loopback_host() -> None:
    """The refusal `start()` already made, now load-bearing for writes too."""
    server = ScreenServer(
        lambda: "", host="0.0.0.0", port=0, submit_text=lambda text: asyncio.sleep(0)
    )
    with pytest.raises(ConfigError):
        await server.start()


async def test_nothing_is_dispatched_once_the_view_has_stopped() -> None:
    """The loop captured in `start()` is released in `stop()`; a request that
    raced the shutdown must not reach a closed loop."""
    rig = _Rig()
    await rig.server.start()
    url = rig.server.url
    await rig.server.stop()
    with pytest.raises(urllib.error.URLError):
        _post(url + "submit", {"text": "x"})
    assert rig.submitted == []


# -- the whole device -----------------------------------------------------


async def test_a_stranger_can_start_a_turn_from_the_page(tmp_path: Path) -> None:
    """The finding this chunk exists for: `AgentSession.send()` had exactly one
    caller in `src/`, inside crash recovery, so nobody could begin a
    conversation by any means."""
    app = NomadApp(_config(tmp_path, offline={"enabled": False}))
    await app.start()
    try:
        assert app.view is not None and app.view.writable
        status, _ = _post(app.view_url + "submit", {"text": "hello"})
        assert status == 202
        for _ in range(100):
            await asyncio.sleep(0.02)
            if app.session._backend.sent:  # type: ignore[attr-defined]
                break
        assert app.session._backend.sent == ["hello"]  # type: ignore[attr-defined]
        messages = await app.conversations.get_messages_for_session(app.session.session_id)
        assert [m.content["text"] for m in messages if m.role == "user"] == ["hello"]
    finally:
        await app.stop()


async def test_an_authorization_prompt_can_be_answered_from_the_page(tmp_path: Path) -> None:
    """Every gated tool call was denied permanently on the default config,
    because nothing could ever answer. This is that path, end to end."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        await app.bus.publish(
            Event(
                type=EVENT_AUTH_PENDING,
                source="authorization_queue",
                payload={
                    "pending_id": "p1",
                    "tool": "write_file",
                    "target": "local",
                    "scope": "workspace",
                    "risk": "moderate",
                },
            )
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if _get(app.view_url + "state")["prompt"] is not None:
                break
        prompt = _get(app.view_url + "state")["prompt"]
        assert prompt is not None
        assert prompt["options"] == ["Deny", "Approve once", "Approve for this session"]
        assert "write_file" in prompt["question"]

        status, body = _post(app.view_url + "answer", {"token": prompt["token"], "index": 1})
        assert status == 202 and body == {"submitted": True}
        for _ in range(100):
            await asyncio.sleep(0.02)
            if _get(app.view_url + "state")["prompt"] is None:
                break
        assert _get(app.view_url + "state")["prompt"] is None
        assert app.authprompt.prompts_shown == 1
    finally:
        await app.stop()


async def test_the_onboard_tier_answers_without_a_turn_and_says_so(tmp_path: Path) -> None:
    """Chunk O's fast path, wired. A matched phrase never reaches the model —
    and it lands on the screen under its own title, because a device that
    answers some questions itself with no way to tell which is one you stop
    trusting."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        outcome = await app.submit("what time is it")
        assert getattr(outcome, "handled", False) is True
        assert app.session._backend.sent == []  # type: ignore[attr-defined]
        assert OFFLINE_TITLE in app.display.screen.text  # type: ignore[attr-defined]
    finally:
        await app.stop()


async def test_an_unrecognised_phrase_falls_through_to_the_model(tmp_path: Path) -> None:
    """The router fails *toward* Claude. Matching is equality, so anything it
    does not recognise exactly costs a round trip rather than a wrong answer."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.offline is not None
        await app.submit("write me a haiku about a raspberry pi")
        assert app.session._backend.sent == [  # type: ignore[attr-defined]
            "write me a haiku about a raspberry pi"
        ]
    finally:
        await app.stop()


# -- the mode selector (D14) ----------------------------------------------
#
# The control that decides whether the device asks before it acts. Everything
# else on this page changes what Nomad does next; this changes what he is
# allowed to do without checking, so it gets its own attack surface.


class _ModeRig:
    """A `ScreenServer` wired to a mode reader and writer, and nothing else."""

    def __init__(self) -> None:
        self.mode = "manual"
        self.switches: list[str] = []
        self.server = ScreenServer(
            lambda: "<pre>screen</pre>",
            port=0,
            refresh_seconds=0.05,
            mode_source=lambda: self.mode,
            set_mode=self._set_mode,
        )

    async def _set_mode(self, mode: str) -> None:
        self.switches.append(mode)
        self.mode = mode


@pytest.fixture
async def modes() -> Any:
    rig = _ModeRig()
    await rig.server.start()
    try:
        yield rig
    finally:
        await rig.server.stop()


async def test_the_page_says_which_mode_the_device_is_in(modes: _ModeRig) -> None:
    """A selector that could change the mode but not show it would leave the
    operator guessing which one they are in."""
    state = _get(modes.server.url + "state")

    assert state["mode"] == "manual"
    assert [entry["name"] for entry in state["modes"]] == ["manual", "smart", "auto"]
    # Labelled by consequence, not by name: "smart" tells an operator nothing
    # about what happens the next time Nomad wants to run something.
    assert state["modes"][0]["label"] == "Ask me every time"


@pytest.mark.parametrize("mode", ["manual", "smart", "auto"])
async def test_each_mode_can_be_selected(modes: _ModeRig, mode: str) -> None:
    status, body = _post(modes.server.url + "mode", {"mode": mode})

    assert status == 202 and body == {"submitted": True}
    await asyncio.sleep(0.05)
    assert modes.switches == [mode]
    assert _get(modes.server.url + "state")["mode"] == mode


@pytest.mark.parametrize("mode", ["", "AUTO", "yolo", "session", "../auto", 7, None, True])
async def test_an_unknown_mode_is_refused_and_never_coerced(modes: _ModeRig, mode: object) -> None:
    """Refused, not defaulted — and certainly not defaulted to the loosest
    one. `session` is rejected too: it is a grant scope in D14, not a posture,
    and offering it beside three postures is how a safety control gets
    misread.
    """
    status, _body = _post(modes.server.url + "mode", {"mode": mode})

    assert status == 400
    assert modes.switches == []
    assert modes.mode == "manual"


async def test_switching_the_mode_needs_the_same_csrf_proof_as_any_write(
    modes: _ModeRig,
) -> None:
    """A cross-origin page must not be able to turn the asking off."""
    status, _body = _post(modes.server.url + "mode", {"mode": "auto"}, content_type="text/plain")
    assert status == 403

    status, _body = _post(
        modes.server.url + "mode",
        {"mode": "auto"},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert modes.switches == []


async def test_a_view_with_no_mode_writer_does_not_mount_the_route() -> None:
    """Half a selector is not a state this can be in.

    A reader with no writer leaves the view read-only, exactly as it was
    before the selector existed: the route is not mounted and the page has no
    buttons to press.
    """
    server = ScreenServer(lambda: "<pre>x</pre>", port=0, mode_source=lambda: "manual")
    await server.start()
    try:
        assert server.writable is False
        status, _body = _post(server.url + "mode", {"mode": "auto"})
        assert status == 404
    finally:
        await server.stop()


async def test_the_selector_reaches_the_real_session(tmp_path: Path) -> None:
    """End to end through the composition root: a click changes the mode the
    broker will actually be asked with, not a copy of it."""
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        assert app.view is not None
        assert app.session.mode is not None
        status, _body = _post(app.view.url + "mode", {"mode": "auto"})
        assert status == 202
        await asyncio.sleep(0.1)
        assert str(app.session.mode) == "auto"
        assert _get(app.view.url + "state")["mode"] == "auto"
    finally:
        await app.stop()


# -- the origin check must not refuse the address the operator actually types --


@pytest.fixture
async def remote_writable():
    """A view bound off loopback and reachable by address, as the device runs."""
    submitted: list[str] = []

    async def submit(text: str) -> None:
        submitted.append(text)

    server = ScreenServer(
        lambda: "<pre>x</pre>",
        host="0.0.0.0",
        port=0,
        token="s3cret-token",
        submit_text=submit,
    )
    await server.start()
    try:
        yield server, f"http://127.0.0.1:{server.port}/", submitted
    finally:
        await server.stop()


async def test_a_remote_view_accepts_writes_from_its_own_lan_address(remote_writable) -> None:
    """The regression that made every button on the page look broken.

    A remote view is reached by whatever the operator typed, and the fall-through
    that allows it used to sit in an `except ValueError` arm — which a dotted quad
    never enters. So `http://nomad.local:8081` could write and
    `http://100.94.143.39:8081` could not: the page loaded, the prompt rendered,
    and every click came back 403 while the device logged a refusal nobody was
    watching. The token and the `SameSite=Strict` cookie are what authorize this;
    the origin check is the third layer, and it must not be stricter than the
    address bar.
    """
    server, url, submitted = remote_writable
    for origin in (
        f"http://100.94.143.39:{server.port}",
        f"http://192.168.1.40:{server.port}",
        f"http://nomad.local:{server.port}",
    ):
        status, _body = _post(
            url + "submit",
            {"text": origin},
            headers={"Origin": origin, "Authorization": "Bearer s3cret-token"},
        )
        assert status == 202, origin
        # One turn at a time: let the previous submission drain, or the second
        # origin gets refused for being busy rather than for being foreign.
        await asyncio.sleep(0.05)
    assert len(submitted) == 3


async def test_a_remote_view_still_refuses_another_port_and_another_site(
    remote_writable,
) -> None:
    """Loosening the host must not loosen the rest of the check."""
    server, url, submitted = remote_writable
    for origin in (
        f"https://100.94.143.39:{server.port}",  # scheme
        f"http://100.94.143.39:{server.port + 1}",  # another service on the box
        "http://evil.example",
    ):
        status, _body = _post(
            url + "submit",
            {"text": "x"},
            headers={"Origin": origin, "Authorization": "Bearer s3cret-token"},
        )
        assert status == 403, origin
    assert submitted == []


async def test_a_loopback_view_does_not_trust_a_lan_origin() -> None:
    """`remote` is what widens the check, so a loopback-only view must not."""
    server = ScreenServer(lambda: "<pre>x</pre>", port=0, submit_text=_never)
    await server.start()
    try:
        status, _body = _post(
            server.url + "submit",
            {"text": "x"},
            headers={"Origin": f"http://192.168.1.40:{server.port}"},
        )
        assert status == 403
    finally:
        await server.stop()


async def _never(text: str) -> None:  # pragma: no cover - the write is refused first
    raise AssertionError(f"a refused write reached the session: {text!r}")
