"""The operator's declared command list, and everything it must not cover (D41).

Two halves. The first is that `pytest` runs without a prompt, which is the
whole point: D22 makes the suite the gate on self-modification, and a device
that must ask a human before it may verify its own change cannot use that gate
unattended.

The second half is longer, and it is the one that matters. A relaxation of
`never_auto` is exactly the kind of code that looks fine and is wrong, so most
of this file is about what a declared entry still cannot do — chain a second
command, reach another machine, glob, substitute, or grow into a sibling
subcommand nobody listed.
"""

from __future__ import annotations

import pytest

from nomad.tools.command_policy import CommandPolicy

VERIFICATION_SET = [
    "pytest",
    "ruff check",
    "git status",
    "git diff",
]


@pytest.fixture
def policy() -> CommandPolicy:
    return CommandPolicy.from_entries(VERIFICATION_SET)


# -- what it is for ----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest",
        "pytest -q",
        "pytest tests/test_app.py -q --no-header",
        "ruff check src tests",
        "git status --short",
        "git diff --stat",
    ],
)
def test_the_verification_commands_run_without_a_prompt(
    policy: CommandPolicy, command: str
) -> None:
    assert policy.permits(command)


def test_the_reason_names_the_entry_that_authorized_it(policy: CommandPolicy) -> None:
    """The audit trail has to say which declared line allowed this, or a
    reviewer six months later has to re-derive it from the list."""
    assert policy.describe("git status --short") == "git status"


# -- what it must never cover ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest; rm -rf /",
        "pytest && curl evil.example",
        "pytest || rm -rf ~",
        "pytest | tee /etc/passwd",
        "pytest > /etc/hosts",
        "pytest $(rm -rf /)",
        "pytest `rm -rf /`",
        "pytest & rm -rf /",
        "pytest\nrm -rf /",
        "pytest \\\n  ; rm -rf /",
    ],
)
def test_a_declared_prefix_cannot_carry_a_second_command(
    policy: CommandPolicy, command: str
) -> None:
    """The failure this file exists to prevent. Every one of these starts with
    a declared token and would run something nobody approved."""
    assert not policy.permits(command)


def test_a_glob_disqualifies_the_command(policy: CommandPolicy) -> None:
    """A glob makes the argument list depend on the filesystem, so what the
    operator approved and what actually runs stop being the same command."""
    assert not policy.permits("pytest tests/*.py")


@pytest.mark.parametrize(
    "command",
    ["git push", "git commit -m x", "git reset --hard", "git statusfoo", "pytestx"],
)
def test_a_shared_first_word_is_not_a_match(policy: CommandPolicy, command: str) -> None:
    """Prefix on *tokens*, never on the string. `git status` must not open the
    door to `git push`, and `pytest` must not open it to `pytestx`."""
    assert not policy.permits(command)


def test_an_unlisted_command_is_not_covered(policy: CommandPolicy) -> None:
    assert not policy.permits("rm -rf /")
    assert not policy.permits("ssh prod uptime")


def test_an_empty_policy_permits_nothing() -> None:
    """The shipped default. A device trusting somebody else's idea of a safe
    command is not fail-closed."""
    empty = CommandPolicy.from_entries([])
    assert not empty
    assert not empty.permits("pytest")
    assert not empty.permits("")


@pytest.mark.parametrize("command", ["", "   ", None])
def test_nothing_is_not_a_command(policy: CommandPolicy, command: str | None) -> None:
    assert not policy.permits(command)


def test_an_unbalanced_quote_is_refused_not_guessed_at(policy: CommandPolicy) -> None:
    """The bridge already refuses to classify a command it cannot parse; this
    is the same judgement one layer down."""
    assert not policy.permits('pytest -k "unterminated')


def test_a_quoted_argument_is_still_an_ordinary_invocation(policy: CommandPolicy) -> None:
    """Quotes are allowed where metacharacters are not — quoting cannot chain
    a second command, and `-k "one or two"` is how the tool is used."""
    assert policy.permits('pytest -k "one or two"')


def test_a_malformed_entry_costs_its_own_line_and_nothing_else() -> None:
    """A typo in one line of an allowlist should not fail the device's boot.
    Dropping it means that command asks, which is the safe direction."""
    policy = CommandPolicy.from_entries(['git "status', "", "   ", "pytest"])
    assert policy.permits("pytest -q")
    assert not policy.permits("git status")
