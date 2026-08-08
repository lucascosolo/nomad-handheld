"""One error for the whole utility suite, and why it is only one.

These tools are the seed corpus for the offline tier: they must answer with no
network and no model, which means a bad argument has to come back as a *reason*
the caller can act on rather than a traceback nobody is there to read. A single
`UtilityError` carrying structured `details` is enough for that — the caller
that matters is a `ToolResult.failure`, and it renders a sentence either way.
Splitting this into six subclasses would buy discrimination nothing in this
package currently makes a decision on.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class UtilityError(NomadError):
    """A utility was asked for something it cannot compute.

    An unknown unit, a conversion across dimensions, an unknown timezone, a
    note or stopwatch id that does not exist. Always the caller's input, never
    a device fault — nothing in this package touches hardware.
    """
