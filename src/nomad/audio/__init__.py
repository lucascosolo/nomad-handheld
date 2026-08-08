"""Nomad's voice: driver protocols for capture, playback, and the two speech
models that sit between text and audio (D37).

This package imports `nomad.core` and nothing else — not `protocol`, not
`mcp`, not `tools`. Not `protocol` is the point, not an accident: D37 puts
audio on its own USB Audio Class endpoint precisely so it never contends with
the CDC control link, and a driver here that reached for `nomad.protocol`
would be reopening the channel D37 closed. `tests/test_layering.py` enforces
the absence mechanically.

Mock is the default everywhere (D9): the whole system, including this
package's own tests, runs with no microphone, no speaker, no model, and no
credentials attached.
"""

from __future__ import annotations

from nomad.audio.drivers import (
    MockRecorder,
    MockSpeaker,
    MockSynthesizer,
    MockTranscriber,
    Recorder,
    Speaker,
    Synthesizer,
    Transcriber,
)
from nomad.audio.errors import AudioError
from nomad.audio.selection import (
    create_recorder_driver,
    create_speaker_driver,
    create_synthesizer_driver,
    create_transcriber_driver,
)

__all__ = [
    "AudioError",
    "MockRecorder",
    "MockSpeaker",
    "MockSynthesizer",
    "MockTranscriber",
    "Recorder",
    "Speaker",
    "Synthesizer",
    "Transcriber",
    "create_recorder_driver",
    "create_speaker_driver",
    "create_synthesizer_driver",
    "create_transcriber_driver",
]
