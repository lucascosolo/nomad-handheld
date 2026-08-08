"""Voice drivers, and the one tool built on top of them (D37).

The property under test throughout: `speak` is reachable, `Recorder` is not.
No amount of hardware being attached later should change that without
someone deliberately failing a test to do it.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from nomad.audio import (
    AudioError,
    MockRecorder,
    MockSpeaker,
    MockSynthesizer,
    MockTranscriber,
    Recorder,
    Speaker,
    Synthesizer,
    Transcriber,
    create_recorder_driver,
    create_speaker_driver,
    create_synthesizer_driver,
    create_transcriber_driver,
)
from nomad.core.config import AudioConfig
from nomad.input.actions import ACTION_PUSH_TO_TALK, CORE_ACTIONS
from nomad.input.events import ActionPhase, InputAction, InputSource
from nomad.mcp import voice as voice_module
from nomad.mcp.voice import SpeakParams, SpeakTool, build_voice_tools
from nomad.tools.base import ToolResult

# -- mocks satisfy the protocols structurally --------------------------------


def test_every_mock_satisfies_its_protocol() -> None:
    assert isinstance(MockRecorder(), Recorder)
    assert isinstance(MockSpeaker(), Speaker)
    assert isinstance(MockTranscriber(), Transcriber)
    assert isinstance(MockSynthesizer(), Synthesizer)


# -- Recorder: bounded capture -----------------------------------------------


async def test_mock_recorder_pops_its_script_in_order() -> None:
    recorder = MockRecorder(script=[b"clip-one", b"clip-two"])
    assert await recorder.capture(max_duration_s=5.0) == b"clip-one"
    assert await recorder.capture(max_duration_s=5.0) == b"clip-two"


async def test_mock_recorder_with_no_script_returns_silence() -> None:
    recorder = MockRecorder()
    assert await recorder.capture(max_duration_s=5.0) == b""


async def test_mock_recorder_remembers_the_bound_it_was_asked_for() -> None:
    """A stuck key must not be able to record forever (D37) — this asserts a
    caller cannot invoke `capture` without naming a bound at all, since
    `max_duration_s` is a required keyword on the only method the protocol
    has."""
    recorder = MockRecorder()
    await recorder.capture(max_duration_s=3.0)
    await recorder.capture(max_duration_s=30.0)
    assert recorder.calls == [3.0, 30.0]
    with pytest.raises(TypeError):
        await recorder.capture()  # type: ignore[call-arg]


# -- Speaker / Synthesizer ----------------------------------------------------


async def test_mock_speaker_records_every_clip_played() -> None:
    speaker = MockSpeaker()
    await speaker.play(b"pcm-bytes", sample_rate=22050)
    assert speaker.played == [(b"pcm-bytes", 22050)]


async def test_mock_synthesizer_returns_fixed_audio_and_records_the_text() -> None:
    synthesizer = MockSynthesizer(audio=b"synth-bytes")
    audio = await synthesizer.synthesize("hello there")
    assert audio == b"synth-bytes"
    assert synthesizer.calls == ["hello there"]


async def test_mock_transcriber_returns_fixed_text_and_records_the_audio() -> None:
    transcriber = MockTranscriber(text="hello there")
    text = await transcriber.transcribe(b"some-clip")
    assert text == "hello there"
    assert transcriber.calls == [b"some-clip"]


# -- selection ----------------------------------------------------------------


def test_mock_driver_is_the_default_for_every_category() -> None:
    config = AudioConfig()
    assert isinstance(create_recorder_driver(config), MockRecorder)
    assert isinstance(create_speaker_driver(config), MockSpeaker)
    assert isinstance(create_transcriber_driver(config), MockTranscriber)
    assert isinstance(create_synthesizer_driver(config), MockSynthesizer)


async def test_a_named_real_driver_constructs_but_raises_when_used() -> None:
    """Same shape as `targets/hid.py`: importing and wiring never requires the
    hardware to be attached, but calling into it fails loudly."""
    config = AudioConfig(
        recorder_driver="alsa",
        speaker_driver="alsa",
        transcriber_driver="whisper",
        synthesizer_driver="piper",
    )
    recorder = create_recorder_driver(config)
    speaker = create_speaker_driver(config)
    transcriber = create_transcriber_driver(config)
    synthesizer = create_synthesizer_driver(config)

    with pytest.raises(NotImplementedError):
        await recorder.capture(max_duration_s=5.0)
    with pytest.raises(NotImplementedError):
        await speaker.play(b"x")
    with pytest.raises(NotImplementedError):
        await transcriber.transcribe(b"x")
    with pytest.raises(NotImplementedError):
        await synthesizer.synthesize("x")


def test_an_unknown_driver_name_raises_audio_error() -> None:
    config = AudioConfig(recorder_driver="bluetooth")
    with pytest.raises(AudioError):
        create_recorder_driver(config)


# -- the speak tool -------------------------------------------------------


async def test_speak_tool_synthesizes_then_plays() -> None:
    synthesizer = MockSynthesizer(audio=b"spoken-audio")
    speaker = MockSpeaker()
    tool = SpeakTool(synthesizer, speaker)

    result = await tool.execute(SpeakParams(text="hello operator"), ctx=None)  # type: ignore[arg-type]

    assert isinstance(result, ToolResult)
    assert result.ok
    assert synthesizer.calls == ["hello operator"]
    assert speaker.played == [(b"spoken-audio", 16000)]


def test_speak_tool_spec_is_device_local_and_auto_runnable() -> None:
    """D35/D37: Nomad's own speaker is not the world, so `speak` auto-runs in
    every mode, just like `display_text`."""
    assert SpeakTool.spec.device_local is True
    assert SpeakTool.spec.never_auto is False


def test_build_voice_tools_defaults_to_mocks() -> None:
    tools = build_voice_tools()
    assert len(tools) == 1
    assert tools[0].spec.name == "speak"


# -- D37's load-bearing security property: no listen tool --------------------


def test_no_registered_voice_tool_can_start_capture() -> None:
    """There is no `listen` tool, and there must never be one. A future
    contributor who imports `Recorder` into `mcp/voice.py` fails this test —
    checked structurally (via `ast`, not a raw substring) so the module's own
    docstrings can still discuss the reasoning by name."""
    tree = ast.parse(inspect.getsource(voice_module))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Recorder" not in imported_names and "MockRecorder" not in imported_names, (
        "mcp/voice.py must never import Recorder (D37): recording is "
        "reachable only from the operator-held push_to_talk action, never "
        "from a model-callable tool."
    )

    tools = build_voice_tools()
    names = {tool.spec.name for tool in tools}
    assert "listen" not in names
    assert names == {"speak"}


def test_no_mcp_package_module_can_reach_the_recorder_protocol() -> None:
    """Broader than the single-module check above: nothing under `nomad.mcp`
    may reach `Recorder` at all, wherever a future tool might be added.

    Checking imports alone was not enough — `import nomad.audio.drivers`
    followed by `drivers.Recorder()` is the same capability by a different
    spelling, and it is exactly what someone adding a `listen` tool in a hurry
    would write. So this asserts on every *use* of the name in code position:
    `ast.Name` and `ast.Attribute` nodes only, which means the module
    docstrings can still argue the case by name (a docstring is an
    `ast.Constant`) while no executable reference survives.
    """
    from pathlib import Path

    # `Recorder` is the type; `create_recorder_driver` is how this codebase
    # actually obtains one, and `capture` is the only thing either is for.
    # Banning the type alone left the hole shaped like the composition root's
    # own idiom — a `listen` tool written in a hurry imports the factory, never
    # the protocol, and would have sailed past a name-only check.
    forbidden = {"Recorder", "MockRecorder", "create_recorder_driver", "capture"}
    mcp_dir = Path(voice_module.__file__).parent
    for path in sorted(mcp_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            referenced: str | None = None
            if isinstance(node, ast.Name):
                referenced = node.id
            elif isinstance(node, ast.Attribute):
                referenced = node.attr
            elif isinstance(node, ast.ImportFrom):
                referenced = next(
                    (alias.name for alias in node.names if alias.name in forbidden), None
                )
            assert referenced not in forbidden, (
                f"{path}: reaches the recorder ({referenced!r}) — mcp/ must never "
                "open the microphone (D37). Recording is reachable only from the "
                "operator-held push_to_talk action, by any spelling."
            )


# -- push_to_talk: a logical action, drivable with no physical control -------


def test_push_to_talk_is_a_core_action() -> None:
    assert ACTION_PUSH_TO_TALK in CORE_ACTIONS


def test_push_to_talk_is_drivable_from_the_action_layer_alone() -> None:
    """Joystick and buttons are physically disconnected today (per the task
    brief), so push_to_talk must be constructible and consumable with nothing
    but the logical action vocabulary — no physical control required."""
    press = InputAction(
        action=ACTION_PUSH_TO_TALK,
        phase=ActionPhase.PRESS,
        ts=0.0,
        source=InputSource.BUTTON,
    )
    release = InputAction(
        action=ACTION_PUSH_TO_TALK,
        phase=ActionPhase.RELEASE,
        ts=1.0,
        source=InputSource.BUTTON,
    )
    assert press.action == ACTION_PUSH_TO_TALK
    assert release.phase == ActionPhase.RELEASE
