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

from nomad.agent.identity import compose_identity
from nomad.app import NomadApp
from nomad.core.config import NomadConfig
from nomad.core.logging import get_logger
from nomad.mcp.skills import LoadSkillParams, LoadSkillTool, build_skill_tools
from nomad.skills import (
    MAX_DESCRIPTION_CHARS,
    Skill,
    SkillCard,
    SkillError,
    SkillLibrary,
    default_seed_root,
    parse_skill,
)
from nomad.targets.local import LocalTarget
from nomad.tools.base import Risk, ToolContext
from nomad.tools.workspace import Workspace

SRC = Path(__file__).resolve().parent.parent / "src" / "nomad"


def _app(tmp_path: Path, **overrides: object) -> NomadApp:
    """A booted composition root, claude_cli so there is a real prompt to read.

    `claude_cli` rather than the `mock` default because a mock backend has no
    system prompt at all, and "the index reaches the model" is not a claim any
    amount of mock can support.
    """
    data: dict[str, object] = {
        "core": {"data_dir": str(tmp_path)},
        "storage": {"path": str(tmp_path / "nomad.db")},
        "workspace": {"root": str(tmp_path / "workspace")},
        "agent": {"backend": "claude_cli"},
        "view": {"enabled": False},
    }
    data.update(overrides)
    return NomadApp(NomadConfig.model_validate(data))


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


