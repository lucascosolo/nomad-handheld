---
name: low-battery
description: What to stop, what to warn about, and how to answer when the battery is running out.
---
Nomad runs off a small pack. A device that dies mid-sentence has failed at
being a pocket computer, and the operator cannot see the battery unless
something shows it to them.

`read_battery` gives percentage and charging state; `get_context` gives both
plus everything else, so prefer it if you need the moment anyway. **Charging
changes everything** — at 12% and charging there is nothing to manage.

**Below 20%, discharging.** Say it once, plainly, the first time it matters:
"battery 18%". Then behave differently rather than repeating it:

- Answer shorter. Every token is radio and CPU time.
- Stop volunteering. No unprompted briefings, no proactive notifications you
  were going to raise anyway.
- Prefer the screen over `speak` — synthesis and the amplifier cost more than
  drawing does.
- Do not start anything long-running. If they ask for something that will take
  a while, say the cost first and let them decide.

**Below 8%, discharging.** Treat every turn as possibly the last.

- One line per answer.
- Before anything with a deadline attached — a timer, an alarm, a reminder —
  warn that the device may not be awake for it. A reminder that never fires is
  worse than a refusal, because they stopped holding it themselves.
- Anything the operator would lose belongs in `note` or `remember` **now**,
  not at the end of the conversation. Storage survives a shutdown; the
  transcript may not.
- Say once that it is about to go. Do not spend the remaining charge saying so
  repeatedly.

**Never**: silently degrade, ask a `display_choice` question the operator has
to walk back to answer, or claim a charge level you have not read this turn.
Battery numbers go stale fast, so read it rather than reusing one from earlier
in the conversation.
