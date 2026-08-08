"""Classifying a shell command by where its effects land (D12, D21).

The asymmetry these tests encode: a false positive costs a prompt, a false
negative costs an unapproved shell on someone else's machine. Everything
ambiguous therefore resolves away from `LOCAL`.
"""

from __future__ import annotations

import pytest

from nomad.tools.egress import Egress, classify, classify_params


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "python -m pytest",
        "git status",
        "grep -r ssh_config /etc",  # names ssh, invokes grep
        "echo 'ssh prod'",  # quoted, so a single token
        "",
    ],
)
def test_local_commands_stay_local(command: str) -> None:
    assert classify(command) is Egress.LOCAL


@pytest.mark.parametrize(
    "command",
    [
        "ssh prod uptime",
        "/usr/bin/ssh prod uptime",
        "cat x | ssh prod sh",
        "env FOO=1 ssh prod uptime",
        "scp a.txt prod:/tmp/",
        "rsync -a ./ prod:/srv/",
        "sftp prod",
        "mosh prod",
        "sshpass -p hunter2 ssh prod",
        "nc 10.0.0.5 4444",
        "socat TCP:10.0.0.5:4444 EXEC:/bin/sh",
        "kubectl exec pod -- sh",
    ],
)
def test_anything_that_reaches_another_host_is_remote(command: str) -> None:
    assert classify(command) is Egress.REMOTE


def test_an_unbalanced_quote_is_unclassifiable_not_local() -> None:
    assert classify("ssh prod 'oops") is Egress.UNCLASSIFIABLE
    assert classify('echo "unterminated') is Egress.UNCLASSIFIABLE


def test_the_scan_covers_every_token_not_just_the_first() -> None:
    """`&&` and pipes make the first token a poor proxy for what runs."""
    assert classify("cd /tmp && ssh prod uptime") is Egress.REMOTE
    assert classify("true; ssh prod uptime") is Egress.REMOTE


def test_classify_params_finds_the_shell_command() -> None:
    assert classify_params({"command": "ls"}) is Egress.LOCAL
    assert classify_params({"command": "ssh prod uptime"}) is Egress.REMOTE


def test_params_with_no_command_carry_no_verdict() -> None:
    """`None` is "not a shell call", which is not the same as "local"."""
    assert classify_params({"file_path": "/tmp/x"}) is None


def test_a_non_string_command_is_unclassifiable_not_local() -> None:
    """The bridge routes before the params model validates; it must not guess."""
    assert classify_params({"command": ["ssh", "prod"]}) is Egress.UNCLASSIFIABLE
    assert classify_params({"command": None}) is Egress.UNCLASSIFIABLE
