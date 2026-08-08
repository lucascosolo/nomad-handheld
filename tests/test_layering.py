"""The dependency direction, enforced mechanically (CLAUDE.md).

```
api  ->  agent  ->  tools  ->  targets/hardware  ->  protocol  ->  core
```

That line is the shape, not the whole graph — `storage`, `memory`, `mcp`,
`input` and `view` all sit somewhere on it too. So the contract below is an
explicit allowlist per package rather than a linear rank: each package names
exactly the sibling packages it may import, and an edge that is not listed is
a violation whether or not it "feels" like it points downhill. Adding an edge
is then a decision someone made on purpose, in a diff, rather than an import
that arrived unnoticed.

Imports are read with `ast`, never by importing the modules. A layering test
that imports its subject cannot distinguish a layering violation from a slow
optional dependency, and would need the Claude SDK installed to run.

**If this fails, the fix is the import, not the table.**
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "nomad"

#: Everything below the composition root. `app.py` may import all of it.
EVERYTHING = {
    "core", "protocol", "storage", "targets", "hardware", "input",
    "memory", "tools", "mcp", "agent", "view",
}

#: Which sibling packages each package may import. Deliberately tight: this is
#: what the tree does today, so any widening is visible.
ALLOWED: dict[str, set[str]] = {
    # Depends on nothing above it. `core` depends on nothing at all.
    "core": set(),
    "protocol": {"core"},
    "storage": {"core"},
    "targets": {"core"},
    # Drivers and the logical input layer: bytes and config, nothing more.
    "hardware": {"core", "protocol"},
    "input": {"core", "protocol"},
    "memory": {"core", "storage"},
    "tools": {"core", "storage", "targets"},
    "mcp": {"core", "storage", "targets", "tools", "memory"},
    "agent": {"core", "storage", "targets", "tools", "memory", "mcp"},
    # Views onto the session (D11). They render the agent's event vocabulary
    # and hold the `DisplayDriver` protocol; they own no state.
    "view": {"core", "agent", "mcp"},
    # Forward-declared: `api` does not exist yet, and when it does it is a
    # view like any other, allowed to see everything and seen by nothing.
    "api": set(EVERYTHING),
    # `app.py` and `__main__.py`. The composition root knows the whole device
    # — that is its job, and it is the only module that may. `app` is in the
    # set because `__main__` imports `app`; both are top-level modules.
    "<root>": EVERYTHING | {"app"},
}

#: Nothing imports `api` (CLAUDE.md). Vacuous today; the point is that it stays
#: true the day the package appears.
NEVER_IMPORTED = "api"


def _package_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else "<root>"


def _nomad_imports(path: Path) -> set[str]:
    """Top-level `nomad.<package>` names imported by one module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import cannot cross a package
                continue
            modules = [node.module or ""]
        for module in modules:
            parts = module.split(".")
            if parts[0] == "nomad" and len(parts) > 1:
                found.add(parts[1])
    return found


def _edges() -> list[tuple[Path, str, str]]:
    """Every (module, from-package, to-package) edge in the tree."""
    edges = []
    for path in sorted(SRC.rglob("*.py")):
        package = _package_of(path)
        for imported in sorted(_nomad_imports(path)):
            if imported != package:
                edges.append((path, package, imported))
    return edges


def test_every_package_is_in_the_table() -> None:
    """A new package must be placed deliberately, not default to unchecked."""
    packages = {_package_of(path) for path in SRC.rglob("*.py")}
    missing = sorted(packages - set(ALLOWED))
    assert missing == [], (
        f"Packages with no declared layer: {missing}. Add them to ALLOWED in this "
        "test, and to the layering section of CLAUDE.md."
    )


def test_no_package_imports_above_its_layer() -> None:
    violations = sorted(
        f"{path.relative_to(SRC)}: {package} -> {imported}"
        for path, package, imported in _edges()
        if imported not in ALLOWED.get(package, set())
    )
    assert violations == [], (
        "Dependencies point one way only (CLAUDE.md). The fix is the import, "
        f"not this test. Violations: {violations}"
    )


def test_nothing_imports_api() -> None:
    """`api` is a view. Anything reaching back into it has inverted the stack."""
    offenders = sorted(
        str(path.relative_to(SRC))
        for path, _package, imported in _edges()
        if imported == NEVER_IMPORTED
    )
    assert offenders == [], f"Nothing may import nomad.api. Imported by: {offenders}"


def test_core_imports_nothing_from_nomad() -> None:
    for path in sorted((SRC / "core").rglob("*.py")):
        assert _nomad_imports(path) <= {"core"}, f"{path.relative_to(SRC)} imports outside core"


def test_protocol_imports_only_core() -> None:
    for path in sorted((SRC / "protocol").rglob("*.py")):
        assert _nomad_imports(path) <= {"core", "protocol"}


def test_input_imports_only_core_and_protocol() -> None:
    """D13: application code consumes logical actions, so `input` never needs
    to know about tools, targets or the agent."""
    for path in sorted((SRC / "input").rglob("*.py")):
        assert _nomad_imports(path) <= {"core", "protocol", "input"}


def test_memory_imports_only_core_and_storage() -> None:
    """D33: memory is an index over storage, not a participant in the loop."""
    for path in sorted((SRC / "memory").rglob("*.py")):
        assert _nomad_imports(path) <= {"core", "storage", "memory"}


def test_hardware_does_not_import_its_own_facade() -> None:
    """Drivers implement `mcp.hardware`'s protocols structurally (D2, D9).

    Importing the facade would put `hardware` above `mcp` and make the drivers
    untestable without the tool layer they exist beneath.
    """
    for path in sorted((SRC / "hardware").rglob("*.py")):
        assert "mcp" not in _nomad_imports(path), f"{path.relative_to(SRC)} imports mcp"


def test_the_composition_root_is_the_only_top_level_module() -> None:
    """Everything else lives in a package with a declared layer."""
    top_level = sorted(p.name for p in SRC.glob("*.py"))
    assert top_level == ["__init__.py", "__main__.py", "app.py"]
