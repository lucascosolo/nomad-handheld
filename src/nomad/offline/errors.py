"""Errors raised by the offline tier.

There is exactly one, and it is deliberately never raised by the *routing*
path. A router that can raise is a router that can take the device down when
the operator says something odd, and "said something odd" is the normal case
for a phrase set with a few dozen entries in it. Routing answers `None`;
only the evidence ledger and the promotion path — which are administration,
not answering — raise.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class OfflineError(NomadError):
    """A promotion or evidence operation could not be completed."""
