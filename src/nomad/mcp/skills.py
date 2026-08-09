"""`load_skill`, and the one tool this module deliberately does not have (D39).

Loading a skill returns *text*. It grants nothing, reaches nothing, and changes
no permission — every action the returned instructions describe still arrives
back at the broker as an ordinary tool call, gated exactly as if the operator
had asked for it directly (D4, D21). That is why `load_skill` is `READ_ONLY`
with no permissions and no target: like `recall`, it reads Nomad's own store and
transmits nothing, and knowledge the device must ask permission to consult is
knowledge it will not use.

**There is no `install_skill` tool here.** Installing one is an operator-approved
act (D26), and a model-callable installer would close the loop that D39 exists
to keep open: a skill body is untrusted text entering the model's context, so a
model that can both write a skill and load it can write its own instructions and
then follow them. Authoring goes through the settings path with a human in it,
not through this file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nomad.skills.errors import SkillError
from nomad.skills.library import SkillLibrary
from nomad.tools.base import Risk, Tool, ToolContext, ToolResult, ToolSpec


class LoadSkillParams(BaseModel):
    name: str = Field(description="The skill's name, exactly as listed in the skills index.")


class LoadSkillTool:
    """Fetch one skill's full instructions by name."""

    spec = ToolSpec(
        name="load_skill",
        description=(
            "Load the full instructions for one skill, by the name shown in the "
            "skills index. Returns guidance only — it grants no new abilities, "
            "and anything it tells you to do is still an ordinary tool call."
        ),
        params_model=LoadSkillParams,
        # READ_ONLY, no permissions, no target: auto-allowed in every mode
        # including `manual`, for the same reason `recall` is (D35, D39).
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
        device_local=True,
    )

    def __init__(self, library: SkillLibrary) -> None:
        self._library = library

    async def execute(self, params: LoadSkillParams, ctx: ToolContext) -> ToolResult:
        try:
            skill = self._library.load(params.name)
        except SkillError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(skill.body, skill=skill.name, description=skill.card.description)


def build_skill_tools(library: SkillLibrary | None) -> list[Tool]:
    """No library, no tool — a device with no skills should look like one.

    The same rule the memory tools follow: absent infrastructure produces an
    absent capability, never a capability that fails when called.
    """
    if library is None or not library.names():
        return []
    return [LoadSkillTool(library)]
