"""The daily utilities: timers, alarms, stopwatch, notes, clocks, conversions.

Small, offline, and deterministic on purpose. These are the seed corpus for the
offline tier: every one of them has to answer with the radio off and no model
loaded, so nothing here reaches the network, imports an SDK, or needs a data
file beyond `zoneinfo`. A utility that quietly needed the cloud would be worse
than no utility, because it would fail exactly when the device is most useful.

The other half of the design is what is *absent*. There is no timer table and
no timer task: a timer is a row in `nomad.notifications`, because "fires once,
durably, when something is finally watching" is precisely what that package
already is (D6). Chunk N landed first for this reason.
"""

from nomad.utilities.errors import UtilityError
from nomad.utilities.notes import Note, NoteStore
from nomad.utilities.stopwatch import Stopwatch, StopwatchStore, format_duration
from nomad.utilities.timers import next_wall_clock, set_alarm, start_timer, timer_due_at
from nomad.utilities.units import Conversion, convert, known_units
from nomad.utilities.worldclock import (
    ZoneTime,
    convert_time,
    resolve_zone,
    time_in,
    zone_difference_minutes,
)

__all__ = [
    "Conversion",
    "Note",
    "NoteStore",
    "Stopwatch",
    "StopwatchStore",
    "UtilityError",
    "ZoneTime",
    "convert",
    "convert_time",
    "format_duration",
    "known_units",
    "next_wall_clock",
    "resolve_zone",
    "set_alarm",
    "start_timer",
    "time_in",
    "timer_due_at",
    "zone_difference_minutes",
]
