---
name: radio-off
description: Answer with no network: which asks are fully offline, and how to say so when one is not.
---
Nomad is a pocket device and spends real time out of range. `get_context`
reports `network_reachable`; `offline_status` reports what the onboard tier can
answer without the radio at all.

**These need no network, ever.** They are the offline seed corpus, and they
answer identically in a basement:

- `set_timer`, `set_alarm`, `set_reminder`, `stopwatch` — the whole timing set.
- `notifications` — what is pending or recently fired.
- `note` — add, append, edit, search, delete. The operator's own words.
- `remember`, `recall`, `forget` — the memory store is local.
- `convert_units` — length, mass, temperature and more, computed on device.
- `world_clock` — a zone, or two compared. The zone database is on disk.
- `get_context`, `read_battery`, the `display_*` tools, `speak`, `load_skill`.

**These need the network:** anything fetching a page, anything about weather,
news, prices or live anything, and any answer that depends on the model rather
than on the device.

**When the network is down**, do the offline half and name the missing half in
one clause. "Converted: 12km is 7.5 miles. Weather needs a signal." Do not
prefix an answer with an apology, do not offer to retry on a schedule, and do
not guess at the online part — a plausible invented forecast is worse than a
missing one, because the operator will dress for it.

**When it is up but the ask is fully offline**, still prefer the offline tool.
`convert_units` is exact and instant; reasoning about it in prose is neither.
The same goes for arithmetic on times and zones — use `world_clock` rather than
counting hours, because DST is where mental arithmetic goes wrong.

**If the operator asks the same offline question repeatedly**, that is worth a
`remember` — a habit is the evidence that decides whether a phrase is worth
promoting into the onboard router later.
