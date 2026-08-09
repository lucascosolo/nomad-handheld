---
name: quick-capture
description: Something said in passing: decide between a note, a reminder, and a remembered fact.
---
The operator said something while walking and expects it to be somewhere later.
Three stores exist and picking wrong is how things get lost. Pick by **what has
to happen next**, never by how the sentence was phrased.

**It has a time → `set_reminder` (or `set_alarm`, or `set_timer`).**
Anything with "later", "tomorrow", "in an hour", "before I leave". The device
has to interrupt them for it. See the `timers-and-alarms` skill for which of
the three, and label it so the notification makes sense out of context.

**It is content they will re-read → `note`, `action: "add"`.**
Ideas, lists, a paragraph they dictated, a part number, an address. Notes are
the operator's own words and are kept verbatim. Give a title they would search
for, put the words in `body` unedited, and add one or two `tags`. If it extends
something recent, `note` with `action: "search"` first and `append` instead of
adding a second note on the same subject.

**It is a durable fact about them → `remember`.**
Preferences, constraints, who people are, how they like things done. Short and
in the third person: "prefers metric", not "I prefer metric". Pin only what
should shape *every* future conversation — the pinned set is small and capped,
and a full one refuses the next pin.

**It is none of those → say so and drop it.** Chatter does not need a home.

Ambiguity rules:

- "Remind me to buy milk" is a reminder, not a memory. The milk is transient.
- "I always buy oat milk" is a memory, not a reminder. Nothing has to fire.
- "Here is the wifi password" is a note. It is content, not a preference.
- When it is genuinely both — "remind me Thursday, I always forget bin day" —
  set the reminder *and* remember the pattern. Two calls, not a compromise.

Confirm in one clause naming which store it went to: "noted", "reminder set for
6pm", "remembered". The operator is walking; they need to know it landed, not
where the schema put it.
