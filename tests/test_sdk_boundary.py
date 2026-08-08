"""D24's cheapest and most important rule, enforced mechanically.

`claude-agent-sdk` may be imported in exactly one module. The whole value of
the `AgentBackend` interface is that swapping to a local model over Tailscale
means replacing one file — and that stops being true the moment a second
module reaches for an SDK type. A convention nobody checks is not a boundary,
so this test is the boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "nomad"

#: The one module allowed to import the SDK (D24).
SDK_IMPORT_SITE = SRC / "agent" / "backends" / "claude_cli.py"

SDK_MODULES = {"claude_agent_sdk", "claude_code_sdk"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_sdk_is_imported_in_exactly_one_module() -> None:
    offenders = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path != SDK_IMPORT_SITE and _imported_modules(path) & SDK_MODULES
    )
    assert offenders == [], (
        "claude-agent-sdk may only be imported by agent/backends/claude_cli.py (D24). "
        f"Also imported by: {offenders}"
    )


def test_the_designated_import_site_still_exists() -> None:
    """Guards against the rule passing vacuously after a rename."""
    assert SDK_IMPORT_SITE.is_file()
    assert _imported_modules(SDK_IMPORT_SITE) & SDK_MODULES


def test_the_sdk_is_never_imported_at_module_scope() -> None:
    """Even in its one module, the import is deferred into a function.

    `claude-agent-sdk` is an optional extra: `mock` is the default backend and
    the suite must run with no SDK installed (D9). A module-scope import would
    make importing `nomad.agent` fail on a machine that never asked for the
    Claude backend.
    """
    tree = ast.parse(SDK_IMPORT_SITE.read_text(), filename=str(SDK_IMPORT_SITE))
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not (top_level & SDK_MODULES)


def test_no_sdk_type_names_leak_into_nomads_own_vocabulary() -> None:
    """`AgentEvent` and friends must not be aliases for SDK classes (D24)."""
    from nomad.agent.backends.base import AgentEvent

    assert AgentEvent.__module__ == "nomad.agent.backends.base"
    assert "claude" not in str(AgentEvent.model_fields).lower()
