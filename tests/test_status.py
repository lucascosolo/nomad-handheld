"""The device can say how it is, and a terminal can ask it.

Two things are being pinned here, and only the second is about text:

* **Collecting a status starts nothing.** `nomad status` has to be safe to run
  against a Nomad that is already up, which means it must not bind the view's
  port, open the serial link, or start the session. A test that only checked
  the rendering would pass on an implementation that boots the whole device to
  read a battery percentage.
* **The backend probe reports what is there, not what is configured.** The
  interesting direction is negative: `backend = "claude_cli"` with no CLI on
  `PATH` must come back `ready=False`, because that device looks identical to
  a working one from the outside and answers nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nomad.status
from nomad.app import NomadApp
from nomad.cli import build_parser, cmd_status
from nomad.core.config import NomadConfig
from nomad.core.lifecycle import ComponentState
from nomad.status import (
    STATUS_WRITER,
    _auth_source,
    collect_status,
    probe_backend,
    render_status_json,
    render_status_text,
    status_rows,
)


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


async def test_status_reads_a_device_that_was_never_started(tmp_path: Path) -> None:
    """The property `nomad status` depends on: constructing is not starting."""
    app = NomadApp(_config(tmp_path))
    report = await collect_status(app, probe=False)

    assert app.state is ComponentState.NEW
    assert app.session.state is not ComponentState.STARTED
    assert report.session_id == "(not started)"
    assert report.state == "new"
    # The counts that need a database are absent rather than fabricated.
    assert report.notifications_pending is None
    assert report.tools > 0


async def test_status_of_a_running_device_names_its_session(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    await app.start()
    try:
        report = await collect_status(app, probe=False)
    finally:
        await app.stop()

    assert report.state == "started"
    assert report.session_id != "(not started)"
    assert report.notifications_pending == 0
    assert report.battery is not None
    assert report.components["agent_session"] == "started"
    # Every workload the governor knows about is named, with its tier — the
    # thing that tells an operator whether background work is parked.
    assert any("opportunistic" in state for state in report.workloads.values())


async def test_the_boot_card_reaches_the_screen(tmp_path: Path) -> None:
    """F1 says the screen is never blank. This is what it says."""
    app = NomadApp(_config(tmp_path, display={"driver": "mock"}))
    await app.start()
    try:
        drawn = app.display.screen.text  # type: ignore[attr-defined]
    finally:
        await app.stop()

    assert app.config.core.name in drawn
    assert "backend" in drawn and "mode" in drawn
    # The card is what is on the glass when nobody has asked for anything yet,
    # so it has to carry the two facts an operator standing over the device
    # needs: can it think, and who is allowed to say yes.
    assert "mock" in drawn and "manual" in drawn


async def test_the_status_card_is_arbitrated_like_every_other_writer(tmp_path: Path) -> None:
    """D36: one screen, one writer. The boot card is not an exception."""
    app = NomadApp(_config(tmp_path))
    assert STATUS_WRITER != ""
    view = app.screen.view(STATUS_WRITER)
    assert view.writer == STATUS_WRITER


async def test_a_failing_status_card_does_not_fail_the_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that will not finish booting because it could not draw a
    summary of itself has turned a cosmetic problem into an outage."""
    app = NomadApp(_config(tmp_path))

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no glass")

    monkeypatch.setattr(app.screen, "draw", explode)
    await app.start()
    try:
        assert app.state is ComponentState.STARTED
    finally:
        await app.stop()


async def test_an_unreadable_battery_is_a_line_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = NomadApp(_config(tmp_path))

    async def explode() -> None:
        raise OSError("i2c bus is gone")

    monkeypatch.setattr(app.battery, "read", explode)
    report = await collect_status(app, probe=False)

    assert report.battery is None
    assert any("battery unreadable" in problem for problem in report.problems)
    assert "i2c bus is gone" in render_status_text(report)


async def test_the_mock_backend_probes_ready(tmp_path: Path) -> None:
    health = await probe_backend(_config(tmp_path))
    assert health.ready is True
    assert health.auth == "not-required"


