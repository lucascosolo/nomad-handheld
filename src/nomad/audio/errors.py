"""Audio-specific errors (D37).

A separate module rather than an addition to `nomad.core.errors`, mirroring
`hardware/errors.py`: `core` sits below every layer and must not accumulate
error classes for concerns it does not own. `AudioError` still derives from
`NomadError`, so callers only ever need to catch that one base class.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class AudioError(NomadError):
    """A driver name in config does not resolve to any known implementation.

    Raised at selection time for an unrecognised `driver` string — never as a
    bare `KeyError` escaping module scope. An unimplemented-but-known driver
    (e.g. a real recorder before the engine exists) is a different failure:
    it constructs fine and raises `NotImplementedError` from its methods,
    exactly as `targets/hid.py` does, so importing and wiring the audio
    package never requires the hardware or model to be present (D9).
    """
