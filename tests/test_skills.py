"""D39: a skill is instructions, never authority.

Two families of test here. The first is ordinary — parsing, budgeting, loading.
The second is structural, and matters more: it asserts that nothing in the
permission path can even *see* this package, so a skill cannot become a way to
widen what the model is allowed to do.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nomad.core.logging import get_logger
from nomad.mcp.skills import LoadSkillParams, LoadSkillTool, build_skill_tools
from nomad.skills import Skill, SkillCard, SkillError, SkillLibrary, parse_skill
from nomad.targets.local import LocalTarget
from nomad.tools.base import Risk, ToolContext
from nomad.tools.workspace import Workspace

SRC = Path(__file__).resolve().parent.parent / "src" / "nomad"


@pytest.fixture
def tool_ctx(tmp_path) -> ToolContext:
    """A minimal call context. `load_skill` touches neither target nor workspace."""
    return ToolContext(
        target=LocalTarget(),
        workspace=Workspace(tmp_path),
        session_id="session-test",
        turn_id=None,
        logger=get_logger("test"),
    )

GOOD = """---
name: morning-briefing
description: How to assemble the operator's morning briefing.
---
Read the calendar, then the weather, then unread notifications.
Keep it under six lines.
"""


def _library(*skills: Skill) -> SkillLibrary:
    library = SkillLibrary()
    for skill in skills:
        library.add(skill)
    return library


# -- parsing -----------------------------------------------------------------


def test_a_skill_parses_into_a_card_and_a_body() -> None:
    skill = parse_skill(GOOD)
    assert skill.name == "morning-briefing"
    assert skill.card.description.startswith("How to assemble")
    assert "Keep it under six lines." in skill.body


@pytest.mark.parametrize(
    ("text", "because"),
    [
        ("name: x\ndescription: y\n", "no opening fence"),
        ("---\nname: x\ndescription: y\n", "front matter never closed"),
        ("---\nname: x\n---\nbody", "no description"),
        ("---\nname: x\ndescription: y\n---\n", "no body"),
        ("---\nnot a pair\n---\nbody", "front-matter line is not key: value"),
    ],
)
def test_a_malformed_skill_is_refused(text: str, because: str) -> None:
    with pytest.raises(SkillError):
        parse_skill(text)


def test_a_description_may_not_smuggle_in_a_body() -> None:
    """Without this the budget is advisory rather than enforced.

    A multi-line description is a body that gets injected on every turn
    forever, which is the exact cost D39 exists to avoid.
    """
    with pytest.raises(ValueError, match="one line"):
        SkillCard(name="x", description="line one\nline two")


def test_a_description_longer_than_the_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        SkillCard(name="x", description="y" * 500)


# -- the index is what every turn pays for -----------------------------------


def test_the_index_carries_names_and_descriptions_but_never_a_body() -> None:
    library = _library(parse_skill(GOOD))
    index = library.render_index()
    assert "morning-briefing" in index
    assert "How to assemble" in index
    assert "Keep it under six lines." not in index, "the body reached the index"


def test_a_card_has_no_body_attribute_to_leak() -> None:
    """Structural, not incidental: the index path holds a type with no body."""
    assert not hasattr(SkillCard(name="x", description="y"), "body")


def test_the_index_fits_its_budget_and_says_when_it_could_not() -> None:
    skills = [
        Skill(
            card=SkillCard(name=f"skill-{i:02d}", description="d" * 60),
            body="body",
        )
        for i in range(20)
    ]
    library = _library(*skills)
    index = library.render_index(budget_chars=300)
    assert len(index) <= 400, "budget was ignored"
    assert "more skills not shown" in index, "truncation was silent"


def test_an_empty_library_renders_nothing_at_all() -> None:
    assert SkillLibrary().render_index() == ""


# -- loading -----------------------------------------------------------------


async def test_loading_a_skill_returns_its_body(tool_ctx) -> None:
    tool = LoadSkillTool(_library(parse_skill(GOOD)))
    result = await tool.execute(LoadSkillParams(name="morning-briefing"), tool_ctx)
    assert result.ok is True
    assert "Read the calendar" in result.content


async def test_an_unknown_skill_fails_without_raising(tool_ctx) -> None:
    tool = LoadSkillTool(_library(parse_skill(GOOD)))
    result = await tool.execute(LoadSkillParams(name="nope"), tool_ctx)
    assert result.ok is False
    assert "morning-briefing" in result.error, "should say what does exist"


def test_a_device_with_no_skills_has_no_skill_tool() -> None:
    """Absent infrastructure means an absent capability, never a broken one."""
    assert build_skill_tools(None) == []
    assert build_skill_tools(SkillLibrary()) == []
    assert [t.spec.name for t in build_skill_tools(_library(parse_skill(GOOD)))] == ["load_skill"]


def test_one_bad_file_does_not_stop_the_library_loading(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text(GOOD)
    (tmp_path / "bad.md").write_text("this is not a skill")
    library = SkillLibrary()
    skipped = library.load_directory(tmp_path)
    assert library.names() == ["morning-briefing"]
    assert skipped == ["bad.md"]


def test_a_missing_skills_directory_is_not_an_error(tmp_path: Path) -> None:
    assert SkillLibrary().load_directory(tmp_path / "absent") == []


# -- D39's load-bearing property: a skill grants nothing ---------------------


def test_loading_a_skill_is_read_only_and_needs_no_permission() -> None:
    spec = LoadSkillTool(_library(parse_skill(GOOD))).spec
    assert spec.risk is Risk.READ_ONLY
    assert spec.permissions == frozenset()
    assert spec.required_capabilities == frozenset()
    assert spec.never_auto is False


def test_there_is_no_tool_that_installs_a_skill() -> None:
    """A model that can write a skill and load it writes its own instructions.

    Installing is an operator-approved act (D26), so the installer is not a
    tool at all — the same shape as D36's authorization prompt and D37's
    missing `listen`.
    """
    names = {tool.spec.name for tool in build_skill_tools(_library(parse_skill(GOOD)))}
    assert names == {"load_skill"}
    for forbidden in ("install_skill", "write_skill", "author_skill", "delete_skill"):
        assert forbidden not in names


def test_nothing_in_the_permission_path_can_see_the_skills_package() -> None:
    """The broker must never read the model's own notes to decide what it may do.

    Checked structurally rather than by convention: `tools/` is the whole
    permission pipeline and `agent/permission_bridge.py` is the door Claude
    Code's calls come through. If either grows an import of `nomad.skills`,
    a skill body has become an input to an authorization decision.
    """
    targets = sorted((SRC / "tools").rglob("*.py")) + [SRC / "agent" / "permission_bridge.py"]
    for path in targets:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "skills" not in node.module.split("."), f"{path} imports skills (D39)"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "skills" not in alias.name.split("."), f"{path} imports skills (D39)"