async def test_a_missing_cli_is_not_ready(tmp_path: Path) -> None:
    """The negative case is the whole point: a configured backend with nothing
    behind it looks exactly like a working one until someone asks it something."""
    config = _config(
        tmp_path,
        agent={"backend": "claude_cli", "claude_cli": {"cli_path": "claude-that-is-not-installed"}},
    )
    health = await probe_backend(config)
    assert health.ready is False
    assert "not on PATH" in health.detail or "Agent SDK" in health.detail


async def test_remote_llm_says_it_is_not_implemented(tmp_path: Path) -> None:
    health = await probe_backend(_config(tmp_path, agent={"backend": "remote_llm"}))
    assert health.ready is False
    assert "not implemented" in health.detail


async def test_an_installed_but_unauthenticated_cli_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly provisioned device looks healthy and can do nothing.

    The CLI installer writes `~/.claude.json` on a machine nobody has logged in
    on. Counting it as a credential reported `ready` for a Pi whose CLI answers
    `Not logged in · Please run /login` — the exact shape of failure this
    module exists to prevent, found by running the probe against a real device
    rather than by reading it.

    Asserted against `_auth_source` rather than through `probe_backend`,
    deliberately. The first version of this test went through the whole probe
    and so depended on whether a real CLI happened to be on `PATH` — it passed
    on the laptop, where one is, and failed on the device, where a
    non-interactive shell has no `~/.local/bin`. A test whose result depends on
    the machine it runs on cannot be the gate D22 needs it to be.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    cli = _config(tmp_path).agent.claude_cli
    assert _auth_source(cli) == "none"

    # And the credential file itself is what flips it.
    (home / ".claude" / ".credentials.json").write_text("{}")
    assert _auth_source(cli) == "cli-credentials"


async def test_the_probe_never_reports_an_api_key_as_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20 strips `ANTHROPIC_API_KEY` from the child environment. Reporting it
    as this device's credential would describe an authentication that cannot
    happen — and would bill per token if it somehow did."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-count")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    config = _config(
        tmp_path,
        agent={"backend": "claude_cli", "claude_cli": {"cli_path": "claude-that-is-not-installed"}},
    )
    health = await probe_backend(config)
    assert health.auth != "oauth-env"


async def test_the_three_renderings_agree(tmp_path: Path) -> None:
    app = NomadApp(_config(tmp_path))
    report = await collect_status(app, probe=False)

    text = render_status_text(report, verbose=True)
    rows = dict(status_rows(report))
    blob = render_status_json(report)

    assert report.mode in text
    assert rows["mode"] == report.mode
    assert report.mode in blob
    # Verbose lists what terse does not, and both come from one report.
    assert all(name in text for name in report.tool_names)


# -- the terminal face -----------------------------------------------------


def test_a_bare_invocation_still_means_run() -> None:
    """`python -m nomad` has meant "switch the device on" since F1, and a
    hundred lines of argparse must not quietly change that."""
    args = build_parser().parse_args([])
    assert getattr(args, "func", None) is None  # resolved by main(), not the parser

    args = build_parser().parse_args(["run"])
    assert args.func.__name__ == "cmd_run"


def test_the_parser_offers_the_four_commands() -> None:
    parser = build_parser()
    for command in ("run", "status", "ask", "chat"):
        assert parser.parse_args([command, *(["hi"] if command == "ask" else [])])


