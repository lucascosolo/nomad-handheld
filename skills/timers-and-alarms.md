---
name: timers-and-alarms
description: Pick timer, alarm or reminder, label it so the operator knows what fired, and check what is due.
---
Four tools, and the wrong one produces a device that interrupts at the wrong
moment. They are distinguished by **what the operator said the deadline
relative to**, not by how long it is.

- **`set_timer`** — a duration from now, short, attached to something physical
  happening. Tea, pasta, a parking meter. `minutes` and `seconds` add together.
- **`set_alarm`** — a wall-clock time of day. `hour` and `minute` on a 24-hour
  clock, `timezone` as an IANA zone if they named a place, `repeat: "daily"` or
  `"weekly"` for a standing one. A repeating alarm holds its wall-clock time
  across a daylight-saving change, which is what "7am every day" means.
- **`set_reminder`** — a duration from now for something that is *not*
  physical, or that repeats on an interval rather than a clock time.
  `repeat_minutes` is for "every two hours", which no alarm expresses.
- **`stopwatch`** — measuring elapsed time rather than waiting for it. `start`,
  `lap`, `read`, `stop`, `resume`, `reset`, `list`.

**Label everything as if the operator has forgotten they set it**, because they
will have. The label is what appears when it fires, possibly hours later and
possibly on a dark screen. "oven — take the bread out" is a label; "timer 3" is
not. Put anything longer in `note`, which is shown alongside it.

**Before setting another, look.** `notifications` with `action: "pending"` —
duplicates are the common failure here, because the operator cannot see the
queue and will ask twice. If one already covers it, say so instead of adding a
second.

**Resolving.** When they say something is done or no longer needed, use
`notifications` with `acknowledge` (it happened) or `cancel` (it should not
happen). Both need `notification_id`, so list first. Do not silently leave a
fired notification pending — a queue full of dead rows is one the operator
stops reading.

**Sanity.** Timers cap at 48 hours; anything longer is a slipped decimal or
wants an alarm. "Half an hour" is `minutes: 30`, not `minutes: 0.5`. Repeat the
time back in the confirmation — "alarm at 07:00 daily" — so a misheard number
is caught now rather than at 7am.
