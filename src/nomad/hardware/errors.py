"""Hardware-specific errors (D9).

A separate module rather than an addition to `nomad.core.errors` on purpose:
`core` sits below every layer and must not accumulate error classes for
concerns it does not own. `HardwareError` still derives from `NomadError`, so
callers only ever need to catch that one base class.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class HardwareError(NomadError):
    """A driver could not be constructed, or could not talk to its device.

    Raised at construction time for a missing optional dependency — never as
    a bare `ImportError` escaping module scope, which would make importing
    `nomad.hardware` itself require hardware that is not attached (D9).
    """
