"""The library, and why the index is budgeted rather than merely short (D39).

`SkillLibrary` holds cards and hands out bodies. The interesting parts are the
two limits, both of which exist because this index is injected into *every*
turn:

**The index has a character budget and truncation is announced.** A library
that quietly drops its last four skills produces a device that has forgotten
how to do things for no reason the operator can see, which is worse than one
that says so. So `render_index` appends a line naming how many were omitted —
the same reasoning that makes a silent cap a bug elsewhere in this project.

**Bodies are never held in the index path.** `cards()` returns `SkillCard`s,
which have no body attribute to accidentally render; the body is read from disk
only when `load()` is called by name. On a device where the model's context is
the scarcest resource, "we only send the summary" has to be a property of the
types, not a discipline someone maintains.

Parsing is deliberately dependency-free — a few `key: value` lines between
`---` fences — because adding a YAML parser to read four fields would be a
required dependency on the critical path of a device that must boot offline.
"""

from __future__ import annotations

from pathlib import Path

from nomad.core.logging import get_logger
from nomad.skills.errors import SkillError
from nomad.skills.models import Skill, SkillCard

logger = get_logger(__name__)

_FENCE = "---"


def default_seed_root() -> Path:
    """`skills/` at the source root — the seed library that ships with Nomad.

    Skills need two homes, not one, and the split is the whole reason this
    function exists.

    `var/skills` (the configured `[skills].root`) is where *authored* skills
    live: `var/` is Nomad's writable data directory and it is gitignored, which
    is exactly right for D25's cheap half of self-upgrading — an operator
    installs one there without a release, and nothing the device writes about
    itself is ever committed. But it also means a fresh checkout, and a freshly
    flashed device, have **no skills at all**, and a library of zero renders no
    index and builds no `load_skill` tool. Progressive disclosure over an empty
    directory is an unreachable directory.

    So the seeds are committed, and they sit at the source root next to
    `NOMAD.md` for the same three reasons that file does (see
    `agent/identity.py`): they are data rather than code, the operator can read
    and edit them without a release, and Nomad may *read* its own source tree
    while writing to it is `never_auto` (D21, D22) — which is precisely the
    posture wanted for instructions the model must not be able to rewrite for
    itself. Putting them under `var/` instead would have made them writable by
    the thing they constrain.

    The composition root loads this first and the configured root second, so a
    skill authored under `var/skills` **shadows** a seed of the same name. That
    ordering is the point: an operator disagreeing with a shipped skill edits
    around it rather than editing the source tree.

    Only `*.md` files that parse as skills are loaded, and anything else in the
    directory is skipped with a warning — so this directory holds skills and
    nothing else, no README among them.

    The path is a guess (four levels up, resolved), correct for a source
    checkout and for `pip install -e .`, which is how this device runs. Where
    it is wrong, `[skills].seed_root` names the real one rather than requiring
    a code change, and the boot log says how many seeds were found so a wrong
    guess is visible rather than silent.
    """
    return Path(__file__).resolve().parents[3] / "skills"


def parse_skill(text: str, *, source: str = "<string>") -> Skill:
    """Parse `---\\nname: x\\ndescription: y\\n---\\nbody` into a `Skill`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise SkillError(f"{source}: a skill starts with a '---' front-matter fence")
    try:
        closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == _FENCE)
    except StopIteration:
        raise SkillError(f"{source}: front matter is never closed with '---'") from None

    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillError(f"{source}: front-matter line is not 'key: value': {line!r}")
        fields[key.strip().lower()] = value.strip()

    missing = {"name", "description"} - fields.keys()
    if missing:
        raise SkillError(f"{source}: front matter is missing {', '.join(sorted(missing))}")

    try:
        card = SkillCard(name=fields["name"], description=fields["description"])
    except ValueError as exc:
        raise SkillError(f"{source}: {exc}") from exc

    body = "\n".join(lines[closing + 1 :]).strip()
    if not body:
        raise SkillError(f"{source}: a skill with no body is just an index entry")
    return Skill(card=card, body=body)


class SkillLibrary:
    """Cards in memory, bodies on disk."""

    def __init__(self, *, index_budget_chars: int = 800) -> None:
        self._skills: dict[str, Skill] = {}
        self._budget = index_budget_chars

    def add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def load_directory(self, root: Path) -> list[str]:
        """Load every `*.md` under `root`. Returns the names of skills skipped.

        A malformed skill is skipped and named, never fatal: one bad file a
        self-authoring device wrote (D25) must not stop the device booting, and
        a skill that fails to parse is missing advice, not a broken capability.
        """
        skipped: list[str] = []
        if not root.is_dir():
            return skipped
        for path in sorted(root.rglob("*.md")):
            try:
                self.add(parse_skill(path.read_text(encoding="utf-8"), source=str(path)))
            except (SkillError, OSError, UnicodeDecodeError) as exc:
                logger.warning(
                    "Skipping unreadable skill",
                    extra={"path": str(path), "error": str(exc)},
                )
                skipped.append(path.name)
        return skipped

    def names(self) -> list[str]:
        return sorted(self._skills)

    def cards(self) -> list[SkillCard]:
        """Cards only. There is no body on these to leak into the index."""
        return [self._skills[name].card for name in self.names()]

    def load(self, name: str) -> Skill:
        try:
            return self._skills[name.strip().lower()]
        except KeyError:
            raise SkillError(
                f"No skill named '{name}'. Known skills: {', '.join(self.names()) or 'none'}"
            ) from None

    def render_index(self, *, budget_chars: int | None = None) -> str:
        """The block injected into the prompt. Fits the budget, says if it did not."""
        budget = self._budget if budget_chars is None else budget_chars
        cards = self.cards()
        if not cards:
            return ""
        header = "Skills available (load one by name for the full instructions):"
        lines: list[str] = []
        used = len(header)
        omitted = 0
        for card in cards:
            line = card.index_line()
            if used + len(line) + 1 > budget:
                omitted += 1
                continue
            lines.append(line)
            used += len(line) + 1
        if omitted:
            # Announced, never silent: a device that has quietly forgotten how
            # to do things looks broken for no visible reason.
            lines.append(f"- (+{omitted} more skills not shown; the index budget is full)")
        return "\n".join([header, *lines])
