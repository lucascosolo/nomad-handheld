"""Who Nomad is, and what its face can do (D19, D24).

Goal (a) for this device is "a Claude Code session with a Nomad identity". The
backend has always accepted a `system_prompt`; nothing ever passed one, so the
handheld's brain believed it was a terminal on a laptop. This module is the
missing half.

**It appends; it never replaces.** Claude Code's own system prompt is why the
laptop version is good at the work, and the whole point of D19 was to keep that
competence rather than reimplement it. Replacing the prompt with "you are a
friendly pocket robot" would trade the one thing that is hard to rebuild for
the one thing that is easy. So `claude_cli.py` sends this as an *append* to the
`claude_code` preset, and a backend that has no preset (`remote_llm`) uses it
as the whole prompt — the honest asymmetry of D24 showing up again.

**The identity file is data, not code.** It lives at the source root as
`NOMAD.md` so the operator can edit it without a release, and so Nomad can
*read* it — reading its own source is allowed and writing is `never_auto`
(D21/D22), which is exactly the right shape for a self-description.
"""

from __future__ import annotations

from pathlib import Path

from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: Used when `NOMAD.md` is missing or unreadable. Deliberately not empty: a
#: device with no identity is a regression to the bug this module fixes, and it
#: would fail silently. Keep this in sync with `NOMAD.md`'s opening section.
FALLBACK_IDENTITY = """\
You are Nomad, a persistent AI companion running on a handheld device the size
of a Game Boy. You are not a terminal session; you are the device. Your screen
is small and your operator has no keyboard, so answer in a few short lines
unless asked for depth, and lead with the answer rather than the reasoning.
"""


def load_identity(path: Path | None = None) -> str:
    """Read Nomad's identity, falling back to a built-in minimum.

    A missing file is a warning, not an error. The device booting without a
    personality file should still boot — but it must say so, because the
    failure is otherwise invisible until someone notices Nomad answering like
    a laptop.
    """
    resolved = path or default_identity_path()
    try:
        text = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "Identity file unreadable; using the built-in fallback",
            extra={"path": str(resolved), "error": f"{type(exc).__name__}: {exc}"},
        )
        return FALLBACK_IDENTITY
    if not text:
        logger.warning("Identity file is empty; using the built-in fallback",
                       extra={"path": str(resolved)})
        return FALLBACK_IDENTITY
    return text


def default_identity_path() -> Path:
    """`NOMAD.md` at the source root — four levels up from this module."""
    return Path(__file__).resolve().parents[3] / "NOMAD.md"
