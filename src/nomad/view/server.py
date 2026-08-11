"""The browser view: watching the screen, and now driving it (D9, chunk F3).

`HeadlessDisplay`'s docstring promises "you can watch what the model just put
on the screen while building the rest of the stack". Until this module that was
false: the only way to see the screen was a Python REPL holding the object.

Deliberately `http.server` on a thread and not a web framework. This started as
one route returning one string; it is now three, which is still not a reason to
put a second HTTP stack in the process.

**This is a write surface, and off loopback it is an authenticated one.** Until
chunk F3 nothing in `src/` could start a turn or answer an authorization prompt
— with a headless display the device denied every gated tool call permanently,
by construction. The cheapest honest fix is to let the page that already shows
the screen also act as the input peripheral the ESP32 will eventually be. That
raises the stakes on a server reachable from anywhere, so:

* **A non-loopback bind requires a token, enforced, not documented.** A
  handheld the operator cannot reach from their laptop is a handheld they have
  to SSH into to answer a yes/no question, so remote access is a feature, not a
  leak. What must never happen is it arriving *by accident*: `start()` refuses
  a non-loopback host unless a token was supplied, and every request — read and
  write alike — must then carry it. The screen shows what the device is doing
  and the page can start turns; neither is public.
* **The token is compared with `hmac.compare_digest`** and never logged. It
  reaches the browser once, as a `?token=` on the first request, and is
  immediately moved into a `HttpOnly; SameSite=Strict` cookie so it stops
  appearing in the address bar, in history, and in any `Referer`.
* **Loopback is not an origin boundary.** Any page in any browser on this
  machine can POST a form to `127.0.0.1`, and cross-origin form posts need no
  preflight. An unauthenticated write endpoint that accepted them would let
  `evil.example` start a turn — with attacker-chosen text — on a device whose
  whole purpose is running tools. So every state-changing request must be JSON
  (which forces a preflight a cross-origin page cannot pass) *and* must carry
  no foreign `Origin`. Both checks, because either alone is one browser quirk
  away from being wrong.
* **Neither check is authentication**, and neither is claimed to be. They stop
  another *page* acting as the operator. Any process that can open a socket on
  this machine is still the operator, exactly as the known-gaps list says.

The endpoints hand work across a thread boundary: `http.server` runs on a
daemon thread and the session is asyncio. Everything that crosses goes through
`asyncio.run_coroutine_threadsafe` against the loop captured in `start()`, and
**nothing here ever waits on the result**. A request that blocked on the loop
would hang for as long as the loop was busy, and would deadlock outright if the
caller were itself on it; the page polls `/state` instead, which is the better
signal anyway — it reflects what the device did, not what this thread was told.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hmac
import http.cookies
import ipaddress
import json
import socket
import threading
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nomad.core.errors import ConfigError
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: A callable returning the current screen as an HTML fragment. A callable and
#: not a driver: this module has no business knowing which display it is
#: looking at, and the read has to be live because the screen changes under it.
ScreenSource = Callable[[], str]

#: Start a turn from the operator's text. A coroutine function, and `view` is
#: not allowed to know it lands on `AgentSession.send` — the composition root
#: supplies it, the same way it supplies `ScreenSource`.
TextSubmit = Callable[[str], Awaitable[Any]]

#: The unanswered authorization question, or `None`. Read from the HTTP thread,
#: so whatever supplies it must return an immutable snapshot.
PendingSource = Callable[[], Any]

#: `(token, index)` where `index` is `-1` for "dismiss". Returns whether it
#: resolved anything; a stale page resolves nothing.
AnswerChoice = Callable[[str, int], Awaitable[bool]]

#: The permission mode the running session is in, read live from the HTTP
#: thread. A plain string, because `view` may not import `core.config` for an
#: enum it only ever displays.
ModeSource = Callable[[], str]

#: Switch the running session's permission mode (D14). A coroutine, because
#: tightening to `manual` also revokes standing session grants — which is a
#: database write, and the one thing that makes a tightening real.
SetMode = Callable[[str], Awaitable[None]]

#: The three the operator chooses between, in the order they are offered:
#: ask every time, let the model judge, don't ask. `session` exists in D14 and
#: is deliberately not here — it is a grant *scope*, not a posture, and a
#: four-button selector where one button means something structurally
#: different is how a safety control gets misread.
SELECTABLE_MODES = ("manual", "smart", "auto")

#: What each one means on a screen the operator reads in one second. Written
#: as consequences, not as names: "smart" tells you nothing about what will
#: happen the next time Nomad wants to run something.
MODE_LABELS = {
    "manual": "Ask me every time",
    "smart": "Ask when it matters",
    "auto": "Don't ask",
}

_LOOPBACK_NAMES = {"localhost", "ip6-localhost"}
_SHUTDOWN_TIMEOUT_S = 5.0

#: Where the token lives once the browser has it. `HttpOnly` keeps it away from
#: the page's own JavaScript, and `SameSite=Strict` means no cross-site request
#: carries it at all — belt and braces with the JSON/Origin checks below.
_COOKIE_NAME = "nomad_view"

#: A request body is a sentence and a token. Anything larger is not an operator.
_MAX_BODY_BYTES = 8192

#: The one field on the page that carries operator text. Long enough for a real
#: instruction, short enough that the endpoint is not a file upload.
_MAX_TEXT_CHARS = 4000

#: Dismissing the prompt rather than choosing one of its options.
DISMISS_INDEX = -1

_STYLE = """
  body { background:#111; color:#eee; font-family:ui-monospace,monospace;
         display:flex; justify-content:center; padding:2rem; }
  .device { background:#000; border:2px solid #333; border-radius:12px;
            padding:1rem; width:__WIDTH__px; min-height:__HEIGHT__px; }
  pre { white-space:pre-wrap; word-break:break-word; margin:0; font-size:14px; }
  .caption { color:#666; font-size:11px; text-align:center; margin-top:.6rem; }
  form { display:flex; gap:.4rem; margin-top:.8rem; width:__WIDTH__px; }
  input[type=text] { flex:1; background:#000; color:#eee; border:1px solid #333;
                     border-radius:6px; padding:.5rem; font:inherit; }
  button { background:#1d1d1d; color:#eee; border:1px solid #444; border-radius:6px;
           padding:.5rem .8rem; font:inherit; cursor:pointer; }
  button:disabled { opacity:.4; cursor:default; }
  #prompt { margin-top:.8rem; width:__WIDTH__px; display:none; }
  #prompt.live { display:block; }
  #prompt button { display:block; width:100%; margin-bottom:.3rem; text-align:left; }
  .warn { color:#c58; font-size:11px; text-align:center; margin-top:.6rem; }
  #modes { display:flex; gap:.3rem; margin-top:.8rem; width:__WIDTH__px; }
  #modes button { flex:1; font-size:11px; padding:.4rem .2rem; text-align:center; }
  #modes button.on { background:#2b3a2b; border-color:#5a7a5a; color:#cfe6cf; }
  #modes button.on.loose { background:#3a2b2b; border-color:#7a5a5a; color:#e6cfcf; }
"""

#: The read-only page: unchanged from chunk F1, meta-refresh and all. A device
#: with no way to drive it keeps the simplest possible viewer.
_PAGE_READONLY = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="__REFRESH__">
<title>Nomad screen</title>
<style>__STYLE__</style>
</head>
<body>
<div>
  <div class="device">__SCREEN__</div>
  <div class="caption">nomad headless display &middot; refreshing every __REFRESH__s</div>
</div>
</body>
</html>
"""

#: The interactive page. **No meta refresh**: a whole-page reload every second
#: would delete whatever the operator was half-way through typing, which is a
#: fine trade for a viewer and a useless one for an input device. It polls
#: `/state` instead and repaints only the parts that changed.
_PAGE_INTERACTIVE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Nomad</title>
<style>__STYLE__</style>
</head>
<body>
<div>
  <div class="device" id="screen">__SCREEN__</div>
  <div id="prompt"></div>
  __COMPOSE__
  <div id="modes"></div>
  <div class="caption">nomad __WHERE__ &middot; polling every __REFRESH__s</div>
  <div class="warn">this page can start turns and answer authorization prompts</div>
</div>
<script>
const post = async (path, body) => {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  return res.json().catch(() => ({}));
};
let shownToken = null;
// The token last clicked, and when. The answer is dispatched and not waited
// on, so without this the buttons sit there until the next poll — up to a
// full second of nothing after a tap, which feels broken. Collapsing on click
// is a claim, not a confirmation, so it expires: if the same question is still
// pending __ANSWER_GRACE_MS__ later the answer did not land and the buttons
// come back rather than leaving a question that looks answered and is not.
let answeredToken = null;
let answeredAt = 0;
const prompt = document.getElementById('prompt');
const collapse = () => { prompt.className = ''; prompt.replaceChildren(); shownToken = null; };
const renderPrompt = (p) => {
  if (!p) { collapse(); answeredToken = null; return; }
  if (p.token === answeredToken) {
    if (Date.now() - answeredAt < __ANSWER_GRACE_MS__) return;
    answeredToken = null;  // it did not take; show it again
  }
  if (p.token === shownToken) return;
  shownToken = p.token;
  prompt.replaceChildren();
  const choose = (index) => {
    answeredToken = p.token;
    answeredAt = Date.now();
    collapse();
    post('/answer', {token: p.token, index: index}).then(poll);
  };
  p.options.forEach((option, index) => {
    const button = document.createElement('button');
    button.textContent = option;
    button.onclick = () => choose(index);
    prompt.appendChild(button);
  });
  const dismiss = document.createElement('button');
  dismiss.textContent = 'Dismiss';
  dismiss.onclick = () => choose(__DISMISS__);
  prompt.appendChild(dismiss);
  prompt.className = 'live';
};
const modes = document.getElementById('modes');
let shownMode = null;
const renderModes = (state) => {
  if (!state.modes) { modes.replaceChildren(); shownMode = null; return; }
  // Rebuilt only when the mode actually changed, so a click does not race a
  // poll that is halfway through replacing the button under the cursor.
  if (state.mode === shownMode) return;
  shownMode = state.mode;
  modes.replaceChildren();
  state.modes.forEach((entry) => {
    const button = document.createElement('button');
    button.textContent = entry.label;
    button.title = entry.name;
    if (entry.name === state.mode) {
      // `auto` is coloured differently on purpose: the operator should be able
      // to see at a glance that the device is not asking any more.
      button.className = entry.name === 'auto' ? 'on loose' : 'on';
    }
    button.onclick = async () => { await post('/mode', {mode: entry.name}); poll(); };
    modes.appendChild(button);
  });
};
const poll = async () => {
  try {
    const state = await (await fetch('/state', {cache: 'no-store'})).json();
    document.getElementById('screen').innerHTML = state.screen;
    renderPrompt(state.prompt);
    renderModes(state);
  } catch (err) { /* the device went away; the next tick will say so */ }
};
const form = document.getElementById('compose');
if (form) {
  form.onsubmit = async (event) => {
    event.preventDefault();
    const field = form.querySelector('input');
    const text = field.value.trim();
    if (!text) return;
    field.value = '';
    await post('/submit', {text: text});
    poll();
  };
}
poll();
setInterval(poll, __REFRESH_MS__);
</script>
</body>
</html>
"""

_COMPOSE = (
    '<form id="compose" autocomplete="off">'
    '<input type="text" name="text" maxlength="__MAXLEN__" placeholder="say something to Nomad">'
    '<button type="submit">Send</button></form>'
)


def _render(template: str, **fields: object) -> str:
    """`str.format` is unusable here: the page is mostly JavaScript braces."""
    out = template
    for key, value in fields.items():
        out = out.replace(f"__{key.upper()}__", str(value))
    return out


def _address_family(host: str) -> int:
    """`::1` needs an AF_INET6 socket; `http.server` defaults to AF_INET."""
    try:
        return socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    except ValueError:
        return socket.AF_INET


def is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ScreenServer:
    """Component serving the current screen on localhost, auto-refreshing."""

    name = "view_server"

    def __init__(
        self,
        screen_source: ScreenSource,
        *,
        host: str = "127.0.0.1",
        port: int = 8081,
        refresh_seconds: float = 1.0,
        width: int = 320,
        height: int = 240,
        submit_text: TextSubmit | None = None,
        pending_choice: PendingSource | None = None,
        answer_choice: AnswerChoice | None = None,
        token: str | None = None,
        mode_source: ModeSource | None = None,
        set_mode: SetMode | None = None,
    ) -> None:
        self._screen_source = screen_source
        self._host = host
        #: The shared secret, or `None` for the loopback-only original. An
        #: empty string is not a token — it would authenticate every request
        #: that omitted the header, which is the opposite of the point.
        self._token = token or None
        self._port = port
        self._refresh_seconds = refresh_seconds
        self._width = width
        self._height = height
        self._submit_text = submit_text
        self._pending_choice = pending_choice
        self._answer_choice = answer_choice if pending_choice is not None else None
        self._mode_source = mode_source
        #: Paired with the reader for the same reason `answer_choice` is paired
        #: with `pending_choice`: a selector that can change the mode but not
        #: show it would leave the operator guessing which one they are in,
        #: and this is the control that decides whether the device asks.
        self._set_mode = set_mode if mode_source is not None else None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state = ComponentState.NEW
        #: Captured in `start()` and cleared in `stop()`. The HTTP thread has
        #: no loop of its own, and `get_event_loop()` from a non-loop thread is
        #: either an error or, worse, a *different* loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        #: One turn may be in flight from this page at a time. `AgentSession`
        #: already serializes turns behind a lock, so without this an impatient
        #: operator (or a page left open on a broken form) would queue an
        #: unbounded pile of coroutines against an unauthenticated endpoint.
        #: Refusing is also the honest answer: the device is busy.
        self._submitting = threading.Lock()
        self.turns_submitted = 0
        #: Counted rather than logged in detail: a device on a network gets
        #: scanned, and the useful signal is "someone is knocking", not each
        #: individual knock.
        self.unauthorized = 0

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def writable(self) -> bool:
        """Whether this view can change anything, or is only a window."""
        return (
            self._submit_text is not None
            or self._answer_choice is not None
            or self._set_mode is not None
        )

    @property
    def port(self) -> int:
        """The port actually bound. Differs from the requested one when 0."""
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    @property
    def remote(self) -> bool:
        """Whether this view is reachable from anywhere but this machine."""
        return not is_loopback(self._host)

    @property
    def url(self) -> str:
        """The address to point a browser at — no token in it.

        The token deliberately does not appear here, because this string is
        logged at boot and printed by `nomad status`. `login_url` is the one
        that carries it, and only a caller that asked for it gets it.
        """
        host = "127.0.0.1" if self._host in _LOOPBACK_NAMES else self._host
        if host in ("0.0.0.0", "::"):  # noqa: S104 - the bind address, not a URL
            # A wildcard bind is not somewhere a browser can go. Name the host
            # the operator would actually type; the socket still listens on all
            # interfaces.
            host = socket.gethostname()
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    @property
    def login_url(self) -> str:
        """`url` plus the token, for handing to the operator exactly once."""
        if self._token is None:
            return self.url
        return f"{self.url}?token={self._token}"

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        if self.remote and self._token is None:
            self._state = ComponentState.FAILED
            raise ConfigError(
                f"[view].host is '{self._host}', which is not loopback, and no token is "
                "set. A view that can start turns and answer authorization prompts must "
                "not be reachable off this machine without one.",
                {"host": self._host},
            )

        # Bound synchronously so a port clash fails the start (and rolls the
        # lifecycle back) rather than dying unnoticed inside the thread.
        server_class = type(
            "NomadViewServer",
            (ThreadingHTTPServer,),
            {"address_family": _address_family(self._host), "daemon_threads": True},
        )
        # Captured *before* the socket is served, so no request can reach a
        # dispatch path with a `None` loop.
        self._loop = asyncio.get_running_loop()
        self._server = server_class((self._host, self._port), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="nomad-view",
            daemon=True,
        )
        self._thread.start()
        self._state = ComponentState.STARTED
        logger.info("Screen view serving", extra={"url": self.url, "writable": self.writable})

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self._loop = None
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, _SHUTDOWN_TIMEOUT_S)
        self._state = ComponentState.STOPPED

    # -- rendering ---------------------------------------------------------

    def _render_page(self) -> str:
        style = _render(_STYLE, width=self._width, height=self._height)
        if not self.writable:
            return _render(
                _PAGE_READONLY,
                style=style,
                refresh=self._refresh_seconds,
                screen=self._screen_source(),
            )
        compose = _render(_COMPOSE, maxlen=_MAX_TEXT_CHARS) if self._submit_text is not None else ""
        return _render(
            _PAGE_INTERACTIVE,
            style=style,
            refresh=self._refresh_seconds,
            refresh_ms=max(200, int(self._refresh_seconds * 1000)),
            # Three polls' worth: one for the answer to reach the loop, and
            # slack so a slow tick does not flap the buttons back onto a
            # question that is about to disappear anyway.
            answer_grace_ms=max(2000, int(self._refresh_seconds * 3000)),
            screen=self._screen_source(),
            compose=compose,
            dismiss=DISMISS_INDEX,
            where="over the network" if self.remote else "on loopback",
        )

    def _state_payload(self) -> dict[str, Any]:
        """What the interactive page polls for. Read live, from the HTTP thread.

        Both reads are of a single attribute holding an immutable value —
        `HeadlessDisplay` rebinds one frozen `Screen` per draw and
        `ExternalChoicePrompter` rebinds one frozen `PendingQuestion` per
        question — so a reader sees the previous value or the next one, never
        half of either. Nothing here touches the event loop.
        """
        pending = self._pending_choice() if self._pending_choice is not None else None
        prompt = None
        if pending is not None:
            prompt = {
                "token": pending.token,
                "question": pending.question,
                "options": list(pending.options),
            }
        payload: dict[str, Any] = {
            "screen": self._screen_source(),
            "prompt": prompt,
            "can_submit": self._submit_text is not None,
            "busy": self._submitting.locked(),
        }
        if self._mode_source is not None:
            payload["mode"] = self._mode_source()
            if self._set_mode is not None:
                payload["modes"] = [
                    {"name": name, "label": MODE_LABELS[name]} for name in SELECTABLE_MODES
                ]
        return payload

    # -- crossing the thread boundary --------------------------------------

    def _dispatch(self, coro: Any) -> concurrent.futures.Future[Any] | None:
        """Hand a coroutine to the event loop from the HTTP thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _start_turn(self, text: str) -> tuple[int, dict[str, Any]]:
        """Dispatch a turn and return at once.

        A turn is a model call: it can take minutes and it holds the session
        lock while it does. Waiting for it here would tie up the request and
        tell the operator nothing they could not see on the screen, so the
        coroutine is launched and the HTTP thread walks away. The only thing
        kept is a callback that logs a failure, because a fire-and-forget
        coroutine whose exception nobody reads is how a device goes quiet.
        """
        submit = self._submit_text
        if submit is None:  # pragma: no cover - route is not mounted
            return 404, {"error": "not enabled"}
        if not self._submitting.acquire(blocking=False):
            return 409, {"error": "a turn is already running"}

        future = self._dispatch(submit(text))
        if future is None:
            self._submitting.release()
            return 503, {"error": "the device is not running"}

        def done(fut: concurrent.futures.Future[Any]) -> None:
            self._submitting.release()
            error = fut.exception() if not fut.cancelled() else None
            if error is not None:
                logger.warning(
                    "Turn submitted from the browser view failed",
                    extra={"error": f"{type(error).__name__}: {error}"},
                )

        future.add_done_callback(done)
        self.turns_submitted += 1
        return 202, {"submitted": True}

    def _answer(self, token: str, index: int) -> tuple[int, dict[str, Any]]:
        """Resolve a pending prompt, and — like a turn — do not wait for it.

        Waiting was the first shape and it was wrong. `future.result()` blocks
        the HTTP thread until the loop gets round to the coroutine, so a busy
        loop turns a click into a hung request, and any caller that is *itself*
        on the loop deadlocks outright. Neither is worth the confirmation,
        because the page already polls `/state`: the prompt disappearing is
        what "it landed" looks like, and it is the same signal whether the
        answer resolved or the prompt had already expired underneath it.
        """
        answer = self._answer_choice
        if answer is None:  # pragma: no cover - route is not mounted
            return 404, {"error": "not enabled"}
        future = self._dispatch(answer(token, index))
        if future is None:
            return 503, {"error": "the device is not running"}

        def done(fut: concurrent.futures.Future[Any]) -> None:
            error = fut.exception() if not fut.cancelled() else None
            if error is not None:
                logger.warning(
                    "Answering the prompt from the browser view failed",
                    extra={"error": f"{type(error).__name__}: {error}"},
                )
            elif not fut.cancelled() and not fut.result():
                # Almost always a page whose prompt already expired or was
                # resolved. Nothing failed; there was nothing left to answer.
                logger.info("Prompt answer from the browser view resolved nothing")

        future.add_done_callback(done)
        return 202, {"submitted": True}

    def _switch_mode(self, mode: str) -> tuple[int, dict[str, Any]]:
        """Change the running session's permission mode (D14).

        Validated against `SELECTABLE_MODES` here rather than trusting the
        page: this endpoint decides whether the device asks before it acts, so
        it is the last thing that should accept whatever string arrives. An
        unknown value is refused, not coerced to a default — and certainly not
        to the loosest one.

        Like the other two writers, this dispatches and returns. Tightening to
        `manual` revokes standing grants, which is a database write; the page
        polls `/state` and sees the new mode when it has actually taken.
        """
        setter = self._set_mode
        if setter is None:  # pragma: no cover - route is not mounted
            return 404, {"error": "not enabled"}
        if mode not in SELECTABLE_MODES:
            return 400, {"error": f"unknown mode '{mode}'"}
        future = self._dispatch(setter(mode))
        if future is None:
            return 503, {"error": "the device is not running"}

        def done(fut: concurrent.futures.Future[Any]) -> None:
            error = fut.exception() if not fut.cancelled() else None
            if error is not None:
                logger.warning(
                    "Switching the permission mode from the browser view failed",
                    extra={"error": f"{type(error).__name__}: {error}"},
                )

        future.add_done_callback(done)
        # Logged at info, unlike the other writes: which mode the device is in
        # is the single most consequential thing an operator can change here,
        # and the audit trail should not have to be reconstructed from a
        # session record.
        logger.info("Permission mode change requested from the browser view", extra={"mode": mode})
        return 202, {"submitted": True}

    # -- the handler -------------------------------------------------------

    def _same_origin(self, origin: str) -> bool:
        """Whether `Origin` is this very server, port included.

        Port included because another service on loopback is every bit as
        foreign as another host: "it came from 127.0.0.1" is not a statement
        about who sent it.
        """
        try:
            parts = urlsplit(origin)
            if parts.scheme != "http" or parts.hostname is None:
                return False
            if (parts.port or 80) != self.port:
                return False
            host = parts.hostname
            if host in _LOOPBACK_NAMES:
                return True
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return True
            except ValueError:
                pass
            # A remote view is reached by whatever the operator typed —
            # `nomad.local`, a bare hostname, a Tailscale address — and this
            # server cannot enumerate them. The token is what authorizes the
            # request; the `SameSite=Strict` cookie is what keeps another site
            # from carrying it. This check is the third layer, and it only has
            # to be exact while there is nothing else.
            #
            # This used to answer `False` for any non-loopback *IP*, because
            # the fall-through lived in the `except ValueError` arm and a
            # dotted quad parses. So a view reached by name worked and the
            # same view reached by address refused every write — every button
            # on the page 403'd while the page itself loaded fine, which reads
            # as "the buttons are broken", not as a security check firing.
            return self.remote
        except ValueError:
            return False

    def _authorized(self, headers: Any, query_token: str | None = None) -> bool:
        """Whether this request carries the shared secret.

        Three places to look, in the order they arrive over a session's life:
        the `?token=` that bootstrapped the browser, the cookie it was moved
        into, and the `Authorization: Bearer` a script would use. All three are
        compared with `hmac.compare_digest` — the comparison is not a plausible
        attack here, but a token check that leaks its own timing is the kind of
        detail that gets copied into somewhere it matters.
        """
        expected = self._token
        if expected is None:
            return True
        offered: list[str] = []
        if query_token:
            offered.append(query_token)
        authorization = headers.get("Authorization") or ""
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            offered.append(value.strip())
        cookies = http.cookies.SimpleCookie()
        with contextlib.suppress(http.cookies.CookieError):
            cookies.load(headers.get("Cookie") or "")
        morsel = cookies.get(_COOKIE_NAME)
        if morsel is not None:
            offered.append(morsel.value)
        return any(hmac.compare_digest(candidate, expected) for candidate in offered)

    def _reject_write(self, headers: Any) -> str | None:
        """Why this write must not happen, or `None` to let it through."""
        content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            # A cross-origin page can only send urlencoded, multipart or plain
            # text without a preflight it cannot pass. Requiring JSON is what
            # makes the browser ask permission first.
            return "requests must be application/json"
        origin = headers.get("Origin")
        if origin is not None and not self._same_origin(origin):
            return "cross-origin request refused"
        site = headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return "cross-site request refused"
        return None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # The prompt is a click target that authorizes tool calls, so
                # it must not be framable by anything, ever.
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                )
                # A no-op unless this request arrived with a `?token=`, which
                # only the first one from a given browser does.
                self._set_cookie_if_bootstrapping()
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

            def _query_token(self) -> str | None:
                query = parse_qs(urlsplit(self.path).query)
                values = query.get("token")
                return values[0] if values else None

            def _gate(self) -> bool:
                """Answer the request myself when it is not authorized."""
                if server._authorized(self.headers, self._query_token()):
                    return True
                server.unauthorized += 1
                # No detail, and the count is what gets read. A 401 body that
                # explained itself would be a scanner's confirmation that
                # something worth guessing at lives here.
                self._send_json(401, {"error": "unauthorized"})
                return False

            def _set_cookie_if_bootstrapping(self) -> None:
                """Move a `?token=` out of the URL and into a cookie.

                The URL form is the only way to hand a browser the secret, and
                it is the worst place to leave it: address bar, history, and
                the `Referer` on anything the page loads. So the first request
                that carries one gets the cookie and the page is served from
                the clean URL thereafter.
                """
                token = self._query_token()
                if not token or server._token is None:
                    return
                self.send_header(
                    "Set-Cookie",
                    f"{_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
                )

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                path = self.path.split("?", 1)[0]
                if not self._gate():
                    return
                if path == "/":
                    self._send(
                        200, server._render_page().encode("utf-8"), "text/html; charset=utf-8"
                    )
                elif path == "/screen":
                    # The bare fragment, for anything that wants to embed it.
                    self._send(
                        200, server._screen_source().encode("utf-8"), "text/html; charset=utf-8"
                    )
                elif path == "/state" and server.writable:
                    self._send_json(200, server._state_payload())
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
                path = self.path.split("?", 1)[0]
                if not self._gate():
                    return
                enabled = {
                    "/submit": server._submit_text is not None,
                    "/answer": server._answer_choice is not None,
                    "/mode": server._set_mode is not None,
                }
                if not enabled.get(path, False):
                    self.send_error(404)
                    return
                refusal = server._reject_write(self.headers)
                if refusal is not None:
                    logger.warning(
                        "Refused a write to the browser view",
                        extra={"path": path, "reason": refusal},
                    )
                    self._send_json(403, {"error": refusal})
                    return
                body = self._read_body()
                if body is None:
                    return
                if path == "/submit":
                    text = body.get("text")
                    if not isinstance(text, str) or not text.strip():
                        self._send_json(400, {"error": "text is required"})
                        return
                    self._send_json(*server._start_turn(text.strip()[:_MAX_TEXT_CHARS]))
                    return
                if path == "/mode":
                    mode = body.get("mode")
                    if not isinstance(mode, str):
                        self._send_json(400, {"error": "mode is required"})
                        return
                    self._send_json(*server._switch_mode(mode))
                    return
                token, index = body.get("token"), body.get("index")
                # `isinstance(True, int)` is True, and `choices[True]` is
                # `choices[1]` — posting `{"index": true}` silently selected
                # the second option, which in an authorization prompt is a
                # real answer the operator never gave. Booleans are rejected
                # explicitly rather than coerced.
                if (
                    not isinstance(token, str)
                    or isinstance(index, bool)
                    or not isinstance(index, int)
                ):
                    self._send_json(400, {"error": "token and index are required"})
                    return
                self._send_json(*server._answer(token, index))

            def _read_body(self) -> dict[str, Any] | None:
                """Bounded, and it answers the request itself when it refuses."""
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > _MAX_BODY_BYTES:
                    self._send_json(413, {"error": "body too large"})
                    return None
                try:
                    parsed = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._send_json(400, {"error": "body must be JSON"})
                    return None
                if not isinstance(parsed, dict):
                    self._send_json(400, {"error": "body must be a JSON object"})
                    return None
                return parsed

            def log_message(self, fmt: str, *args: Any) -> None:
                # http.server logs every request to stderr by default, which
                # would drown the boot output at one line per second.
                logger.debug("view request", extra={"detail": fmt % args})

        return Handler
