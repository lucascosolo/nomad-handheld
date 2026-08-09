"""Command shapes the operator declared safe to run unattended (D41).

`Bash` is `never_auto`, and it has to be: a shell is unbounded, and a rule that
reasoned about the *command* would be a denylist, which `tools/builtin/shell.py`
correctly calls security theatre. But the consequence was that Nomad could not
run `pytest` on his own change without a human answering a prompt for it — and
D22 makes the suite the gate on self-modification. A device whose safety model
forbids it from verifying its own work does not become safer; it becomes one
that proposes unverified changes, or one whose operator switches to `auto` and
turns every rule off at once.

So this is the *allowlist* half, and it is deliberately the same shape as D31's
`allowed_network_hosts`: **data the operator wrote down, not a code path.**

What keeps it narrow:

* **A prefix match on parsed tokens, never a substring.** `git status` permits
  `git status --short`; it does not permit `git statusfoo`, and it does not
  permit `git push` because they share a first word.
* **Any shell metacharacter disqualifies the whole command.** `;`, `&`, `|`,
  redirects, substitution, globs, braces, newlines. This is what stops
  `pytest; rm -rf ~` from riding in on a `pytest` entry, and it is checked
  before parsing rather than after, so a construct this module does not
  understand is refused rather than interpreted.
* **It suppresses exactly two rules and no others.** `spec.never_auto`, and
  the "exec outside the workspace" scope rule that a shell always trips
  because `Bash` declares no path params. SSH targets, HID output,
  `DESTRUCTIVE` specs and unapproved network hosts are all still checked, in
  order, before this is consulted — so an entry cannot be used to reach
  another machine even if the operator lists `ssh`.

The list is empty by default. A device that ships with somebody else's idea of
a safe command is not fail-closed.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Characters that give a shell its power to become a different command. Their
#: presence anywhere in the string disqualifies it — no attempt is made to
#: decide whether this particular use was harmless, because that judgement is
#: the thing that has no reliable implementation.
#:
#: `*` and `?` are here despite looking innocent: a glob makes the argument
#: list depend on the filesystem, so what the operator approved and what runs
#: are no longer the same command.
_FORBIDDEN_CHARS = frozenset(";&|<>$`(){}[]*?!#\n\r\\")

#: Backslash is in the set above, which also rules out line continuations.
#: Quotes are *not*: `pytest -k "one or two"` is an ordinary invocation and
#: quoting cannot chain a second command.


@dataclass(frozen=True)
class CommandPolicy:
    """An operator-declared set of command prefixes that need no prompt."""

    #: Each entry is a tuple of tokens. `("git", "status")` matches any command
    #: whose first two tokens are exactly those.
    allowed: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[str] | None) -> CommandPolicy:
        """Build from config. An unparseable or empty entry is dropped.

        Dropped rather than raised on: a typo in one line of an allowlist
        should cost that line, not the device's boot. The effect of dropping
        is that the command asks, which is the safe direction.
        """
        prefixes: list[tuple[str, ...]] = []
        for entry in entries or ():
            tokens = _tokenize(entry)
            if tokens:
                prefixes.append(tokens)
        return cls(allowed=tuple(prefixes))

    def __bool__(self) -> bool:
        return bool(self.allowed)

    def permits(self, command: str | None) -> bool:
        """Whether this exact command is covered by a declared prefix."""
        if not self.allowed or not command:
            return False
        tokens = _tokenize(command)
        if tokens is None:
            return False
        return any(_is_prefix(prefix, tokens) for prefix in self.allowed)

    def describe(self, command: str) -> str:
        """The reason string for an allowed command, naming what matched.

        The audit record has to say *which* declared entry authorized this, or
        a reviewer reading the trail six months later has to re-derive it.
        """
        tokens = _tokenize(command) or ()
        for prefix in self.allowed:
            if _is_prefix(prefix, tokens):
                return " ".join(prefix)
        return ""


def _tokenize(command: str) -> tuple[str, ...] | None:
    """Split a command into tokens, or `None` if it must not be trusted.

    Returns `None` — not an empty tuple — for anything rejected, so a caller
    cannot confuse "nothing to match" with "refused".
    """
    if not command or not command.strip():
        return None
    if _FORBIDDEN_CHARS & set(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        # An unbalanced quote. The bridge already refuses to classify a command
        # it cannot parse; this is the same judgement one layer down.
        return None
    return tuple(tokens) or None


def _is_prefix(prefix: Sequence[str], tokens: Sequence[str]) -> bool:
    return len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == tuple(prefix)
