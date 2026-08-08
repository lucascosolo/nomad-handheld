"""Serving the headless screen to a browser, over loopback only (D9).

`HeadlessDisplay`'s docstring promises "you can watch what the model just put
on the screen while building the rest of the stack". Until this module that was
false: the only way to see the screen was a Python REPL holding the object.

Deliberately `http.server` on a thread and not a web framework. This is one
route returning one string; taking a dependency for it would put a second HTTP
stack in the process purely to look at a `<pre>`.

**Loopback is enforced, not documented.** HTTP API authentication is on the
deliberately-deferred list in DECISIONS.md, so anything in this process that
binds a socket is one config edit away from being the unauthenticated network
service that gets there first. `start()` refuses a non-loopback host rather
than trusting the default.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nomad.core.errors import ConfigError
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: A callable returning the current screen as an HTML fragment. A callable and
#: not a driver: this module has no business knowing which display it is
#: looking at, and the read has to be live because the screen changes under it.
ScreenSource = Callable[[], str]

_LOOPBACK_NAMES = {"localhost", "ip6-localhost"}
_SHUTDOWN_TIMEOUT_S = 5.0

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>Nomad screen</title>
<style>
  body {{ background:#111; color:#eee; font-family:ui-monospace,monospace;
          display:flex; justify-content:center; padding:2rem; }}
  .device {{ background:#000; border:2px solid #333; border-radius:12px;
             padding:1rem; width:{width}px; min-height:{height}px; }}
  pre {{ white-space:pre-wrap; word-break:break-word; margin:0; font-size:14px; }}
  .caption {{ color:#666; font-size:11px; text-align:center; margin-top:.6rem; }}
</style>
</head>
<body>
<div>
  <div class="device">{screen}</div>
  <div class="caption">nomad headless display &middot; refreshing every {refresh}s</div>
</div>
</body>
</html>
"""


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
    ) -> None:
        self._screen_source = screen_source
        self._host = host
        self._port = port
        self._refresh_seconds = refresh_seconds
        self._width = width
        self._height = height
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state = ComponentState.NEW

    @property
    def state(self) -> ComponentState:
        return self._state

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
        self._server = server_class((self._host, self._port), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="nomad-view",
            daemon=True,
        )
        self._thread.start()
        self._state = ComponentState.STARTED
        logger.info("Screen view serving", extra={"url": self.url})

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, _SHUTDOWN_TIMEOUT_S)
        self._state = ComponentState.STOPPED

    # -- the handler -------------------------------------------------------

    def _render_page(self) -> str:
        return _PAGE.format(
            refresh=self._refresh_seconds,
            screen=self._screen_source(),
            width=self._width,
            height=self._height,
        )

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = server._render_page().encode("utf-8")
                elif path == "/screen":
                    # The bare fragment, for anything that wants to embed it.
                    body = server._screen_source().encode("utf-8")
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                # http.server logs every request to stderr by default, which
                # would drown the boot output at one line per second.
                logger.debug("view request", extra={"detail": fmt % args})

        return Handler
