"""The browser view: watching the screen, and now driving it (D9, chunk F3).

`HeadlessDisplay`'s docstring promises "you can watch what the model just put
on the screen while building the rest of the stack". Until this module that was
false: the only way to see the screen was a Python REPL holding the object.

Deliberately `http.server` on a thread and not a web framework. This started as
one route returning one string; it is now three, which is still not a reason to
put a second HTTP stack in the process.

**This is a write surface on loopback, and that is a deliberate, narrow
exception rather than an oversight.** Until chunk F3 nothing in `src/` could
start a turn or answer an authorization prompt — with a headless display the
device denied every gated tool call permanently, by construction. The cheapest
honest fix is to let the page that already shows the screen also act as the
input peripheral the ESP32 will eventually be. That raises the stakes on a
server with no authentication, so:

* **Loopback is enforced, not documented.** HTTP API authentication is on the
  deliberately-deferred list in DECISIONS.md, so anything here that binds a
  socket is one config edit away from being the unauthenticated network service
  that gets there first. `start()` refuses a non-loopback host rather than
  trusting the default, and that refusal is now load-bearing for *writes*.
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
import ipaddress
import json
import socket
import threading
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

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

_LOOPBACK_NAMES = {"localhost", "ip6-localhost"}
_SHUTDOWN_TIMEOUT_S = 5.0

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
  <div class="caption">nomad on loopback &middot; polling every __REFRESH__s</div>
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
const prompt = document.getElementById('prompt');
const renderPrompt = (p) => {
  if (!p) { prompt.className = ''; prompt.replaceChildren(); shownToken = null; return; }
  if (p.token === shownToken) return;
  shownToken = p.token;
  prompt.replaceChildren();
  p.options.forEach((option, index) => {
    const button = document.createElement('button');
    button.textContent = option;
    button.onclick = () => post('/answer', {token: p.token, index: index});
    prompt.appendChild(button);
  });
  const dismiss = document.createElement('button');
  dismiss.textContent = 'Dismiss';
  dismiss.onclick = () => post('/answer', {token: p.token, index: __DISMISS__});
  prompt.appendChild(dismiss);
  prompt.className = 'live';
};
const poll = async () => {
  try {
    const state = await (await fetch('/state', {cache: 'no-store'})).json();
    document.getElementById('screen').innerHTML = state.screen;
    renderPrompt(state.prompt);
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
    "<button type=\"submit\">Send</button></form>"
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


def _is_loopback(host: str) -> bool:
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
    ) -> None:
        self._screen_source = screen_source
        self._host = host
        self._port = port
        self._refresh_seconds = refresh_seconds
        self._width = width
        self._height = height
        self._submit_text = submit_text
        self._pending_choice = pending_choice
        self._answer_choice = answer_choice if pending_choice is not None else None
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

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def writable(self) -> bool:
        """Whether this view can change anything, or is only a window."""
        return self._submit_text is not None or self._answer_choice is not None

    @property
    def port(self) -> int:
        """The port actually bound. Differs from the requested one when 0."""
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self._host in _LOOPBACK_NAMES else self._host
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        if not _is_loopback(self._host):
            self._state = ComponentState.FAILED
            raise ConfigError(
                f"[view].host must be a loopback address, got '{self._host}'. The screen "
                "view has no authentication and must not be exposed on a network.",
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
        compose = (
            _render(_COMPOSE, maxlen=_MAX_TEXT_CHARS) if self._submit_text is not None else ""
        )
        return _render(
            _PAGE_INTERACTIVE,
            style=style,
            refresh=self._refresh_seconds,
            refresh_ms=max(200, int(self._refresh_seconds * 1000)),
            screen=self._screen_source(),
            compose=compose,
            dismiss=DISMISS_INDEX,
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
        return {
            "screen": self._screen_source(),
            "prompt": prompt,
            "can_submit": self._submit_text is not None,
            "busy": self._submitting.locked(),
        }

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
            host = parts.hostname
            return (
                (host in _LOOPBACK_NAMES or ipaddress.ip_address(host).is_loopback)
                and (parts.port or 80) == self.port
            )
        except ValueError:
            return False

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
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                path = self.path.split("?", 1)[0]
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
                enabled = {
                    "/submit": server._submit_text is not None,
                    "/answer": server._answer_choice is not None,
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
