"""Nomad's voice, as a tool the model can call (D37).

`speak` is the only voice tool in this module, and that asymmetry is the
point, not a gap to fill later. D37 settles it the same way D35 settled the
screen: Nomad's own speaker is not the world, so speaking through it is an
ordinary `device_local` tool, auto-runnable in every mode exactly like
`display_text`. A microphone is not the same kind of surface — recording
begins only when the operator holds `push_to_talk` (D13, `input/actions.py`)
and never because the model asked — so there is deliberately **no `listen`
tool here, and this module must never import `Recorder`**. `tests/test_audio.py`
enforces that mechanically by scanning this module's source, so a future
addition that wires one in fails a test rather than shipping a way to
eavesdrop.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nomad.audio.drivers import MockSpeaker, MockSynthesizer, Speaker, Synthesizer
from nomad.tools.base import Risk, Tool, ToolContext, ToolResult, ToolSpec


class SpeakParams(BaseModel):
    text: str = Field(description="Text to speak aloud through Nomad's speaker.")


class SpeakTool:
    """Speak text aloud through Nomad's own speaker.

    Mirrors `DisplayTextTool` closely. `device_local=True` because the whole
    effect — synthesis and playback — is confined to Nomad's own transducer,
    so it auto-runs in every mode (D35). `Risk.MUTATING` for the same reason
    `display_text` is mutating rather than read-only: it changes what the
    operator perceives, not because it can reach anywhere else.
    """

    spec = ToolSpec(
        name="speak",
        device_local=True,
        description="Speak a short message aloud through Nomad's built-in speaker.",
        params_model=SpeakParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, synthesizer: Synthesizer, speaker: Speaker) -> None:
        self._synthesizer = synthesizer
        self._speaker = speaker

    async def execute(self, params: SpeakParams, ctx: ToolContext) -> ToolResult:
        audio = await self._synthesizer.synthesize(params.text)
        await self._speaker.play(audio)
        return ToolResult.success(f"spoke {len(params.text)} characters")


def build_voice_tools(
    *,
    synthesizer: Synthesizer | None = None,
    speaker: Speaker | None = None,
) -> list[Tool]:
    """The voice toolset. Mocks by default (D9)."""
    return [SpeakTool(synthesizer or MockSynthesizer(), speaker or MockSpeaker())]