async def test_status_reports_the_prompter_the_device_would_actually_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status` must not report from an edited config.

    It used to construct the app with the browser view forced off, the way
    `ask` and `chat` do to avoid a port clash — and so it printed
    `NullChoicePrompter`, which denies everything, for a device that actually
    runs `ExternalChoicePrompter` and asks. That is the difference between a
    broken device and a working one, and it is precisely the fact an operator
    runs this command to learn.
    """
    config_file = tmp_path / "nomad.toml"
    config_file.write_text(
        "[storage]\n"
        f'path = "{tmp_path / "nomad.db"}"\n'
        "[workspace]\n"
        f'root = "{tmp_path / "workspace"}"\n'
        "[display]\n"
        'driver = "headless"\n'
        "[view]\n"
        "enabled = true\n"
        "port = 0\n"
    )
    monkeypatch.setenv("NOMAD_CONFIG", str(config_file))

    await cmd_status(build_parser().parse_args(["status"]))

    assert "ExternalChoicePrompter" in capsys.readouterr().out


async def test_status_exits_non_zero_when_the_backend_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Usable from a health check without anyone parsing prose."""
    config_file = tmp_path / "nomad.toml"
    config_file.write_text(
        "[storage]\n"
        f'path = "{tmp_path / "nomad.db"}"\n'
        "[workspace]\n"
        f'root = "{tmp_path / "workspace"}"\n'
        "[agent]\n"
        'backend = "remote_llm"\n'
    )
    monkeypatch.setenv("NOMAD_CONFIG", str(config_file))

    args = build_parser().parse_args(["status"])
    code = await cmd_status(args)

    assert code == 1
    assert "NOT READY" in capsys.readouterr().out


# -- a credential that exists and does not work -----------------------------


def _answer(value: object):
    """A stand-in for one of the CLI probes: ignores its arguments, answers."""

    async def probe(*_args: object, **_kwargs: object) -> object:
        return value

    return probe


async def test_an_expired_credential_is_not_reported_as_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found the same way as everything else in this file: by running it.

    The Pi reported `auth: cli-credentials` and `ready` while `claude auth
    status` answered `loggedIn: false` and every turn came back "OAuth session
    expired and could not be refreshed". The file was there; the credential was
    dead. A probe that reads the filesystem cannot tell those apart, so it asks
    the CLI whenever there is a CLI to ask.
    """
    monkeypatch.setattr(nomad.status, "_auth_source", lambda _cli: "cli-credentials")
    monkeypatch.setattr(nomad.status, "_cli_version", _answer("2.1.226 (Claude Code)"))
    monkeypatch.setattr(nomad.status, "_cli_logged_in", _answer(False))
    monkeypatch.setattr(nomad.status.shutil, "which", lambda _path: "/usr/bin/claude")

    health = await probe_backend(_config(tmp_path, agent={"backend": "claude_cli"}))

    assert health.auth == "expired"
    assert health.ready is False
    assert "expired" in health.detail


async def test_a_logged_out_cli_exits_nonzero_and_is_still_believed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude auth status` exits **1** when it is not logged in.

    The first version of this fix treated a non-zero exit as "no answer" and so
    discarded the only answer that mattered — the device kept reporting `ready`
    after the fix was deployed. This test goes through `_run_cli` rather than
    stubbing it, because stubbing it is precisely what hid the bug.
    """

    async def logged_out(*_args: object, **_kwargs: object):
        class Proc:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b'{"loggedIn": false, "authMethod": "none"}', b"")

        return Proc()

    monkeypatch.setattr(nomad.status.asyncio, "create_subprocess_exec", logged_out)

    assert await nomad.status._cli_logged_in("/usr/bin/claude") is False


async def test_a_cli_that_cannot_answer_is_not_called_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None` is not `False`. An old CLI without the subcommand, or one that
    times out on a busy device, is a device that could not be asked — and
    downgrading the report on that would make every slow boot look
    unauthenticated."""
    monkeypatch.setattr(nomad.status, "_auth_source", lambda _cli: "cli-credentials")
    monkeypatch.setattr(nomad.status, "_cli_version", _answer("2.1.226 (Claude Code)"))
    monkeypatch.setattr(nomad.status, "_cli_logged_in", _answer(None))
    monkeypatch.setattr(nomad.status.shutil, "which", lambda _path: "/usr/bin/claude")

    health = await probe_backend(_config(tmp_path, agent={"backend": "claude_cli"}))

    assert health.auth == "cli-credentials"
    assert health.ready is True
