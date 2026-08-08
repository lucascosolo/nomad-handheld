"""Driver protocols for Nomad's voice, and their mocks (D9, D37).

Four narrow protocols, one per direction audio can move, because they are
authorized and consumed by entirely different code and conflating them would
force one interface to serve two audiences. `Speaker` and `Synthesizer` back
the `speak` tool (`mcp/voice.py`) — a model-reachable, `device_local` path,
gated like any other tool call. `Recorder` and `Transcriber` back the
operator-held `push_to_talk` action (D13) and are never imported by anything
under `mcp/` — there is deliberately no tool wrapping them, and that omission
is D37's load-bearing security property, not an oversight.

**`Recorder.capture` is one bounded call, not start/stop and not an async
iterator.** `max_duration_s` is a required keyword on the only method this
protocol has, so a caller cannot invoke capture without naming the bound — a
stuck `push_to_talk` key still cannot record forever. The eventual
push-to-talk handler is expected to race this coroutine against the RELEASE
edge (`asyncio.wait_for` or equivalent) and treat whichever comes first as the
end of the clip; a driver still enforces `max_duration_s` on its own, since a
handler bug must not turn into an open microphone.

Every mock below is inspectable, exactly like `MockDisplay`/`MockHid` in
`mcp/hardware.py`: it records what it was asked to do so a test can assert on
it, and it is the config default everywhere (D9) — the full suite runs with
no microphone, no speaker, no model, and no credentials attached.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Recorder(Protocol):
    """Operator-initiated, bounded audio capture. See module docstring."""

    async def capture(self, *, max_duration_s: float) -> bytes: ...


@runtime_checkable
class Speaker(Protocol):
    """Plays PCM audio through Nomad's own transducer."""

    async def play(self, pcm: bytes, *, sample_rate: int = 16000) -> None: ...


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text. Audio bytes in, text out."""

    async def transcribe(self, audio: bytes) -> str: ...


@runtime_checkable
class Synthesizer(Protocol):
    """Text-to-speech. Text in, audio bytes out."""

    async def synthesize(self, text: str) -> bytes: ...


class MockRecorder:
    """Feed it a script of clips ahead of time; `capture` pops the next one.

    `calls` records every `max_duration_s` a caller asked for, so a test can
    assert the bound was honoured without needing a real clock.
    """

    def __init__(self, script: list[bytes] | None = None) -> None:
        self._script: list[bytes] = list(script or [])
        self.calls: list[float] = []

    async def capture(self, *, max_duration_s: float) -> bytes:
        self.calls.append(max_duration_s)
        if self._script:
            return self._script.pop(0)
        return b""


class MockSpeaker:
    """Records every clip it was asked to play, in order."""

    def __init__(self) -> None:
        self.played: list[tuple[bytes, int]] = []

    async def play(self, pcm: bytes, *, sample_rate: int = 16000) -> None:
        self.played.append((pcm, sample_rate))


class MockTranscriber:
    """Returns a fixed transcript and records every clip it was handed."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return self.text


class MockSynthesizer:
    """Returns fixed audio and records every string it was asked to speak."""

    def __init__(self, audio: bytes = b"") -> None:
        self.audio = audio
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return self.audio
