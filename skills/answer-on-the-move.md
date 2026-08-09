---
name: answer-on-the-move
description: Shape an answer for a 320x240 screen and an operator who is walking: length, screen or voice.
---
The screen is 320x240 with no keyboard. Roughly **eight lines of about forty
characters** are legible at arm's length. That is the whole canvas. An answer
that would scroll is an answer the operator will not read.

**Length, in order of preference:** one clause, one line, three lines, a list
of five. Past that, say the headline and offer the rest — do not paginate
uninvited.

**Choosing the channel.** `get_context` tells you which situation you are in.

- Still, looking at the device → the screen. `display_text` for prose,
  `display_card` for one fact with a label, `display_list` for three to seven
  short items.
- Moving → `speak`. They cannot read while walking. Keep spoken answers to one
  or two sentences: there is no scrollback for audio, and a spoken list past
  three items is gone by the time it ends.
- Night, or ambiguous → screen, quietly. Do not `speak` unprompted at night.
- Something they will act on later — a code, an address, a number → screen even
  if they are moving, and say the short version aloud too. Digits do not
  survive text-to-speech.

**Never use `display_choice` to be conversational.** It is for a real fork with
two to four answers, and it blocks on the operator looking at the device. A
question you could have answered yourself becomes a prompt they have to walk
back to. Do not use it to confirm something they just asked for.

**Formatting that does not survive this screen:** tables, nested bullets, code
fences over four lines, markdown emphasis, em-dash asides, URLs. Give the fact.
If a URL is genuinely the answer, put it in a `note` and say you did.

**Lead with the answer.** "17 minutes" then, if it matters, why. Reasoning
before the conclusion costs the whole screen before the useful part arrives.
