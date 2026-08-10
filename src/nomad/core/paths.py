"""Where Nomad's own code lives on disk.

This is one fact, and it is here rather than in `tools/permissions.py` — where
it started — because two layers now need it and `core` is the only one both
may import. The permission rule that forbids writing to the running source
tree (D21) needs it, and so does the config validation that refuses a scratch
root pointing *at* that tree (D43). A second copy of this logic in `core`
would be two answers to a question that must have exactly one.

`tools.permissions` re-exports `nomad_source_root`, so the D21 rule still
reads as if the fact belongs to it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def nomad_source_root() -> Path:
    """Where Nomad's own running source lives (D21).

    Resolved from this module's location rather than from config, because a
    config value could be edited to point the rule somewhere harmless — and
    the whole purpose of the rule is that the running tree cannot exempt
    itself. If the package sits in a git checkout with a `pyproject.toml` the
    repo root is returned, so `tests/`, `nomad.toml` and `scripts/` are
    protected too; otherwise the installed package directory is the boundary.
    """
    package = Path(__file__).resolve().parent.parent
    repo_root = package.parent.parent
    if (repo_root / "pyproject.toml").is_file() and (repo_root / ".git").exists():
        return repo_root
    return package
