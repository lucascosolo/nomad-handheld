"""The terminal face of the device.

Until this file existed, `AgentSession.send()` had exactly one caller in `src/`
— crash recovery replaying an aborted turn — and the only way a human could
begin a conversation was the browser page. That made the whole system
unreachable over SSH, which is how a headless Pi in a pocket is actually
reached, and it made "does the backend work" a question nobody could ask
without writing a script.

Four subcommands, and the split between them is the point:

* `run` — the daemon. Unchanged behaviour, still what systemd starts.
* `status` — read-only, starts nothing, binds nothing. Safe to run against a
  device that is already up.
* `ask` — one turn, printed, then stop. The smallest possible proof that this
  device can think.
* `chat` — a REPL over the same `submit()` the screen uses, so the onboard
  tier answers here exactly as it would on the glass.

`ask` and `chat` disable the browser view by default. Two processes cannot bind
one port, and a CLI that fails to start because a daemon is running would push
the operator toward killing the daemon — which is the opposite of what a
persistent session is for.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from nomad.app import NomadApp
from nomad.core.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    NomadConfig,
    load_config,
)
from nomad.core.errors import NomadError
from nomad.core.logging import configure_logging, get_logger
from nomad.status import collect_status, render_status_json, render_status_text

logger = get_logger(__name__)

#: What ends an interactive session. `Ctrl-D` does too, by EOF.
_QUIT_WORDS = frozenset({"exit", "quit", ":q"})


def config_path() -> Path | None:
    raw = os.environ.get(CONFIG_PATH_ENV)
    if raw:
        return Path(raw)
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None


def _load(args: argparse.Namespace) -> NomadConfig:
    config = load_config(config_path(), env=os.environ)
    if getattr(args, "quiet", False):
        # The terminal commands print an answer. Structured INFO logs
        # interleaved with it turn a readable reply into a haystack, so the
        # floor is raised rather than the logging turned off — a warning about
        # a denied tool call still has to reach the operator.
        quieter = config.core.model_copy(update={"log_level": "WARNING"})
        config = config.model_copy(update={"core": quieter})
    return config


def _headless(config: NomadConfig) -> NomadConfig:
    """The same device with its browser view off.

    A copy rather than a mutation: `NomadConfig` is what the running daemon is
    also reading, and a CLI that edited it in place would be a second writer to
    the one thing the whole tree treats as immutable.
    """
    return config.model_copy(update={"view": config.view.model_copy(update={"enabled": False})})


# -- commands --------------------------------------------------------------


async def cmd_run(args: argparse.Namespace) -> int:
    """Start, wait for a signal, stop. The daemon path."""
    import signal

    config = _load(args)
    configure_logging(config.core.log_level, config.core.log_format)  # type: ignore[arg-type]

    app = NomadApp(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        # A handler installed before start means a Ctrl-C during a slow boot
        # still shuts down rather than tearing the process down mid-migration.
        loop.add_signal_handler(sig, stop.set)

    try:
        await app.start()
    except Exception as exc:  # noqa: BLE001 - a failed boot is an exit code, not a traceback
        logger.error("Nomad failed to start", extra={"error": f"{type(exc).__name__}: {exc}"})
        await app.stop()
        return 1

    if app.view_url:
        logger.info(f"Watch the screen at {app.view_url}")
    logger.info("Nomad is running. Ctrl-C or SIGTERM to stop.")

    try:
        await stop.wait()
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        await app.stop()
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    """Report the device without starting it.

    `NomadApp.__init__` is free of side effects by contract — it binds no port,
    opens no serial link and runs no migration — so a report can be collected
    from a constructed app while a second Nomad is running the real one. What
    that costs is honesty about scope, and the header says it: this is the
    device's configuration and durable state, not a live session's.

    The durable counts do need the database, so `status` starts exactly two
    components by hand and stops them again. Going through the registry would
    start the view, the link and the session with it.
    """
    config = _load(args)
    configure_logging("WARNING" if not args.debug else config.core.log_level, "console")

    # The *real* config, not the view-less copy `ask` and `chat` use. Nothing
    # here starts the view, and `ScreenServer` binds nothing until `start()` —
    # so constructing it is free, and reporting from an edited config would
    # describe a device that does not exist. It said `NullChoicePrompter` on a
    # device that actually runs `ExternalChoicePrompter`, which is the
    # difference between "denies everything" and "asks the browser": exactly
    # the fact an operator runs this command to learn.
    app = NomadApp(config)
    started = False
    try:
        await app.db.start()
        started = True
        await app.migrator.start()
    except Exception as exc:  # noqa: BLE001 - report what is readable, say what is not
        logger.warning("Could not open the database", extra={"error": str(exc)})

    try:
        report = await collect_status(app, probe=not args.no_probe)
    finally:
        if started:
            await app.db.stop()

    if args.json:
        print(render_status_json(report))
    else:
        print(render_status_text(report, verbose=args.verbose))
        # Printed here and not carried in the report: `render_status_json` is
        # the shape a health check pipes into a file, and the token has no
        # business in one. This is a terminal the operator is already sitting
        # at, which is the one place handing it over is safe.
        login = app.view_login_url
        if login and login != report.view_url:
            print(f"\nopen this once to pair a browser:\n  {login}")
    # A device whose backend cannot run a turn exits non-zero, so this is
    # usable from a health check and a systemd `ExecStartPre` without anyone
    # having to parse the text.
    return 0 if report.backend.ready else 1


async def cmd_ask(args: argparse.Namespace) -> int:
    """One turn, start to finish. The smallest proof the device can think."""
    config = _load(args)
    configure_logging(config.core.log_level, config.core.log_format)  # type: ignore[arg-type]
    app = NomadApp(_headless(config) if not args.view else config)

    try:
        await app.start()
    except Exception as exc:  # noqa: BLE001
        print(f"nomad: failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        await app.stop()
        return 1
    try:
        return await _one_turn(app, " ".join(args.text))
    finally:
        await app.stop()


async def cmd_chat(args: argparse.Namespace) -> int:
    """A REPL over the same `submit()` the screen calls.

    Deliberately not a second input path: everything typed here goes through
    the onboard router first and the model second, exactly as an utterance
    from the glass would. A terminal that bypassed the router would answer
    differently from the device, which is the drift that makes a debug channel
    useless for debugging.
    """
    config = _load(args)
    configure_logging(config.core.log_level, config.core.log_format)  # type: ignore[arg-type]
    app = NomadApp(_headless(config) if not args.view else config)

    try:
        await app.start()
    except Exception as exc:  # noqa: BLE001
        print(f"nomad: failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        await app.stop()
        return 1

    report = await collect_status(app, probe=False)
    print(render_status_text(report))
    print("\nType a message, or 'exit'. Ctrl-C interrupts a turn; Ctrl-D quits.\n")
    try:
        while True:
            try:
                # `input()` blocks, and blocking the loop would stop the
                # notification poller and the governor dead while the operator
                # thinks about what to type (D1).
                text = await asyncio.to_thread(input, "you> ")
            except EOFError:
                print()
                break
            text = text.strip()
            if not text:
                continue
            if text.lower() in _QUIT_WORDS:
                break
            try:
                await _one_turn(app, text)
            except KeyboardInterrupt:
                # Interrupting a turn must not end the session — that is the
                # whole difference between a chat and a command.
                await app.session.interrupt()
                print("\n(interrupted)")
    finally:
        await app.stop()
    return 0


async def _one_turn(app: NomadApp, text: str) -> int:
    """Submit and print. Returns a shell-shaped code for `ask`."""
    outcome = await app.submit(text)
    handled = getattr(outcome, "handled", None)
    if handled is not None:
        # An onboard answer (chunk O). Labelled, because a device that answers
        # some things itself and others through the model with no way to tell
        # which happened is one you stop trusting.
        print(f"onboard> {outcome.content or outcome.reason}")
        return 0
    status = getattr(outcome, "status", None)
    body = getattr(outcome, "text", "") or ""
    error = getattr(outcome, "error", None)
    print(f"nomad> {body}" if body else "nomad> (no reply)")
    if error:
        print(f"        ! {error}", file=sys.stderr)
    return 0 if str(status) in {"completed", "TurnOutcomeStatus.COMPLETED"} else 1


# -- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nomad", description="Nomad — a pocket computer that stays awake."
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the device and stay running (the daemon)")
    run.set_defaults(func=cmd_run, quiet=False)

    status = sub.add_parser("status", help="report the device; starts nothing, binds nothing")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("-v", "--verbose", action="store_true", help="list components and tools")
    status.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the backend subprocess probe (faster, less certain)",
    )
    status.add_argument("--debug", action="store_true", help="show the configured log level")
    status.set_defaults(func=cmd_status, quiet=True)

    ask = sub.add_parser("ask", help="run one turn and print the answer")
    ask.add_argument("text", nargs="+", help="what to ask")
    ask.add_argument("--view", action="store_true", help="also serve the browser screen")
    ask.add_argument("--verbose", action="store_true", help="show INFO logs during the turn")
    ask.set_defaults(func=cmd_ask)

    chat = sub.add_parser("chat", help="an interactive session over the same path as the screen")
    chat.add_argument("--view", action="store_true", help="also serve the browser screen")
    chat.add_argument("--verbose", action="store_true", help="show INFO logs during turns")
    chat.set_defaults(func=cmd_chat)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (`nomad`), and `python -m nomad`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # No subcommand means the daemon. `python -m nomad` has meant "switch
        # the device on" since F1 and a hundred lines of argparse must not
        # quietly change that.
        args = parser.parse_args(["run", *(argv or [])])
    if "quiet" not in args:
        args.quiet = not getattr(args, "verbose", False)

    try:
        return asyncio.run(args.func(args))
    except NomadError as exc:
        # Config errors happen before logging is configured, so this is the
        # one place a message has to reach the terminal on its own.
        sys.stderr.write(f"nomad: {exc}\n")
        return 2
    except KeyboardInterrupt:  # pragma: no cover - only without a signal handler
        return 0
