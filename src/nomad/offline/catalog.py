"""The phrase set, in full, in one file somebody can read in a minute.

This is the whole of what the device answers without the model, and it is a
literal list on purpose: the only way a new phrase starts being answered
onboard is a line added here, in a diff, by a person. There is no learned
component, no corpus, and nothing that grows at runtime — chunk O's promotion
path *proposes* additions to this file's spirit as skills for an operator to
install, and never edits it.

Two rules for anything added here, both learned from what the tier is for:

- **The tool must already exist**, and answering must not need a second call.
  The router builds one call; a phrase that means "do these three things" is a
  phrase for the model, which is good at that and cheap to reach.
- **The phrase must be one the operator would only ever mean one way.** "note"
  is not here, because "note" could mean read one, write one, or search them,
  and a device that guesses which is a device that quietly loses a note.

Every entry names a tool that ships in `mcp/hardware.py`, `mcp/context.py` or
`mcp/utilities.py`. If a tool is absent from the registry at runtime the broker
denies the call as unknown and the utterance goes to the model — an intent for a
tool this device does not have degrades to a round trip, never to an error.
"""

from __future__ import annotations

from nomad.offline.intents import SLOT_TEXT, SLOT_VALUE, Intent, SlotKind

#: Every phrase is written the way it is spoken; `normalize()` handles case,
#: apostrophes and punctuation, so "What's my battery?" needs no second entry.
DEFAULT_INTENTS: tuple[Intent, ...] = (
    Intent(
        name="battery",
        tool="read_battery",
        summary="How much charge is left.",
        phrases=(
            "battery",
            "battery level",
            "what's my battery",
            "what is my battery",
            "what's my battery level",
            "how's my battery",
            "how much battery is left",
            "how much battery do i have",
            "am i charging",
        ),
    ),
    Intent(
        name="ambient_context",
        tool="get_context",
        summary="Time, charge, network and motion in one line.",
        phrases=(
            "what time is it",
            "what's the time",
            "what is the time",
            "status",
            "what's my status",
            "how are things",
        ),
    ),
    Intent(
        name="world_clock",
        tool="world_clock",
        summary="The time somewhere else.",
        templates=(
            "what time is it in {}",
            "what's the time in {}",
            "what is the time in {}",
            "time in {}",
        ),
        slot=SlotKind.ZONE,
        params={"zone": SLOT_TEXT},
    ),
    Intent(
        name="set_timer",
        tool="set_timer",
        summary="Start a durable countdown.",
        templates=(
            "set a timer for {}",
            "set timer for {}",
            "timer for {}",
            "wake me in {}",
        ),
        slot=SlotKind.DURATION,
        # `label` is the spoken duration rather than a fixed word, so a device
        # holding three timers can tell the operator which one just fired.
        params={"label": SLOT_TEXT, "seconds": SLOT_VALUE},
    ),
    Intent(
        name="pending_notifications",
        tool="notifications",
        summary="What Nomad is still waiting to say.",
        phrases=(
            "what's pending",
            "what is pending",
            "what's waiting",
            "any reminders",
            "what are my timers",
            "what's my next alarm",
        ),
        params={"action": "pending"},
    ),
    Intent(
        name="start_stopwatch",
        tool="stopwatch",
        summary="Start a stopwatch.",
        phrases=(
            "start a stopwatch",
            "start the stopwatch",
            "start stopwatch",
        ),
        params={"action": "start"},
    ),
)
