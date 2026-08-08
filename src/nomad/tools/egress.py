"""Does this shell command leave the device? (D12, D21)

D12 says tools act on a `Target`, and D21 says anything on an SSH target is
`never_auto`. Both are true of Nomad's own tools, where the target is named in
the request. Neither was true of the path the model actually takes: Claude Code
does not call an `ssh` *tool*, it calls `Bash("ssh prod 'rm -rf /'")`. Routed on
capabilities alone that is a local shell command, and the SSH guarantee is
bypassed on day one — not by an attack, but by the ordinary way the tool works.

So a command is classified by what it *invokes*. Any token whose basename is a
remote-execution binary makes the whole command an SSH-target call, which
inherits `never_auto` and the SSH target's own identity rather than the
device's.

The classifier is deliberately blunt and deliberately over-eager:

* It scans **every** token, not just the first, because `cat x | ssh host sh`
  and `env FOO=1 ssh host` both reach a remote host.
* A command it cannot tokenise is `UNCLASSIFIABLE`, and the bridge denies it.
  An unbalanced quote is not a reason to guess local (D21: cannot classify,
  deny).
* A false positive costs a prompt. A false negative costs a shell on another
  machine with no approval. The asymmetry is the whole design.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

#: Binaries that execute or copy on a *different* host. `nc`/`socat` are here
#: because a reverse shell is remote execution wearing a different name.
REMOTE_EXEC_BINARIES: frozenset[str] = frozenset(
    {
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "mosh",
        "autossh",
        "ssh-copy-id",
        "sshpass",
        "telnet",
        "rsh",
        "rlogin",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "kubectl",
        "docker-machine",
    }
)

#: Parameter names that carry a shell command. Claude Code's `Bash` uses
#: `command`; keeping this a table means a future tool is one line, not a
#: second classifier.
COMMAND_PARAMS: tuple[str, ...] = ("command",)


class Egress(StrEnum):
    """Where a command's effects land."""

    LOCAL = "local"
    REMOTE = "remote"
    #: Could not be tokenised. The caller must deny — never fall back to local.
    UNCLASSIFIABLE = "unclassifiable"


def classify_params(params: Mapping[str, Any]) -> Egress | None:
    """Classify a tool call's parameters. `None` means it carries no command.

    Returning the verdict rather than the command text is deliberate: an
    earlier shape handed back a string, and a non-string `command` collapsed to
    `""`, which classified as `LOCAL`. Every "no command here" answer must be
    distinguishable from "a command I could not read".
    """
    for key in COMMAND_PARAMS:
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, str):
            return classify(value)
        # Not a string, so not tokenisable. `Bash`'s params model would reject
        # it later, but the routing decision happens first and must not guess.
        return Egress.UNCLASSIFIABLE
    return None


def classify(command: str) -> Egress:
    """Classify a shell command by the binaries it invokes."""
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return Egress.UNCLASSIFIABLE
    if not tokens and command.strip():
        # Non-empty text that tokenises to nothing is not something to reason
        # about confidently.
        return Egress.UNCLASSIFIABLE
    for token in tokens:
        if _basename(token) in REMOTE_EXEC_BINARIES:
            return Egress.REMOTE
    return Egress.LOCAL


def _basename(token: str) -> str:
    """`/usr/bin/ssh` and `ssh` are the same binary; so is `"ssh"` quoted."""
    stripped = token.strip().strip("\"'")
    if not stripped:
        return ""
    return PurePosixPath(stripped).name
