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

**Everything appended to the preset is composed here, from strings** —
`compose_identity` is the one place that decides what the model's prompt
contains and in what order. Two things join the identity today: the memory
briefing (D33) and the skills index (D39). Both arrive *already rendered*, and
for the index that is a boundary rather than a convenience: `tests/
test_layering.py` does not list `skills` among the packages `agent` may import,
so a `SkillLibrary` cannot be reached from anywhere inside the session — the
composition root renders the index and hands down a `str`. A skill therefore
enters the prompt as text and cannot enter anywhere else, which is the
structural half of "a skill is instructions, never authority".
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
        logger.warning(
            "Identity file is empty; using the built-in fallback", extra={"path": str(resolved)}
        )
        return FALLBACK_IDENTITY
    return text


def compose_identity(identity: str, *, briefing: str = "", skill_index: str = "") -> str:
    """Assemble what gets appended to the backend's prompt, in a fixed order.

    Identity, then what Nomad knows about the operator, then what Nomad knows
    how to *do*. The order is not cosmetic:

    * **Identity first** so the rest is read as this device's context rather
      than as a standalone instruction block. `test_memory.py` pins it.
    * **The skills index last** because it is a menu, not a directive. A list
      of names sitting above the operator's pinned preferences reads as work to
      be done; below them it reads as what is available if needed.

    Empty sections vanish entirely rather than leaving a bare heading — a
    device with no skills must look like a device with no skills, not like one
    whose index failed to render. That is the same absent-capability rule
    `build_skill_tools` follows, applied to the prompt instead of the toolset.

    Pure and deterministic: same inputs, same bytes, in this order. A prompt
    that reshuffles between boots defeats caching and makes behaviour
    irreproducible, which is why `compose_briefing` is pure too.
    """
    sections = [section.strip() for section in (identity, briefing, skill_index)]
    return "\n\n".join(section for section in sections if section)


def default_identity_path() -> Path:
    """`NOMAD.md` at the source root — four levels up from this module."""
    return Path(__file__).resolve().parents[3] / "NOMAD.md"
