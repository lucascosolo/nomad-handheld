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

**Wrapped commands (the third adversarial review).** Scanning tokens is only
honest while a token *is* a word. `bash -c "ssh prod rm -rf /"` tokenises to
three tokens, the third of which is an entire command wearing one pair of
quotes, so the scan saw no `ssh` and confidently answered `LOCAL` — the same
bypass D27 closed, reopened by one level of quoting. `eval`, `su -c`,
`python -c`, `$(…)` and backticks are the same shape.

Perfect shell parsing is not available and is not attempted. What is available
is a rule about *which way to be wrong*, applied in three parts:

1. **A command string interpreted by a shell is classified as a command.**
   When any token names a shell or `eval`, every quoted compound in that
   command is re-tokenised and scanned (bounded depth). `bash -c "ls"` is
   therefore still `LOCAL` — read, not guessed — which is what keeps this from
   denying every wrapped-but-innocuous command.
2. **Code this module cannot read is `UNCLASSIFIABLE`, never `LOCAL`.**
   `python -c`, `perl -e`, `node --eval` and friends carry a language whose
   `os.system("ssh h")` this module has no business trying to parse. So does
   any command containing `$(`, a backtick or `<(`: the text that will run has
   not been written down yet.
3. **Precedence is REMOTE, then UNCLASSIFIABLE, then LOCAL.** `LOCAL` is only
   ever the answer when every token, at every depth, was read and understood.

Both new outcomes fail *toward* caution: `REMOTE` inherits `never_auto` and the
SSH target, and `UNCLASSIFIABLE` is denied by the bridge. Neither can widen
what a `LOCAL` verdict would have permitted.

*The residual gap, stated rather than papered over:* indirection through a
variable (`X=ssh; $X host`) and a command hidden in the quoted argument of a
binary this module does not know interprets it are both still read as `LOCAL`.
The line drawn here is "recurse where the text is *known* to be executed as a
command", because recursing into every quoted string would classify
`git commit -m "fix the ssh bug"` as remote — and on a device with no SSH
target registered that is not a prompt, it is a refusal to do ordinary work.
That is why the operator is now shown the command text itself (D36): the
classifier narrows what may run unattended, and the prompt is what lets a human
catch the rest.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
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

#: Binaries that hand a *string* to a shell, so the string is another command
#: and has to be classified as one. `eval` and `command` are builtins rather
#: than binaries, which is why this is matched on the token and not on `argv[0]`.
SHELL_WRAPPERS: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "mksh",
        "ash",
        "busybox",
        "fish",
        "eval",
        "command",
        "su",
        "doas",
        "xargs",
        "watch",
        "flock",
        "chroot",
        "unshare",
    }
)

#: Interpreters that take source code inline. Their argument is a *program*,
#: not a shell command, so re-tokenising it would be theatre — this module
#: cannot tell `print("ssh")` from `os.system("ssh h")` and must not try.
INLINE_CODE_INTERPRETERS: frozenset[str] = frozenset(
    {
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "deno",
        "bun",
        "php",
        "lua",
        "osascript",
        "Rscript",
        "julia",
    }
)

#: The flags that mean "the next argument is source code".
INLINE_CODE_FLAGS: frozenset[str] = frozenset(
    {"-c", "-e", "-E", "-r", "--eval", "--command", "--exec"}
)

#: Substrings that build a command out of the output of another one. The text
#: that will actually run does not exist yet, so there is nothing to classify.
COMMAND_SUBSTITUTIONS: tuple[str, ...] = ("$(", "`", "<(", ">(")

#: How many levels of quoting are followed before giving up. Three covers
#: `sh -c "sh -c '…'"`; past that, the honest answer is that nobody can read it.
MAX_NESTING_DEPTH = 3

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
    return _classify(command, depth=MAX_NESTING_DEPTH)


def _classify(command: str, *, depth: int) -> Egress:
    """One level of `classify`. Recurses into strings a shell will interpret."""
    if _builds_a_command(command):
        return Egress.UNCLASSIFIABLE
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return Egress.UNCLASSIFIABLE
    if not tokens:
        # An empty command runs nothing. Non-empty text that tokenises to
        # nothing is not something to reason about confidently.
        return Egress.LOCAL if not command.strip() else Egress.UNCLASSIFIABLE

    # `REMOTE` wins outright wherever it is found, at any depth: it is the one
    # verdict that names a specific consequence rather than an absence of
    # knowledge. `UNCLASSIFIABLE` is remembered and returned only if nothing
    # remote turns up, so "I found ssh" is never downgraded to "I cannot tell".
    verdict = Egress.LOCAL
    wrapped = False
    for index, token in enumerate(tokens):
        base = _basename(token)
        if base in REMOTE_EXEC_BINARIES:
            return Egress.REMOTE
        if base in SHELL_WRAPPERS:
            wrapped = True
        if base in INLINE_CODE_INTERPRETERS and _carries_inline_code(tokens[index + 1 :]):
            verdict = Egress.UNCLASSIFIABLE

    if not wrapped:
        return verdict
    for token in tokens:
        # A token with whitespace in it survived quoting as one word, which is
        # how a whole command travels through `-c`. Tokens without whitespace
        # were already scanned above as the words they are.
        if not any(char.isspace() for char in token):
            continue
        if depth <= 0:
            return Egress.UNCLASSIFIABLE
        inner = _classify(token, depth=depth - 1)
        if inner is Egress.REMOTE:
            return Egress.REMOTE
        if inner is Egress.UNCLASSIFIABLE:
            verdict = Egress.UNCLASSIFIABLE
    return verdict


def _builds_a_command(command: str) -> bool:
    """Does this text assemble a command out of another command's output?"""
    return any(marker in command for marker in COMMAND_SUBSTITUTIONS)


def _carries_inline_code(rest: Sequence[str]) -> bool:
    """Is one of an interpreter's remaining arguments a `run this source` flag?"""
    return any(token in INLINE_CODE_FLAGS for token in rest)


def _basename(token: str) -> str:
    """`/usr/bin/ssh` and `ssh` are the same binary; so is `"ssh"` quoted."""
    stripped = token.strip().strip("\"'")
    if not stripped:
        return ""
    return PurePosixPath(stripped).name
