"""The workspace boundary (D15). Security-critical — read before editing.

A prompt-level instruction not to leave the workspace is not a boundary. A
resolved-path check is. Every filesystem path a tool touches passes through
`Workspace.resolve()` first, and anything landing outside the root raises
`PermissionDenied`.

Three distinct escapes have to be defeated, and they need two different
checks:

1. **`..` traversal** (`"../../etc/passwd"`) — defeated lexically, by
   normalising the joined path without touching the filesystem.
2. **Absolute paths** (`"/etc/passwd"`) — also lexical: an absolute path is
   only accepted if it normalises to somewhere under the root.
3. **Symlink escape** — a symlink *inside* the root pointing outside it. Only
   a full `realpath` resolution catches this, because lexically the path is
   perfectly innocent.

So `resolve()` applies the lexical check **always**, and the realpath check
**unless** `workspace.follow_symlinks_outside_root` is set. That flag
therefore relaxes exactly one of the three escapes and cannot be used to
re-enable `..` traversal or absolute-path escape.

Order matters the other way round too: the realpath check runs on a path that
has already passed the lexical check, so a symlinked directory combined with
`..` cannot be used to walk out through the resolved side.

**Known limit:** this is a check-then-open design, so it is TOCTOU-racy in
principle — an attacker who can create symlinks inside the workspace between
`resolve()` and the target's `open()` could still escape. Closing that needs
`openat`/`O_NOFOLLOW` plumbing in the target layer. It is out of scope for the
MVP because the only writer inside the workspace is the agent itself; revisit
if the workspace ever becomes shared with an untrusted process.
"""

from __future__ import annotations

import os
from pathlib import Path

from nomad.core.errors import PermissionDenied
from nomad.core.logging import get_logger

logger = get_logger(__name__)


class Workspace:
    """A resolved filesystem root that tool paths are confined to (D15)."""

    def __init__(self, root: Path | str, *, follow_symlinks_outside_root: bool = False) -> None:
        # The root itself is fully resolved once, at construction: if the root
        # is reached through a symlink (common on macOS /tmp, and on Pi setups
        # where the workspace lives on an SD card mount), every later
        # comparison must be against the real location or nothing matches.
        self._root = Path(root).expanduser().resolve()
        self._follow_symlinks_outside_root = follow_symlinks_outside_root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def follow_symlinks_outside_root(self) -> bool:
        return self._follow_symlinks_outside_root

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Workspace(root={str(self._root)!r})"

    def ensure_exists(self) -> None:
        """Create the root if missing. Never creates anything outside it."""
        self._root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: Path | str) -> Path:
        """Resolve `path` against the root, or raise `PermissionDenied`.

        Relative paths are joined onto the root; absolute paths are accepted
        only if they land inside it. The returned path is absolute and safe to
        hand to a target.
        """
        raw = str(path)
        if "\x00" in raw:
            raise PermissionDenied(
                "Path contains a null byte", {"path": raw.replace("\x00", "\\0")}
            )

        candidate = Path(raw).expanduser()
        joined = candidate if candidate.is_absolute() else self._root / candidate

        # (1) + (2): lexical normalisation defeats `..` traversal and any
        # absolute path that does not normalise under the root. This does not
        # touch the filesystem, so it cannot be defeated by a race.
        lexical = Path(os.path.normpath(str(joined)))
        if not self._is_inside(lexical):
            raise self._denied(raw, lexical, "resolves outside the workspace root")

        # (3): realpath resolution catches a symlink inside the root pointing
        # out of it. `strict=False` resolves the existing prefix and normalises
        # the rest, so a not-yet-created file still gets its parent directories
        # checked — which is what matters for a write.
        real = lexical.resolve()
        if not self._follow_symlinks_outside_root and not self._is_inside(real):
            raise self._denied(raw, real, "resolves outside the workspace root through a symlink")

        return real if self._is_inside(real) else lexical

    def contains(self, path: Path | str) -> bool:
        """True if `path` resolves inside the root. Never raises."""
        try:
            self.resolve(path)
        except PermissionDenied:
            return False
        return True

    def relative(self, path: Path | str) -> str:
        """Workspace-relative display form of an already-resolved path."""
        resolved = self.resolve(path)
        if resolved == self._root:
            return "."
        return resolved.relative_to(self._root).as_posix()

    def _is_inside(self, path: Path) -> bool:
        return path == self._root or path.is_relative_to(self._root)

    def _denied(self, requested: str, resolved: Path, why: str) -> PermissionDenied:
        logger.warning(
            "Workspace boundary rejected a path",
            extra={"requested": requested, "resolved": str(resolved), "root": str(self._root)},
        )
        return PermissionDenied(
            f"Path {requested!r} {why}",
            {"path": requested, "resolved": str(resolved), "root": str(self._root)},
        )