def test_nothing_in_the_agent_layer_can_see_the_skills_package() -> None:
    """The index reaches the prompt as text, and text is all `agent/` ever holds.

    `tests/test_layering.py` already omits `skills` from what `agent` may
    import; this pins the *reason*, next to the rule it protects. A session
    that could reach a `SkillLibrary` could hand one to the broker it also
    owns, and `permission_bridge.py` lives in this same package. Threading a
    rendered `str` down from the composition root is what makes "a skill
    cannot reach the permission path" structural rather than remembered.
    """
    for path in sorted((SRC / "agent").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "skills" not in node.module.split("."), f"{path} imports skills (D39)"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "skills" not in alias.name.split("."), f"{path} imports skills (D39)"


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


# -- the seed library ships, because `var/` does not -------------------------


def test_seed_skills_are_committed_and_every_one_of_them_parses() -> None:
    """The defect this chunk fixes: `var/` is gitignored, so nothing shipped.

    With no seeds a fresh checkout has an empty library, which renders no
    index and builds no `load_skill` tool — progressive disclosure over a
    directory the model is never told about. A committed seed root is what
    makes the subsystem exist at all on a device nobody has authored on yet.
    """
    root = default_seed_root()
    assert root.is_dir(), f"the seed skills directory is missing: {root}"
    files = sorted(root.rglob("*.md"))
    assert len(files) >= 4, "a seed library this small is a placeholder"
    for path in files:
        skill = parse_skill(path.read_text(encoding="utf-8"), source=str(path))
        assert skill.card.description
        assert len(skill.body) > 200, f"{path.name} is a stub, not a skill"


def test_the_shipped_index_fits_the_default_budget_without_truncating() -> None:
    """Shipping a seed set that overflows its own budget is a silent regression.

    Truncation is announced, so this would not be invisible — but a device
    that arrives already dropping skills it shipped with is a bug in the seed
    set, not in the budget.
    """
    library = SkillLibrary(index_budget_chars=NomadConfig().skills.index_budget_chars)
    assert library.load_directory(default_seed_root()) == []
    index = library.render_index()
    assert len(index) <= NomadConfig().skills.index_budget_chars
    assert "more skills not shown" not in index, "the shipped seeds overflow the budget"
    for name in library.names():
        assert name in index


def test_every_seed_description_is_one_line_within_the_cap() -> None:
    library = SkillLibrary()
    library.load_directory(default_seed_root())
    for card in library.cards():
        assert "\n" not in card.description
        assert len(card.description) <= MAX_DESCRIPTION_CHARS


# -- both roots, and which one wins ------------------------------------------


def test_an_authored_skill_shadows_a_seed_of_the_same_name(tmp_path: Path) -> None:
    """Disagreeing with a shipped skill must not require editing the source tree.

    Seeds load first and `var/skills` loads over them, so the operator's copy
    wins by name. The reverse order would make the only way to override a seed
    a write into Nomad's own tree, which is `never_auto` on purpose (D22).
    """
    authored = tmp_path / "authored"
    authored.mkdir()
    seed_name = sorted(default_seed_root().glob("*.md"))[0].stem
    (authored / f"{seed_name}.md").write_text(
        f"---\nname: {seed_name}\ndescription: The operator's own version.\n---\nMine.\n"
    )
    app = _app(tmp_path, skills={"root": str(authored)})
    assert app.skills is not None
    assert app.skills.load(seed_name).body == "Mine."
    assert "The operator's own version." in app.skill_index()


def test_an_absent_var_skills_directory_still_gets_the_seeds(tmp_path: Path) -> None:
    """The state every fresh device is in: nothing authored, seeds regardless."""
    app = _app(tmp_path, skills={"root": str(tmp_path / "never-created")})
    assert app.skills is not None
    assert app.skills.names(), "a device with no authored skills got no skills"


def test_seeds_can_be_declined_without_disabling_skills(tmp_path: Path) -> None:
    app = _app(tmp_path, skills={"root": str(tmp_path / "empty"), "seed_root": ""})
    assert app.skills is not None
    assert app.skills.names() == []
    assert app.skill_index() == ""


# -- the index actually reaches the model ------------------------------------


def test_the_index_is_appended_to_the_prompt_after_the_identity() -> None:
    """D39 says the index is in every prompt; ordering is `test_memory`'s rule."""
    prompt = compose_identity(
        "You are Nomad.", briefing="## What you know", skill_index="Skills available:\n- a: b"
    )
    assert prompt.index("You are Nomad.") < prompt.index("## What you know")
    assert prompt.index("## What you know") < prompt.index("Skills available:")


def test_an_absent_section_leaves_no_trace_in_the_prompt() -> None:
    assert compose_identity("You are Nomad.") == "You are Nomad."
    assert compose_identity("You are Nomad.", skill_index="") == "You are Nomad."


def test_a_booted_device_has_the_index_in_its_live_system_prompt(tmp_path: Path) -> None:
    """The end-to-end claim, checked where it is actually made.

    Every link matters and each one was individually green while the chain was
    dead: seeds on disk, a non-empty library, `load_skill` in the toolset the
    backend is handed, and the rendered index inside the string the SDK is
    given as `system_prompt`.
    """
    app = _app(tmp_path)
    assert app.skills is not None and app.skills.names()

    surface = {tool.spec.name for tool in app.agent_tools}
    assert "load_skill" in surface
    assert "load_skill" in {spec.name for spec in app.session._router.specs}  # noqa: SLF001

    prompt = app.session._backend._system_prompt()  # noqa: SLF001 - pinning the contract
    assert prompt["preset"] == "claude_code", "the index must not displace the preset"
    index = app.skill_index()
    assert index and index in prompt["append"]
    for name in app.skills.names():
        assert name in prompt["append"]


def test_a_rolled_backend_keeps_the_index(tmp_path: Path) -> None:
    """Rollover rebuilds the backend; a device must not forget its skills at 168h."""
    app = _app(tmp_path)
    rolled = app.session._new_backend(briefing="", resume_session_id=None)  # noqa: SLF001
    assert app.skill_index() in rolled._system_prompt()["append"]  # noqa: SLF001


def test_a_device_with_skills_disabled_injects_nothing(tmp_path: Path) -> None:
    app = _app(tmp_path, skills={"enabled": False})
    assert app.skills is None
    assert app.skill_index() == ""
    prompt = app.session._backend._system_prompt()  # noqa: SLF001
    assert "Skills available" not in prompt["append"]
    assert "load_skill" not in {tool.spec.name for tool in app.agent_tools}
