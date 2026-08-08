"""`python -m nomad` — switch the device on.

Boots the composition root against whatever `nomad.toml` selects, which by
default is mocks the whole way down: no hardware, no network, no credentials
(D9). Then it waits. Nomad is a session, not a command — the process staying
alive *is* the product.

Shutdown is a signal, because on the device it always will be: SIGTERM from
systemd, SIGINT from a terminal, and in both cases the session gets to close
its turns and the database gets closed cleanly rather than being killed
mid-write. A startup failure exits non-zero so a supervisor can see it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from nomad.app import NomadApp
from nomad.core.config import DEFAULT_CONFIG_PATH, load_config
from nomad.core.errors import NomadError
from nomad.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

#: Points at a config other than `./nomad.toml`. The layered local override
#: and `NOMAD_*` env vars still apply on top of whatever this names.
CONFIG_PATH_ENV = "NOMAD_CONFIG"

_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _config_path() -> Path | None:
    raw = os.environ.get(CONFIG_PATH_ENV)
    if raw:
        return Path(raw)
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None


async def run() -> int:
    """Start, wait for a signal, stop. Returns the process exit code."""
    config = load_config(_config_path(), env=os.environ)
    configure_logging(config.core.log_level, config.core.log_format)  # type: ignore[arg-type]

    app = NomadApp(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in _SIGNALS:
        # A handler installed before start means a Ctrl-C during a slow boot
        # still shuts down rather than tearing the process down mid-migration.
        loop.add_signal_handler(sig, stop.set)

    try:
        await app.start()
    except Exception as exc:  # noqa: BLE001 - a failed boot is an exit code, not a traceback
        logger.error(
            "Nomad failed to start", extra={"error": f"{type(exc).__name__}: {exc}"}
        )
        await app.stop()
        return 1

    if app.view_url:
        logger.info(f"Watch the screen at {app.view_url}")
    logger.info("Nomad is running. Ctrl-C or SIGTERM to stop.")

    try:
        await stop.wait()
    finally:
        for sig in _SIGNALS:
            loop.remove_signal_handler(sig)
        await app.stop()
    return 0


def main() -> int:
    """Console-script entry point (`nomad`), and `python -m nomad`."""
    try:
        return asyncio.run(run())
    except NomadError as exc:
        # Config errors happen before logging is configured, so this is the
        # one place a message has to reach the terminal on its own.
        sys.stderr.write(f"nomad: {exc}\n")
        return 2
    except KeyboardInterrupt:  # pragma: no cover - only without a signal handler
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
