"""Where a turn came from.

Until now every turn on this device was a turn somebody typed, so "who asked
for this?" had one answer and did not need recording. The moment anything can
begin a turn on its own — a timer, a sensor, Nomad's own self-improvement
loop — that stops being true, and a transcript that cannot tell the operator's
words from the device's own is a transcript you have to trust rather than read.

This enum is deliberately tiny and lives in `core`, which imports nothing. That
placement is the whole point: `agent` must be able to stamp a turn with its
source without importing the package that *produces* unprompted turns, because
`triggers` sits above `agent` and the dependency arrow only points one way.
Putting `TurnSource` next to the trigger that first needed it would have forced
exactly the import that inverts the stack.

Recording it now is cheap; adding it once transcripts, exports and the screen
have all been written against a format without it is not.
"""

from __future__ import annotations

from enum import StrEnum


class TurnSource(StrEnum):
    """What began a turn.

    `USER` is the default everywhere, on purpose: every existing caller means
    it, and a default that has to be passed to stay correct is one that will
    eventually not be.

    `TIMER` and `SENSOR` have no producer yet. They are named anyway because
    the values are what get written to disk — adding a member later costs
    nothing, but discovering that "self" was used as a catch-all for three
    different kinds of unprompted turn costs a migration and a re-read of
    every row.
    """

    USER = "user"
    TIMER = "timer"
    SENSOR = "sensor"
    SELF = "self"
