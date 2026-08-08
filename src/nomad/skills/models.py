"""What a skill is, and the two halves it is deliberately split into (D39).

A skill is knowledge, not capability. The split below is the whole design:
`SkillCard` is what every turn pays for, `Skill` is what one turn pays for when
it asks. Keeping them as separate types rather than one type with an
`include_body` flag means the index-rendering path cannot accidentally hold a
body — there is nothing on a card to render.

The parse target is a Markdown file with YAML-ish front matter, chosen because
a self-authoring device (D25) should write skills in the same format a human
would, and because the cheap half of self-upgrading has to stay cheap.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

#: A one-line description is a hard limit, not a style note. The index is read
#: on every turn, so a paragraph here is a paragraph billed a hundred times a
#: day — the exact failure D33 already fixed once for memory.
MAX_DESCRIPTION_CHARS = 200


class SkillCard(BaseModel):
    """The always-in-context half: a name and when to use it. Never a body."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("description")
    @classmethod
    def _one_line(cls, value: str) -> str:
        """Reject a body smuggled in as a description.

        Without this the budget is advisory: nothing stops an authored skill
        putting its whole content in `description` and being loaded on every
        turn forever, which is precisely the cost D39 exists to avoid.
        """
        if "\n" in value:
            raise ValueError("a skill description is one line; put detail in the body")
        return value.strip()

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.replace("-", "").replace("_", "").isalnum():
            raise ValueError("a skill name is alphanumeric with dashes or underscores")
        return cleaned.lower()

    def index_line(self) -> str:
        return f"- {self.name}: {self.description}"


class Skill(BaseModel):
    """A card plus the body that loads on request."""

    card: SkillCard
    body: str

    @property
    def name(self) -> str:
        return self.card.name
