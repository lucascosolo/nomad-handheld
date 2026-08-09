"""Knowledge Nomad can consult without paying for it every turn (D39).

Depends on `core` alone. Nothing in the permission path may import this package
— a skill is instructions, never authority, and the broker deciding what the
model may do must never be reading the model's own notes to decide it.
`tests/test_skills.py` enforces that as a structural property.
"""

from __future__ import annotations

from nomad.skills.errors import SkillError
from nomad.skills.library import SkillLibrary, default_seed_root, parse_skill
from nomad.skills.models import MAX_DESCRIPTION_CHARS, Skill, SkillCard

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "Skill",
    "SkillCard",
    "SkillError",
    "SkillLibrary",
    "default_seed_root",
    "parse_skill",
]
