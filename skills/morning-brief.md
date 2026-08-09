---
name: morning-brief
description: Assemble the start-of-day briefing: context, due notifications, what the operator asked for.
---
The operator has just picked the device up. They want the day, not a report.

1. `get_context` first — local time, battery, network, whether they are moving.
   Everything below depends on it, and it is one cheap read.
2. `notifications` with `action: "pending"` — alarms and reminders that are
   still waiting. This is the only part they cannot reconstruct themselves.
3. `notifications` with `action: "recent"` only if something fired overnight
   while the screen was dark. Skip it otherwise.
4. `recall` with a query like "today" or the current project, once. Do not
   fish; one search or none.

Then say it in **five lines or fewer**, in this order: anything overdue, then
anything due in the next few hours, then one line of context. Lead with the
thing that has a deadline.

Rules that make the difference between useful and ignored:

- Never list what is *not* happening. "No alarms" is one clause, not a line.
- Never read the raw notification rows aloud. "Bins at 8, standup at 10" beats
  three lines of ids and timestamps.
- If the network is unreachable, say so once, at the end. Do not apologise for
  it in every line.
- If they are moving (`motion.moving`), they are walking out the door: cut to
  the two most time-critical items and `speak` it rather than drawing a list.
- Do not `remember` anything during a briefing. A briefing reads; it does not
  write.

If they asked for the briefing at a strange hour — `time_of_day` is `night` —
give the next thing due rather than the day. They are up late, not up early.
