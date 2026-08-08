"""Driver selection by config string, mirroring `hardware/selection.py` (D9).

Four categories, each picked independently by its own `driver` string in
`[audio]`, because a device can gain a real speaker long before it has a real
transcriber. `"mock"` is always the default and needs nothing but the
standard library. A real driver name (`"alsa"`, `"whisper"`, `"piper"`)
resolves to a stub whose methods raise `NotImplementedError`, exactly as
`targets/hid.py` does — it constructs cleanly, so importing and wiring this
module never requires a sound device, a model file, or a credential, and the
error only surfaces to whoever actually tries to use the unfinished path.

**Deliberately does not import `nomad.protocol`.** D37 draws audio's route to
the device on a separate USB endpoint from the control link on purpose; a
real driver here is a leaf that opens a sound device directly (ALSA or
equivalent), never something reached through Nomad's wire codec. Layering
enforces the absence of the import; this module is where that absence would
first be tested if someone tried to add it.
"""

from __future__ import annotations

from nomad.audio.drivers import MockRecorder, MockSpeaker, MockSynthesizer, MockTranscriber
from nomad.audio.errors import AudioError
from nomad.core.config import AudioConfig

_UNIMPLEMENTED = "audio driver not implemented; see docs/BUILD_LEDGER.md"


class _UnimplementedRecorder:
    async def capture(self, *, max_duration_s: float) -> bytes:
        raise NotImplementedError(_UNIMPLEMENTED)


class _UnimplementedSpeaker:
    async def play(self, pcm: bytes, *, sample_rate: int = 16000) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)


class _UnimplementedTranscriber:
    async def transcribe(self, audio: bytes) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)


class _UnimplementedSynthesizer:
    async def synthesize(self, text: str) -> bytes:
        raise NotImplementedError(_UNIMPLEMENTED)


def create_recorder_driver(config: AudioConfig) -> object:
    """Build the recorder named by `[audio].recorder_driver`."""
    kind = config.recorder_driver
    if kind == "mock":
        return MockRecorder()
    if kind == "alsa":
        return _UnimplementedRecorder()
    raise AudioError(f"Unknown recorder driver '{kind}'", {"driver": kind})


def create_speaker_driver(config: AudioConfig) -> object:
    """Build the speaker named by `[audio].speaker_driver`."""
    kind = config.speaker_driver
    if kind == "mock":
        return MockSpeaker()
    if kind == "alsa":
        return _UnimplementedSpeaker()
    raise AudioError(f"Unknown speaker driver '{kind}'", {"driver": kind})


def create_transcriber_driver(config: AudioConfig) -> object:
    """Build the transcriber named by `[audio].transcriber_driver`."""
    kind = config.transcriber_driver
    if kind == "mock":
        return MockTranscriber()
    if kind == "whisper":
        return _UnimplementedTranscriber()
    raise AudioError(f"Unknown transcriber driver '{kind}'", {"driver": kind})


def create_synthesizer_driver(config: AudioConfig) -> object:
    """Build the synthesizer named by `[audio].synthesizer_driver`."""
    kind = config.synthesizer_driver
    if kind == "mock":
        return MockSynthesizer()
    if kind == "piper":
        return _UnimplementedSynthesizer()
    raise AudioError(f"Unknown synthesizer driver '{kind}'", {"driver": kind})
