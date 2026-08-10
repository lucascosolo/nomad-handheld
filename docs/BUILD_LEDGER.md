# Build ledger

Chunk-by-chunk record of the build, plus what comes after it. Append outcomes
as they actually happen; **never record a result before the verify command has
been run and its output read.** After a context compaction, trust this file and
`git log` over recollection, and never re-dispatch a chunk marked DONE.

## Continuation record

Read this first after a compaction.

### 2026-08-09 (night) — the glass heals itself, and Nomad can start a turn

**The panel is verified working, by the operator's eyes, and the bug it
exposed was fixed by the thing that was being tested.** The screen came up
saying "waiting for the Pi" and then corrected itself to the status card
within a tick. That sequence *is* the change: the panel missed the first frame
because opening the serial port had just reset it, and `PanelKeeper` repainted
two seconds later. Every earlier symptom of this was reported as "the screen
still says waiting for the Pi" while the Pi's logs said it had drawn.

**Why a tick rather than another one-shot redraw.** `_redraw_after_reboot`
handles a reset it can *see* — `seq` going backwards — and it cannot see this
one: on a fresh connect both sides are at zero, so there is no backwards step
to detect. The settle-delay redraw written for that case is a guess about
timing, and it was wrong. A repaint is idempotent, so the tick needs no guess
and covers the knocked cable and the battery brownout too.

Two things make it affordable rather than wasteful, and neither is optional:
D30 sends structure and not pixels, so a card is tens of bytes; and the
firmware now hashes the payload it rendered and skips an identical one.
Without that second half this loop would strobe the display several times a
second. The hash is over the *payload*, not the envelope — `seq` and `id`
change every frame, so hashing the body would defeat the check entirely.

`DisplayFanout.repaint` replays the fanout's last call to one surface and is
deliberately not a write: it must not disturb the other surfaces, reset their
failure counters, or count as a frame Nomad decided to draw. It raises where
`_fan` swallows, because a repaint has no other surface to fall back on and a
keeper that counted a pulled cable as success would be a lying screen with a
healthy counter.

**The firmware prints its own version on every screen** (`nomad-face 0.3.0`,
top right). Reading the glass is the only way to tell whether a flash took —
an upload can report success while the board comes back on the old image, and
every symptom of that looks exactly like a bug in the new firmware.

**Chunk P, scoped to the half that unblocks self-improvement.** `TurnSource`
(`user`/`timer`/`sensor`/`self`) lives in `nomad.core.turns`, because `agent`
has to stamp a turn with its provenance without importing the package that
produces unprompted ones. Migration 006 adds `turns.source`, defaulting
existing rows to `user`. `resume()` now carries a recovered turn's original
source instead of relabelling it.

`SelfImproveTrigger` is the first thing in this tree that begins a turn nobody
asked for. Four properties do the real work, and all four are tested:

1. **Off by default.** A device that spends tokens because it was switched on
   has made a decision belonging to whoever pays for them.
2. **It skips rather than queues.** `send()` serializes on a lock, so the
   naive call does not fail during a live turn — it lands minutes later as an
   answer to nothing the operator asked. `busy` is checked, then readiness,
   then `busy` again, with no `await` between the last check and `send`.
3. **It will not fire at a backend that cannot authenticate**, via the probe
   `nomad status` already uses, injected as a callable so `triggers` does not
   import a composition-root module.
4. **Consecutive failures stop it loudly.** Counting *exceptions* alone would
   not have worked: `_run_turn` catches a backend failure and returns a
   `FAILED` outcome, so an hourly turn could fail identically forever and look
   healthy. An interrupted turn is the operator taking the device back and
   does not spend the budget.

Layering gained `triggers → {core, agent}`, declared in `test_layering.py` and
in `CLAUDE.md`.

**Deployed and enabled on the device, and the loop was watched rather than
assumed.** `nomad.local.toml` sets `[triggers.self_improve].enabled = true`.
With the interval temporarily dropped to 60s and the service restarted, the
log shows `Skipping self-improvement: the backend is not ready to run a turn`
— so the tick runs on the Pi and the readiness guard does what it was built
for. Interval restored to an hour. Component order on the device is
`... panel_keeper, agent_session, self_improve_trigger`, which is the intended
one: the keeper is retired before the link it writes through, and the trigger
starts after the session it drives.

**The one thing standing between this and a running loop is a login.** The
device reports `auth: expired` — `claude auth status` says `loggedIn: false` —
and that is interactive, so it is the operator's to run on the device:
`ssh nomad@nomad.local` then `claude auth login`. Until then the trigger
correctly refuses to fire, which is why it was built to check.

Suite: **1003 passed** (3:22 on the laptop), ruff clean, re-run by the
coordinating session rather than taken from the subagent's report.

**GitHub is not a usable remote yet.** An `origin` now exists
(`git@github.com:lucascosolo/nomad-handheld.git`) but it holds no refs and a
repository rule declines the push — `remote rejected ... due to repository
rule violations`. `git ship` cannot work against it either, since it fetches
`origin/main` and finds nothing. Deployment therefore still goes over the `pi`
remote, exactly as before. Whoever added the GitHub repo needs to relax the
ruleset (or push an initial `main`) before the normal branch-and-ship flow
works here.

### 2026-08-09 (late) — a finger can answer

**A tap on an option now answers the question.** The prompt has been drawable
and answerable-by-joystick since chunk W; the glass could show "Allow / Deny"
and touching either did nothing, which on a touchscreen device reads as broken.

**The panel does the hit test, not the Pi.** That is the whole design decision.
The Pi is sent structure rather than pixels precisely so it does not keep a
copy of the firmware's fonts, wrapping and line height — and hit-testing raw
coordinates on the Pi would mean keeping exactly that, and being silently wrong
about every option the moment either side's layout changed. So `renderChoice`
records the row rectangles it drew, a tap inside one sends the new
`input.choice {index, option}`, and what crosses the wire is already logical.
D13 forbids synthesizing navigation from touch; this makes that unnecessary
rather than merely forbidden. Raw `input.touch` is still sent as well — an app
drawing its own framebuffer needs the position.

**`option` is a staleness guard, not decoration.** Screens are replaced
asynchronously, so a tap in flight can arrive after the question it was aimed
at is gone. Index 0 is valid on almost any question, and on an authorization
prompt index 0 is usually the one that says yes — so without the label check a
late finger could approve something the operator never read. A mismatch is
dropped and the prompt stays up. It is the wire's version of the token
`ExternalChoicePrompter` gives the browser.

`ChoiceSelection` is a third event shape beside `InputAction` and `TouchEvent`,
not a synthesized `CONFIRM`: it names an option of a *specific* question, and a
consumer that has moved on has to be able to tell.

**Untested on glass.** The Pi is powered down, so the firmware half compiles
nowhere and has never met a finger. The Python half is covered end to end
(framed bytes in at the transport, a `ChoiceResult` out), and the firmware
change is small and local, but *the hit rectangles have not been seen to line
up with the drawn rows.* That is the first thing to check when the device comes
back — with the panel flashed from the Pi, as in chunk W.

Suite: **965 passed**, run as two halves (377 + 588). The laptop could not
finish a single run: three Claude processes were holding ~1.6 GB with 1.2 GB in
swap and load average above 10, and two full-suite attempts were killed at 600s
and 1500s having reached 100% and 50%. Splitting the run is the workaround; the
suite itself takes about four minutes when the machine is quiet.

### 2026-08-09 (late) — the shadowed-tool hole in D21 is closed

**`Skill` now reaches the broker.** It never did: `skills = "all"` becomes an
SDK allow-rule, an allow-rule settles a call *before* `can_use_tool` fires, and
the SDK said so on every connect (`CanUseToolShadowedWarning`). The fix is the
one the previous entry argued for — a `PreToolUse` hook over a `SHADOWED_TOOLS`
set — so `skills = "all"` stays and the laptop parity D19 exists for stays with
it. The other option, narrowing the entry, would have restored the invariant by
removing capability, which is the exact move D21 says was never the thing to do.

Three details that are load-bearing and were easy to get wrong:

1. **The hook answers `"allow"`, never `"defer"`.** Deferring hands the call
   straight back to the allow-rule this hook exists to get in front of — a fix
   that looks right and changes nothing.
2. **An unshadowed tool returns `{}`**, so `Bash` is not judged in two places
   and does not produce two prompts and two audit records for one call.
3. **`Skill` also needed a spec in `claude_tools.py`.** Without it the broker
   denies it as an unknown tool, and the fix meant to preserve the capability
   silently deletes it. It is `READ_ONLY`/`FS_READ` on D39's reasoning: a skill
   is instructions, never authority, and whatever it goes on to *do* arrives at
   the bridge separately.

A payload naming no tool denies, and a bridge that raises denies — fail closed,
tested rather than asserted in prose. `test_every_shadowed_tool_is_classified`
is the tripwire for the next shadowed tool: if the SDK ever warns about one,
that tool is ungated until it is in the set, and the warning is the alarm.

D21 and `ARCHITECTURE.md` now both describe this path; the earlier text claimed
an invariant that had a hole in it.

Suite: **958 passed** (4:19 on the laptop). Not yet deployed — the Pi is
powered down, so four commits are waiting on `git push pi main`.

### 2026-08-09 (evening) — a charter, a command policy, and a mode selector

**`NOMAD.md` now says what the device is for.** It described how to speak and
what was forbidden and never named the goal. The new opening section scopes it
to five jobs — working memory, small work end to end, engineering on its own
codebase, glanceable status, reaching the operator's other machines — and says
what that rules out. The never-do list grew the items that had only ever been
in a conversation: no acting outward unasked, no credential anywhere, no
weakening a guard as a step toward finishing a task, no claiming an unrun
check.

**D41 — the operator's declared command list.** `Bash` stays `never_auto`; a
declared *command* is the operator's standing approval. Token-prefix match,
any shell metacharacter disqualifies the whole string, and it suppresses
exactly two rules (`spec.never_auto` and the workspace-scope rule every shell
trips) while SSH, HID, DESTRUCTIVE and network are all still evaluated first —
asserted, not assumed, in `tests/test_permissions.py`. `nomad.toml` declares
the verification set: `pytest`, `ruff`, read-only `git`. **This is what
unblocks unattended self-improvement**: Nomad can now run the D22 gate on his
own change without a human answering a prompt per command.

**D42 — the mode selector**, on the view rather than the CLI, because a CLI
would write the database and not the running session's in-memory mode. Three
postures (`manual` / `smart` / `auto`), labelled by consequence, validated
server-side against its own list, carrying every write protection the other
endpoints carry. `never_auto` is untouched by it.

Suite: 922 passed before the selector.

**Not yet deployed** — the operator powered the Pi down mid-session. One
command when it is back: `git push pi main && ssh nomad@nomad.local 'cd
~/nomad-handheld && git pull --ff-only && sudo systemctl restart nomad'`.

### 2026-08-09 (later) — a press crosses the cable, and the screen is reachable

**`InputRouter` exists and is wired** (`src/nomad/input/router.py`).
`InputStream.feed_button`/`feed_joystick`/`feed_touch` were written, tested,
and called by nothing outside their own package — the mapper and the prompter
were both green with no join between them, so a device with a panel attached
could draw a question and not receive the answer. The router is a component
(it owns a task for the device's lifetime) and is **the single consumer of
`Link.messages()`**: one inbox queue, so a second iterator steals every other
press. Unknown types are counted and dropped; a malformed payload costs one
message, not the reader task. `tests/test_input_router.py` runs across the
seam — framed bytes in at the transport, an `InputAction` out of the stream.

**The view is remote by default, gated by a token (D40).** `[view].remote`
defaults on, the bind is a wildcard, and `var/view-token` (0600, minted on
first start, survives restarts) is what authorizes every request — reads
included. `ScreenServer` refuses a non-loopback bind without one; the app never
hands it a host without one. The token reaches a browser once as `?token=` and
moves into an `HttpOnly; SameSite=Strict` cookie. `url` never carries it,
`login_url` does, and only `nomad status` prints that. Verified from the laptop
against the running Pi: 401 bare, 200 with the token. The Pi is on the tailnet,
so this is reachable from anywhere the operator is signed in — which is the
point, and also why the token is not optional.

Suite: 870 passed. Deployed to the Pi and the service restarted.

**Still open, in priority order:** the mode selector (manual / smart / auto)
the operator asked for; a reviewable `CommandPolicy` so Nomad can run his own
suite without a prompt per command (`Bash` is `never_auto`, which is what
blocks unattended self-improvement today); and
making a *tap* select a choice option — the firmware owns layout, so it should
report which option was hit over a new message type rather than the Pi
duplicating geometry (D13 forbids synthesizing navigation from touch).

### 2026-08-09 — Nomad is on the Pi, and can be spoken to

**The device is deployed and running its own code.** `~/nomad-handheld` on
`nomad@nomad.local`, its own venv, `pip install -e ".[dev,agent]"`, Claude Code
CLI 2.1.226 at `~/.local/bin/claude`. Pushed over SSH to a bare repo at
`~/nomad-handheld.git` (`git remote add pi ssh://nomad@nomad.local/~/nomad-handheld.git`);
there is still no GitHub remote and none is needed.

**The suite runs on the device in 56 seconds** — faster than the laptop, which
is worth knowing before anything gets scheduled around "the Pi is slow".

What landed with it:

- **`nomad status`, `nomad ask`, `nomad chat`.** Before these the only way a
  human could begin a turn was the browser page, so a headless Pi reached over
  SSH — which is how this device is actually reached — could not be spoken to
  at all. `nomad status` starts nothing and binds nothing, so it is safe to run
  against a Nomad that is already up, and exits non-zero when the backend
  cannot answer.
- **A status card on the screen at boot.** One `StatusReport`, three
  renderings (terminal, card, JSON), so the glass cannot drift from the wire.
- **The backend probe reports evidence, not configuration.** A CLI on `PATH`
  that answers `--version` and a credential the process can see. `backend =
  "claude_cli"` in a file is a claim.

**Three bugs, all found by running things rather than reading them:**

1. **`NOMAD_CONFIG` was a guaranteed startup failure.** It parsed as an
   override of a top-level `config` key and `extra="forbid"` rejected the whole
   file. The variable documented as "point Nomad at another config" had never
   been set by anything, which is the only reason it went unnoticed.
2. **`problems=problems` handed Pydantic a list it copies**, so every
   degradation recorded after construction was invisible in the report — the
   one failure mode that field exists to surface.
3. **`ManualClock.wait_for_sleepers` bounded itself in loop iterations**, with
   a comment claiming that made it machine-independent. It made it worse: 500
   free yields elapse in under a millisecond, so a workload waiting on the
   database (a worker *thread*, not a coroutine) lost the race. It passed on
   the laptop it was written on and failed on the device every single run.
   Now free ticks first, then a real wall-clock deadline.

**The panel was not flashed** when this entry was first written — the ESP32-S3
had appeared on the Pi's USB bus (`303a:1001`, `/dev/ttyACM0`, `nomad` in
`dialout`) but answered no `system.hello`. It is flashed now; see the chunk W
entry below. The firmware was committed under `firmware/` rather than left
untracked, because an interrupted agent's writes on disk are how chunk R was
lost once already.

### Chunk W is DONE — the panel is Nomad's face

Flashed from the Pi itself with `arduino-cli`, settings measured rather than
guessed (esptool: 16MB flash, 8MB octal PSRAM → `FlashSize=16M,PSRAM=opi`, plus
the `CDCOnBoot=cdc,USBMode=hwcdc` the sketch's own header warns about). The
panel now renders Nomad's status card, mirrored to the browser view, with
`driver = "esp32"` and `mirror = ["headless"]`.

Four things worth not relearning, all found by running it:

1. **`pip install -e ".[serial]"` is required on the device** and nothing said
   so until the boot failed. It failed *correctly* — the error names the exact
   command — but the deploy step should install it.
2. **Rotation is 3, not 1.** Rotation 1 is upside down once the board is in its
   case. Which way is up depends on the mounting, and no controller register
   knows that. Touch follows automatically; `setRotation` transforms both.
3. **Opening the serial port resets the board.** DTR/RTS is the ESP32 reset
   line and pyserial asserts it on open, so the panel restarts *because* Nomad
   connected, and comes back after the one-shot boot card was already sent.
   Every layer reported success and the glass said "waiting for the Pi". A
   redraw on D30's reboot detection does not cover it — on a first connect both
   sides are at `seq` zero, so there is no backwards step to detect. Fixed with
   one deliberate second draw after a settle delay, and the reboot redraw kept
   for unplug/replug.
4. **The prompter is now `InputChoicePrompter`.** With the panel primary, an
   authorization question is answered on the glass rather than in a browser
   tab — which is the operator's stated want: Nomad asks yes/no on the
   touchscreen. **Not yet true, and the next real gap:** nothing routes the
   link's `input.touch` messages into `InputStream`, so `feed_*` still has no
   caller outside its own package. Until that is wired the prompt is drawn and
   cannot be answered.

**The operator's mode request, recorded so it is built once.** Three settings
were asked for — manual, "Nomad decides per call", and full auto — and D14
already has exactly those as `manual`, `smart` and `auto` (with `session` as a
fourth). What is missing is not the policy but the *selector*: `set_mode` has
no operator-facing surface. The device runs `auto` today, so nothing is being
babysat, but the picker is real work — a CLI subcommand is easy, and switching
a live session's mode without a restart wants D26's settings service.

**A hole in D21, found by booting the backend rather than reading it.** The
SDK warns on every connect:

```
CanUseToolShadowedWarning: can_use_tool will not be invoked for: Skill.
An allowed_tools entry that allows a whole tool auto-approves it before the
callback is consulted.
```

`[agent.claude_cli].skills = "all"` becomes an allow-rule, and an allow-rule
settles the call *before* `can_use_tool` fires. So CLAUDE.md's "every Claude
Code tool call routes through `can_use_tool` into Nomad's broker" is not true
today for the `Skill` tool. The exposure is bounded — a Claude Code skill is
instructions, and anything it then does (`Bash`, writes, network) still hits
the bridge — but a documented non-negotiable that is quietly false is worse
than a known gap, so it is written down here rather than left in a log line.

Two fixes, and choosing between them is a real trade the operator should make:
a **PreToolUse hook**, which keeps `skills = "all"` and therefore keeps the
parity with laptop Claude Code that D19 exists for; or **narrowing the entry**,
which restores the invariant by removing capability — the exact move D21 says
was never the thing to do. The hook is the right answer and it is its own
chunk, with tests that prove a shadowed call is refused.

**The D22 gate was walked end to end on the device, not assumed.** A worktree
at `~/nomad-scratch/proving-the-gate` with its own venv imported
`~/nomad-scratch/proving-the-gate/src/nomad`, ran 853 tests in 55s, went red
when a line was deliberately broken, and left `~/nomad-handheld` clean
throughout. **The worktree needs its own venv and that is load-bearing:** the
running tree's venv holds an editable install pointing at
`~/nomad-handheld/src`, so testing a worktree with it tests the code you are
running rather than the code you just wrote — green every time, whatever you
changed. A gate that cannot fail is not a gate. The `improving-yourself` skill
now says so.

**And it caught a bad test of its own**, which is the best evidence it works:
`test_an_installed_but_unauthenticated_cli_is_not_ready` went through the full
probe and so depended on whether a real CLI happened to be on `PATH` — green on
the laptop, red on the device. Now asserted against `_auth_source` directly.

**Recorded, and explicitly not to be worked on yet:** the long arc is replacing
external model calls with work done onboard or on the operator's VPS. Chunk O
is the shape it takes. The operator has said plainly that it is a roadmap item
and not somewhere to commit resources now.

**Objective:** the software skeleton for Nomad — a self-upgrading pocket AI
companion. Claude Code is the agent loop *today*, behind a swappable interface,
with a local LLM over Tailscale as the eventual backend (D19, D24).

**Where things stand:** branch `main`, **723 tests passing, ruff clean**,
verified by the coordinating session rather than self-reported. Chunks A–D, G,
G2, E1–E3, M, F1, F2, **V, R, N, U, S and O** are DONE — the whole 2026-08-08
goal is built. What remains is **wiring, not design**: F3 (composition root,
HTTP API, README), then P, I and H.

**Hardware, verified on the device 2026-08-08 — not assumed.** With the Pi
powered up, `lsusb` and ALSA say:

- **The microphone is real and it works.** A C-Media USB capture device
  (`08bb:2902`, ALSA card 1). A two-second capture returned 32000 frames at
  16 kHz with peak 1949 / RMS 281 — signal, not silence. Output is the Pi's own
  3.5 mm jack (card 0).
- **The ESP32-S3 is not on the Pi's USB bus at all.** No Espressif VID, no
  `/dev/ttyACM*`. It is powered and running its *factory demo*, so it is not
  speaking D30's wire format either. USB-C carries data only if the cable, the
  port and the firmware all cooperate; here at least one does not.
- **The Pi has no fans yet.** It reads cool only because it had been off. Do
  not put sustained load on it, and note that D38's governor currently
  arbitrates for contention, not for heat — on an unfanned Pi 4 thermal
  throttling is a second, unmodelled reason for background work to yield.

This is why the whole stack is built against mock drivers (D9): none of the
above blocked a single chunk. What it *does* change is that the real ALSA
`Recorder`/`Speaker` are now worth writing against a device known to work,
where chunk V deliberately left `alsa`/`whisper`/`piper` as named-but-
unimplemented seams.

**Next action: F3, and it matters more than the chunk list suggests.** V, R, N,
U, S and O are all built, tested and *inert* — `app.py` constructs almost none
of them. In value order:

1. **Wire the composition root.** Register the notification queue and STT as
   `INTERACTIVE` and `PromotionAnalyst` as `OPPORTUNISTIC` (D38); build
   `OfflineResponder` with the same broker and executor the agent uses; build
   `build_offline_tools` and `build_skill_tools`.
2. **Inject `SkillLibrary.render_index()` into the prompt** and ship seed skills
   from a committed directory — `var/` is gitignored, so there is nowhere for
   them to live today. Until this lands the skill system is dormant.
3. **An operator UI for promotion accept/reject.** `IntentLedger.accept/reject`
   are deliberately unreachable from `can_use_tool`, so proposals currently
   accumulate to the cap and stop.
4. **The real ALSA `Recorder`/`Speaker`** against card 1 and card 0, now that
   the mic is confirmed working.
5. **Get the panel addressable** — plug it into the Pi, flash D30 firmware.
   Touch is the stated interface and that panel is the only input hardware that
   exists.

**Chunk O — the offline tier — is the operator's call, recorded here so the
constraints are not relearned.** The device is a brick without network today,
and V puts a speech-to-text model on the Pi anyway; once it is there, "what's
my battery", "show me the last thing I saved", "turn the screen down" should
never pay a round trip to a datacentre. Four constraints the brief must carry:

1. **The router fails *toward* Claude, never away from it.** An offline handler
   that mis-reads an intent is worse than a round trip, because the operator
   cannot tell it happened. Exact, high-confidence matches only; anything else
   goes to the model. Never a confidence threshold tuned to catch more.
2. **Deterministic handlers, not a small local model.** D17 already settled that
   the accelerator cannot run an LLM; this is intent matching over a fixed
   phrase set into existing tools, which is why it can be tested.
3. **Promotion is evidence-driven and operator-approved.** The feedback loop is
   the interesting half: count what gets asked, and when a pattern is frequent
   *and* stable, Nomad proposes a new onboard capability and the operator
   accepts it. Never silent self-promotion — a device that quietly changes what
   a phrase means is a device you stop trusting.

   **Corrected after D39 landed:** this originally said "proposes a new onboard
   *tool*", and D39 says prefer a *skill*. The skill form wins, and the reason
   is what the evidence can actually support: the analyst holds a phrase and two
   integers, which can argue "this gets asked often" but cannot argue a risk
   level or a permission decision. `PromotionForm.TOOL` exists in the vocabulary
   and the analyst deliberately cannot reach it.
4. **A promoted capability is still gated.** If one ever takes tool form it
   carries a `ToolSpec`, is gated by the broker, and is audited (D4, D21).
   "Onboard" describes where it runs, never whether it is authorized. An offline
   fast path that skips the broker would undo the entire security layer at
   exactly the moment nobody is watching.

**Machine constraints, learned the hard way:** 2 CPUs, ~3.8 GB RAM, and the
operator has had this laptop freeze. **One subagent at a time** (the operator's
standing rule: one `deep-implementer`, which may itself use up to two lower-tier
agents). Never two heavy commands at once. **Always wrap test runs in
`timeout`** — an unbounded hang costs the whole window:

```
timeout 300 nice -n 19 .venv/bin/python -m pytest <files> -q -p no:cacheprovider
```

No xdist, no type-checker. Beware `pkill -f "python -m pytest"` — the pattern
matches the invoking shell and kills it.

**Three subagents have now reported success with tests red or unwritten.**
Always re-run the verify command yourself before marking a chunk DONE; the last
one left a nonexistent fixture name, three stale snapshot assertions and a lint
error behind a "done". Keep briefs to one or two packages.

**And read `git status` before writing into paths an interrupted agent owned.**
A rejected or interrupted subagent has usually *already written files*. Chunk R
was lost and had to be rebuilt because this session assumed otherwise and wrote
straight over three of its modules.

The `.venv` already has every current dependency plus `nomad` as an editable
install.

**Docs are consolidated to four files** — `CLAUDE.md`, `docs/DECISIONS.md`,
`docs/ARCHITECTURE.md`, `docs/BUILD_LEDGER.md`. `HARDWARE.md`, `PROTOCOL.md`,
`ROADMAP.md` and `start_here.txt` were deleted and folded in; do not recreate
them. More files meant more places for the contract to drift.

## Chunks

| Chunk | Scope | Owns (paths) | Verify | Status |
|---|---|---|---|---|
| A | Foundation: pyproject, gitignore, CLAUDE.md, DECISIONS.md, nomad.toml, ledger | root files, `docs/**`, `src/nomad/core/config.py` | `load_config()` parses `nomad.toml` under `extra="forbid"`; full `pytest` | **DONE** (re-closed post-pivot) |
| B | Remaining docs written against DECISIONS.md | `docs/ARCHITECTURE.md` | consistent with D1–D26 | **DONE** (consolidated) |
| C | Core: errors, logging, config, events, lifecycle, storage | `src/nomad/core/**`, `src/nomad/storage/**`, matching tests | `pytest tests/{test_events,test_config,test_storage,test_lifecycle}.py` | **DONE** |
| D | Security layer: targets, tools, permissions, agent session | `src/nomad/targets/**`, `src/nomad/tools/**`, `src/nomad/agent/**`, matching tests | `pytest tests/{test_targets,test_tools,test_permissions,test_agent_loop,test_workspace}.py` | **DONE** |
| G | **PIVOT (D19–D21, D24):** swappable `AgentBackend`; Claude CLI backend; broker becomes `can_use_tool`; hardware as MCP | `src/nomad/agent/**` (rewrite), `src/nomad/tools/builtin/**` (retire fs tools), `src/nomad/mcp/**`, `tests/test_backend_*.py`, `tests/test_permission_bridge.py`, `tests/test_mcp_hardware.py` | `pytest tests/{test_backend_claude,test_permission_bridge,test_mcp_hardware,test_permissions}.py` | **DONE** |
| G2 | **Review fallout (D27, D28):** egress classification of shell commands; Nomad's identity appended to the preset | `src/nomad/tools/egress.py`, `src/nomad/agent/identity.py`, `NOMAD.md`, `tests/test_egress.py` | `pytest tests/{test_egress,test_permission_bridge,test_backend_claude}.py` | **DONE** |
| E1 | **Protocol (D30):** framing with resync, JSON codec, transports, `Link` with reboot detection | `src/nomad/protocol/**`, `tests/test_protocol.py` | `pytest tests/test_protocol.py` | **DONE** |
| E2 | Hardware drivers: headless + ESP32 display, PiSugar battery, driver selection; display vocabulary (`display_card`/`display_list`/`display_choice`), `get_context` | `src/nomad/hardware/**`, `src/nomad/mcp/hardware.py`, `tests/test_hardware.py` | `pytest tests/{test_hardware,test_mcp_hardware}.py` | **DONE** |
| E3 | Input: logical action layer, deadzone + hysteresis, repeat vs edge-trigger on one stream, extensible action set (D13, D26) | `src/nomad/input/**`, `tests/test_input.py` | `pytest tests/test_input.py` | **DONE** |
| M | **Memory Nomad owns:** durable owner memory + `remember`/`recall`/`forget` MCP tools; session rollover carries it forward | `src/nomad/memory/**`, migration, `tests/test_memory.py` | `pytest tests/test_memory.py` | **DONE** (D33, D34) |
| F1 | Composition root wired; the screen is never blank | `src/nomad/app.py`, `src/nomad/view/renderer.py`, `src/nomad/view/server.py` | `pytest tests/{test_app,test_view}.py` | **DONE** |
| F2 | **One screen, one writer (D36):** `ScreenOwner`/`ScreenView` arbitration; `AuthorizationPrompter` that is deliberately not a tool | `src/nomad/view/screen.py`, `src/nomad/view/authprompt.py`, `tests/test_authprompt.py` | `pytest tests/{test_authprompt,test_view,test_app}.py` | **DONE** |
| V | **Voice:** `Recorder`/`Speaker`/`Transcriber`/`Synthesizer` protocols with mock defaults, `push_to_talk` action, a `speak` tool, no `listen` tool at any price | `src/nomad/audio/**`, `src/nomad/mcp/voice.py`, `tests/test_audio.py` | `pytest tests/test_audio.py` | **DONE** (D37) |
| R | **Resource governance (D38):** two tiers as two *types*; heavy local compute yields to a live turn, the interface is structurally non-preemptible | `src/nomad/resources/**`, `tests/test_resources.py` | `pytest tests/test_resources.py` | **DONE** (D38) |
| N | **Presence:** durable notification queue (never the lossy `EventBus`), ambient-context tool (time, battery, charging, network, motion) | `src/nomad/notifications/**`, `src/nomad/mcp/context.py`, matching tests | `pytest tests/{test_notifications,test_context}.py` | **DONE** |
| U | **Daily utilities:** timers, alarms, stopwatch, notes, world clock, unit conversion — real broker-gated tools, all answerable offline | `src/nomad/utilities/**`, `src/nomad/mcp/utilities.py`, `tests/test_utilities_core.py` | `pytest tests/test_utilities_core.py` | **DONE** |
| S | **Skills (D39):** progressive disclosure — a name and one line stay in context, the body loads on demand; token-efficient by construction | `src/nomad/skills/**`, `src/nomad/mcp/skills.py`, `tests/test_skills.py` | `pytest tests/test_skills.py` | **DONE** (D39) |
| O | **Offline tier + tool evolution:** a deterministic on-device intent router that answers common asks without a round trip, and an evidence-driven path proposing promotions the operator approves | `src/nomad/offline/**`, `src/nomad/mcp/offline.py`, `tests/test_offline.py` | `pytest tests/test_offline.py` | **DONE** |
| P | **Proactivity:** turn provenance (`user`/`timer`/`sensor`/`self`), trigger layer, foreground/background lanes | `src/nomad/triggers/**`, `src/nomad/core/turns.py`, `src/nomad/agent/session.py`, `tests/test_triggers.py` | `pytest tests/test_triggers.py` | **PARTIAL** 2026-08-09 — provenance and `SelfImproveTrigger` done and wired; sensor/timer triggers and the foreground/background lanes are not |
| I | **Self-upgrade (D25, D26, D29):** app registry + manifest + **out-of-process** supervisor; settings service with validation, audit, revert | `src/nomad/apps/**`, `src/nomad/settings/**`, `tests/test_apps.py`, `tests/test_settings.py` | `pytest tests/{test_apps,test_settings}.py` | TODO |
| F3 | Remaining wire-up: HTTP API, README, full layering sweep | `src/nomad/api/**`, `README.md`, `tests/test_api.py`, `tests/test_layering.py` | full `pytest` | TODO |
| H | Delivery (D22, D23): `scripts/setup.sh`, systemd unit, self-update with rollback | `scripts/**`, `src/nomad/selfupdate/**`, `tests/test_selfupdate.py` | `pytest tests/test_selfupdate.py`, shellcheck | TODO |
| T | **Serial transport:** a real `SerialTransport` behind the existing `Transport` protocol, the `pyserial-asyncio` extra, and the config plumbing that makes `[transports.esp32]` mean something | `src/nomad/protocol/transport.py`, `src/nomad/protocol/selection.py`, `src/nomad/core/config.py`, `pyproject.toml`, `tests/test_protocol.py` | `pytest tests/test_protocol.py tests/test_config.py` | **DONE** 2026-08-08 — 820 pass, ruff clean |
| T2 | **Many surfaces, one screen:** `DisplayFanout`, `[display].mirror`, and the `Link` the composition root never built — so `driver = "esp32"` constructs, and an HDMI monitor sees the same state | `src/nomad/hardware/fanout.py`, `src/nomad/hardware/selection.py`, `src/nomad/app.py`, `tests/test_app.py` | `pytest tests/test_app.py tests/test_hardware.py` | **DONE** 2026-08-08 |
| W | **ESP32-S3 firmware:** LovyanGFX app speaking D30's framing over native USB CDC; renders `display.state`, emits touch as `input.touch` | `firmware/**` | flashes; `display.state` round-trips against the Pi | **DONE** 2026-08-09 — flashed from the Pi, `system.hello` round-trips, status card on the glass |
| X | **Audio on the module (deferred by the operator until after the screen works):** composite CDC+UAC2 in the firmware, an ALSA-backed recorder and speaker behind D37's protocols, and a real `Transcriber`. See "The audio question" below — the research is done, the decision is not | `firmware/**`, `src/nomad/audio/**`, `tests/test_audio.py` | `pytest tests/test_audio.py`; an utterance round-trips to text | DEFERRED |

### The screen goal (2026-08-08)

Goal: **the module displays a Nomad status screen.** What the investigation
established, so it is not rediscovered:

- The panel, backlight, SPI and LVGL **all work** — the board arrived running
  stock `lv_demo_widgets` at 100 FPS. No hardware debugging is owed.
- `[transports.esp32].kind` is **decorative**: nothing in `src/` reads
  `config.transports` at all. Setting it to `serial` changes no behaviour.
- There is **no serial transport** — `protocol/transport.py` ships only
  `Loopback` and `Mock`, deliberately (its own docstring says so).
- `create_display_driver` raises unless handed a `Link`, and `app.py` calls it
  without one. So `driver = "esp32"` cannot work today even with firmware.
- **Nomad is not on the Pi at all.** Empty home directory, no service.

Ordering is **T → T2 → W → H**, and T was deliberately first: it is entirely
board-independent, testable against `MockTransport` with no firmware present,
and it is what turns that dead config knob into a real one. W cannot be
verified without T; H cannot deliver either without both.

**T and T2 are done.** Of the five blockers above, the middle three are closed:
`config.transports` now has a consumer (`protocol/selection.py`), a real
`SerialTransport` exists, and `app.py` builds the `Link`, so `driver = "esp32"`
constructs for the first time. An unknown transport `kind` now raises rather
than falling back to a mock — "connected but silent" is the most expensive
failure this device can present.

Multi-monitor came in with T2 and cost almost nothing, because **D36 arbitrates
writers, not surfaces**: `DisplayFanout` sits *underneath* `ScreenOwner` as the
single driver it writes to, so mirroring is invisible to arbitration. It is
cheap only because E2 chose structure over pixels — a `display.state` message is
a few dozen bytes, so each surface renders it to suit its own panel; a
framebuffer vocabulary would mean rasterising per screen. A surface that fails
is logged, counted and skipped; only a total blackout raises. With no mirrors
configured the primary driver is returned **unwrapped**, so the single-screen
path is byte-for-byte unchanged.

What remains for the screen goal is W (firmware) and H (Nomad is still not
deployed to the Pi).

Before W is written, identify the panel and touch controller ICs at runtime
(SPI read-ID; I2C scan) — see the measured table in `ARCHITECTURE.md`. And the
firmware **must** enable USB CDC on boot, or the control link is silent by
default.

Ordering: A → B → C → D → G → G2 → **E** → M → F1 → F2 → **V → R → N → U → S → O**
→ P → I → F3 → H.
**Sequential only** — this laptop cannot afford concurrent subagents. I needs E,
because apps draw to the display and consume logical input. M, N, V and P were
added after the adversarial review below and are not optional polish: without
them the finished device is a Claude Code terminal in a Game Boy shell, and the
shell makes it worse than the laptop.

**V → R → N → U → S → O is the operator's goal of 2026-08-08**, and the order is
dependency, not preference:

- **V first** because a keyboardless device with no voice has no input method.
  Every chunk after it is downstream of the operator being able to speak.
- **R second** because V introduces the first genuinely heavy local workload
  (speech-to-text), and the resource contract has to exist before there are four
  workloads competing rather than one. Retrofitting a governor is how you get a
  governor with exceptions.
- **N → U** because a timer that cannot survive the screen being off is not a
  timer; U's utilities are the first real consumers of N's durable queue.
- **S before O** because O promotes repeated asks into onboard capability, and
  a skill is the cheapest shape for the thing it promotes *into*.
- **O last of the six** — it is the hardest chunk and it wants every other
  surface already present to route into.

### The audio question (2026-08-08) — researched, deferred to chunk X

The operator asked whether onboard STT is the plan, having had bad accuracy from
small local models, and whether mic chunks could instead be handed to the Claude
CLI. Deferred until the screen works; recorded here so none of it is
re-researched.

**The Claude CLI cannot take audio.** Checked against the API reference rather
than answered from memory. Input content blocks are `text`, `image`, and
`document` (PDF/text) — there is no audio block type, and no transcription
endpoint anywhere in the API. The Python SDK's own MCP conversion helpers name
**audio** as an example of an unsupported content type that raises
`UnsupportedMCPValueError`. The phone app's mic button is **client-side**
speech-to-text: the device transcribes and sends *text* upstream. So Nomad has
to produce text before the backend is involved, exactly as D37 assumed. That is
a property of the API, not of `claude-agent-sdk`, so a local-model backend
changes nothing here.

Three places the transcription can happen, and only the third is new:

1. **On the Pi — `whisper.cpp`.** ARM NEON build, `tiny`/`base` quantised, under
   ~200 MB resident. Python Whisper on PyTorch is the thing to avoid: it wants
   1–2 GB and would evict the Node/CLI session. This is the offline floor, and
   its accuracy is the operator's stated complaint.
2. **A cloud STT vendor.** The accuracy answer, but it is microphone audio
   leaving the device to a third party. That is an explicit operator decision,
   never a default, and it wants a `never_auto`-adjacent treatment.
3. **On the VPS.** The operator's point, and it changes the calculus: the VPS is
   not only a publishing target, it is compute Nomad already owns and already
   trusts. A larger Whisper there is the accuracy of (2) without a new vendor,
   and the RAM cost of (1) without spending the Pi's. It needs a reachability
   story — offline means falling back to (1) or to text — and the audio still
   leaves the device, just to somewhere the operator controls.

Push-to-talk makes all three easier than they look: it yields one complete
utterance, so a batch upload is enough and no streaming protocol is owed. And
D37 already made `Transcriber` a protocol with a mock default, so whichever way
this lands it is a leaf swap — it does not block T, T2, W, or H.

## The adversarial review (2026-08-08)

An adversarial critic read the four docs and the source after chunk G landed and
was asked one question: would a stranger holding the finished device think it
was science fiction? **Verdict: no.** The reasoning is worth keeping, because it
is a critique of the *plan*, not of the code, and the code is fine:

> They press a button. Nothing accepts text — D13's logical actions cannot
> express a character, so they thumb an on-screen keyboard on a 320×240 screen
> with a joystick. Then they wait a full Claude Code turn for "what time is it".
> Then the answer arrives as laptop-sized prose in the one display tool that
> exists. Then it goes in a pocket and does nothing, ever, until poked again. It
> never speaks first, has no memory of them it owns, and does not know the time.

The device-specific surface — the only thing a laptop cannot do — was three
tools: `display_text`, `read_battery`, `hid_type_text`.

What that produced, in order of leverage:

1. **Voice (chunk V).** A keyboardless device with no voice has no input method;
   every other improvement is downstream of the operator being able to speak.
   Audio runs on the Pi, **not** over the ESP32 link — 16 kHz mono is 256 kbps
   before framebuffer deltas, and ARCHITECTURE.md's draft protocol currently puts
   `audio.mic_stream` on the same 921600-baud CDC link as `display.draw`. Decide
   the audio path before ratifying that table.
2. **Memory Nomad owns (chunk M, D30).** D19 retired `agent/context.py` because
   Claude Code compacts better. True for a coding session, false for "remembers
   the owner across two years". Today the owner's long-term memory *is* the
   Claude Code transcript — compacted by rules Nomad does not control, stored
   outside Nomad's SQLite, and destroyed by a session-id rotation or a CLI
   upgrade. That was recorded as one decision and it was two.
3. **Identity and a real display vocabulary (D28, done).** Half of this shipped
   in G2. The other half remains: `display_text` is the whole face, and a tool
   schema is the ceiling on what the model can imagine its face doing. Add
   `display_card`, `display_list`, `display_choice` in chunk E.
4. **Proactivity (chunk P).** `AgentSession.send()` is the only way a turn
   begins, so nothing can start one the operator did not type — and if it could,
   the transcript could not tell the difference. Turn provenance is cheap now and
   expensive once the transcript format is set.
5. **Durable notifications (chunk N).** D6 makes `EventBus` drop slow
   subscribers on purpose. Correct for a display, catastrophic for "your build
   failed". Notifications must be a durable row, and nothing currently
   distinguishes the two.
6. **A designed escape hatch for `never_auto` Bash.** `never_auto=True` is
   hard-coded on the `Bash` spec and cannot be relaxed by config at all. On day
   three of real use somebody will delete that line, and they will delete it
   badly. Design the relaxation as a reviewable `CommandPolicy` — argv[0]
   allowlist, no shell metacharacters, cwd inside the workspace — consulted
   inside `tools/permissions.py`. **It must consult D27's egress classifier**,
   or relaxing Bash reopens the SSH hole G2 just closed.
7. **Ambient context (chunk N).** "Understands my needs" is mostly knowing what
   time it is and whether the operator is moving. Nearly free, fully mockable,
   and it is what lets a trigger fire sensibly instead of at random.
8. **Out-of-process apps (D29, decided).** In-process model-authored apps void
   the entire broker. Fixed on paper before chunk I writes a line.
9. **Demoable with no hardware.** The ledger put I after E, which parks the
   flagship demo — "make me a game" — behind unbuilt hardware. Chunk E must ship
   a headless/web display surface so app authoring is exercisable today.
10. **An offline path.** No network means the device is a brick. A small local
    model for wake-word, timers, notes and notification readout is the
    difference between "alive" and "off". Note that deleting `[ai]` in chunk A's
    re-close removed the bad `api_key_env` **and** the model-routing concept;
    routing now has no home in the config model.

Two findings were code, not plan, and are fixed: the SSH `never_auto` bypass
(D27) and the unimplemented identity (D28). Both shipped in G2 with tests.

**Chunk G supersedes part of D.** `agent/loop.py` and `agent/context.py` are
retired by D19; the permission pipeline, targets and all of `core` carry over
intact. Do not treat the retirement as lost work — the expensive half of D was
the broker, and it survives with *more* weight than before (D21).

**V — DONE.** Protocols with inspectable mocks as the default, real engines
named but unimplemented (the `targets/ssh.py` pattern — the seam exists, the
dependency does not). No new dependencies.

Two things worth not relearning:

- **`Recorder.capture(*, max_duration_s)` is one bounded call**, not start/stop
  and not an async iterator. The bound is a required keyword on the protocol's
  only method, so unbounded capture is not expressible, and the driver enforces
  it rather than trusting a push-to-talk handler to win a race against a stuck
  key.
- **The no-`listen` test had a hole and it is now closed.** Scanning imports
  alone missed `import nomad.audio.drivers` → `drivers.Recorder()`, which is the
  same capability by the spelling someone in a hurry would actually write. It
  now rejects any use of the name in code position (`ast.Name`/`ast.Attribute`),
  which leaves docstrings free to argue the case. Confirmed by injecting that
  exact bypass and watching it fail — a security test never run red is not
  known to work.

Verified by the coordinating session: full suite **529 passed**, ruff clean.

Still open for a later chunk: nothing wires audio into `app.py`, and there is
no path yet carrying transcribed speech into `AgentSession` as a turn input.

**R — DONE.** `InteractiveWorkload` and `OpportunisticWorkload` are two
*classes*, not one class with a tier field, so "suspend the screen" is not an
expression this codebase can form — the interactive base has no `run`, no
`suspend` and no task to cancel. Cooperation happens through a `YieldContext`
the governor owns: a well-behaved workload parks at its next `checkpoint()`
without spending a tick of the deadline, one that will not park is cancelled,
and one that swallows cancellation is abandoned after a grace window, because
there is no SIGKILL for a coroutine.

Two bugs found by running it, both worth not reintroducing:

- **Workloads registered before `start()` were never launched.** `_resume_all`
  only looked at `SUSPENDED` entries, so anything still `REGISTERED` sat idle
  forever. Boot now applies the policy directly instead of going through the
  hysteresis path — there is no previous turn at boot whose follow-up we might
  be sitting in.
- **The resume timer was armed asynchronously.** `_schedule_resume` used a bare
  `ensure_future`, which returns before the new task runs a line, so a caller
  that observed `turn_finished` and then advanced time by the resume delay
  moved *past* a timer that did not exist yet. On the device that is background
  work stranded until the next turn ends. Fixed with an `armed` handshake,
  which relies on `Clock.sleep` registering its wakeup before its first
  suspension point — now stated as part of the protocol's contract.

Verified by the coordinating session: `tests/test_resources.py` **21 passed in
2.18s** and green on three consecutive runs (no real sleeps anywhere — every
delay goes through `ManualClock`), full suite **551 passed**, ruff clean.

**A process lesson that cost real time here.** Chunk R was first dispatched to a
subagent which was interrupted partway. Its writes had *already landed on
disk* — `clock.py`, `workload.py`, a 589-line test file, the config and TOML
entries — and the coordinating session then wrote its own `governor.py`,
`errors.py` and `__init__.py` straight over three of them, having assumed an
interrupted agent had changed nothing. **After interrupting or rejecting an
agent, read `git status` before writing anything into the paths it owned.** The
surviving tests turned out to pin the whole API, which is the only reason the
overwrite was recoverable.

**N + U — DONE.** A notification is a durable row, never a published event,
because D6 drops slow subscribers on purpose and Nomad's screen is dark most of
the time: "tea is ready" published to nobody has not been missed, it has ceased
to exist. Delivery is *claimed* rather than fired — a sink that raises leaves
the row pending for the next poll, which is the property the bus cannot offer.

Two behaviours worth keeping, because both are the kind of thing that looks
fine until the device has been off for a while:

- **Repeats are calendar steps in a named zone, not fixed intervals.** `07:00
  daily` computed as +86400s drifts an hour twice a year, and an alarm clock
  that is an hour wrong for six months is not an alarm clock.
- **A device off for a week fires one reminder, not a week of backlog.** A
  repeat advances past `now` and stops. This is what separates a durable queue
  from a replayed log.

Timers and alarms contain no `asyncio.sleep` and no timer table at all — a
timer is just a notification row with a `due_at`, so it survives the process
restart that D34's rollover makes routine rather than exceptional.

Verified by the coordinating session: full suite **634 passed**, ruff clean.

Three things the subagent left broken, fixed here — **it reported before its
last two test files were green**, which is the third time this has happened on
this project:

- `tests/test_context.py` referenced a fixture name that does not exist
  (`rig_free_ctx` for `tool_ctx`), so the module errored on collection.
- `build_hardware_tools()` grew `convert_units` and `world_clock`, breaking
  three snapshot assertions in `tests/test_mcp_hardware.py`. Updated to 10
  rather than reverted: the pure utilities need no driver, store or network,
  so unlike the memory tools they are unconditional. Note the function's name
  is now drifting — it builds hardware, memory *and* utility tools. Rename it
  when something else touches that file; not worth the churn alone.
- One `ruff` import-order error.

**S — DONE.** `SkillCard` and `Skill` are two types rather than one type with
an `include_body` flag, so the index-rendering path holds nothing that *has* a
body to leak. A one-line description is validated, not merely requested — a
multi-line description is a body injected on every turn forever, which is the
exact cost D33 already removed from memory once. Index truncation is announced
rather than silent: a device that has quietly forgotten how to do things looks
broken for no reason the operator can see.

The load-bearing property is enforced structurally: an ast scan asserts that
nothing under `tools/` and not `agent/permission_bridge.py` imports
`nomad.skills`, so a skill body can never become an input to an authorization
decision. Verified by adding that import and watching the test fail. There is
also deliberately no `install_skill` tool — a model that can both write a skill
and load it writes its own instructions and then follows them, so authoring
stays an operator-approved act (D26), the same shape as D36's prompt and D37's
missing `listen`.

Verified: `tests/test_skills.py` **20 passed**, full suite **654 passed**, ruff
clean.

**Not wired, and needed before skills do anything in anger:** nothing injects
`render_index()` into the prompt yet (that belongs with `agent/identity.py` in
F3), and no starter skills ship — `var/` is gitignored, so seed skills need a
committed source directory the setup script copies from. Both are wiring, not
design.

**O — DONE.** Three separable pieces under one constraint: *onboard* describes
where a call runs, never whether it was authorized.

- **Matching is equality, not scoring.** A canonical normalization plus literal
  token templates with one hole, whose slot must parse under a total grammar.
  Ambiguity returns `None` and the model gets it. There is no cutoff and no
  ranking, and an ast scan fails if any identifier in the package contains
  `threshold`/`confidence`/`fuzzy`/`similarity` or if a similarity library is
  imported. **A scored matcher with a "safe" default was considered and
  rejected: the dial is the failure mode, not a mitigation of it.** A cutoff
  exists to be raised the first afternoon a phrase misses, and each turn buys
  recall by spending correctness invisibly. Adding a phrase is a diff someone
  read.
- **The responder owns no execution.** It builds a `ToolRequest` and goes
  `broker.decide` → `broker.authorize` → `executor.run`. A denied or
  `never_auto` intent is simply not handled — it does not prompt, because there
  is one authorization UI on this device (D36). A fast path for `device_local`
  tools "since they auto-approve anyway" was rejected: that is a second
  execution path reachable only when nobody is watching, and `never_auto`,
  disabled tools and unknown tools all live in the path it would skip.
- **Evidence is rows, and proposing is where it stops.** Misses are counted;
  `PromotionAnalyst` is an `OpportunisticWorkload` (D38) that drafts a *skill*
  when a phrase is frequent and stable, and then stops. Accepting records the
  operator's yes and returns the draft — it writes no file, loads no skill and
  changes no phrase. Rejection is permanent.

Layering added `offline → {core, storage, tools, resources}`. Notably **not
`agent`** (the responder takes `session_id` and mode as arguments, so it answers
with the loop wedged or absent) and **not `skills`** (a draft is text until an
operator installs it; importing the library would put a writer next to the
drafts).

Verified by the coordinating session: `tests/test_offline.py` **69 passed**,
full suite **723 passed**, ruff clean. The no-dial guard was independently
re-confirmed by injecting a `confidence_threshold` and watching it fail.

**Inert until wired.** Nothing in `app.py` builds any of it. The composition
root must construct `PromotionAnalyst` and register it opportunistic, register
`build_offline_tools(...)`, and build `OfflineResponder` with the same broker
and executor the agent uses. There is also **no operator UI for accept/reject**:
`IntentLedger.accept/reject` exist and are deliberately unreachable from
`can_use_tool`, so until a view or CLI calls them, proposals accumulate to the
cap and stop. Both belong in F3.

Also note `offline_asks` stores the operator's phrasings as plaintext SQLite —
the same posture as notes and memories, so the known "encryption at rest" gap
now covers one more table.

## The second adversarial review (2026-08-08, after V/R/N/U/S/O)

A fresh agent with **no session history** was given a read-only brief and told
to default to "no". It re-ran the suite itself (`723 passed`, `ruff` clean)
rather than trusting the claim, then went looking for what those numbers do not
cover. **Verdict: no.** Not close.

> A well-architected library with a composition root that boots, prints a URL,
> and then waits for SIGTERM. A stranger picking this up today cannot start a
> turn by any means. Not by voice, not by touch, not by typing.

The five findings, all reproduced by booting the app rather than by reading:

1. **No input path into the agent exists.** `AgentSession.send()` has exactly
   one caller in `src/` — crash recovery replaying an aborted turn.
2. **The input pipeline is severed from the transport.** `InputStream.feed_*`
   has zero callers outside its own package, so with `mode = "manual"` as
   shipped, every tool call needing approval is denied permanently *by
   construction*. The broker fails closed correctly; the device authorizes
   nothing.
3. `display.driver = "esp32"` raises at construction — an option `nomad.toml`
   advertises and the code cannot honour. (Failing loudly is right; advertising
   it is the bug.)
4. **Six tested subsystems are inert.** The live tool surface, enumerated from a
   running app, was 13 tools and none from V/S/O. `build_voice_tools`,
   `build_skill_tools`, `build_offline_tools`: zero callers.
5. **Touch is discarded** at `input/choice.py:134`, and nothing else in `src/`
   reads a `TouchEvent` — half the stated brief.

Smaller, and all real: the D37 audio guard banned the `Recorder` *type* but not
`create_recorder_driver` (**fixed**, bypass-verified); `ARCHITECTURE.md` put the
mic on the ESP32 in one place and the Pi in another (**fixed**); D37 claimed
composite USB firmware in the present tense (**fixed**); `test_layering.py:132`
skips relative imports on the false premise that one "cannot cross a package"
(`from ..agent import x` does — latent, zero relative imports today); and
`egress.py:104` classifies `bash -c "ssh host rm -rf /"` as LOCAL because the
inner command is one shlex token. That last one is harmless **only** because
`Bash` is `never_auto=True`, and this ledger plans to relax exactly that — it is
a trap set for a future change, not a bug today.

For calibration it found the security model sound and said where: no path in
`permission_bridge.py` reaches `allow` by falling off the end, `_target_for`
classifies by declared capability so a HID tool cannot be renamed into a local
call, and the `GrantVault` handoff makes approval and execution one fact rather
than two joined by convention.

**The lesson worth keeping: "documented as inert" is not "addressed."** This
ledger stated plainly that V/R/N/U/S/O were built and unwired, and the reviewer
credited that candour — then counted it as a defect anyway, correctly. Honest
prose about a gap does not close the gap.

## Notes

**A — DONE.** Design settled in conversation before any code was written. Key
departures from the original brief, all recorded in DECISIONS.md: persistent
agent session (D11), target abstraction with SSH/HID stubs (D12), logical input
layer (D13), Claude-Code-shaped permission modes (D14), and an explicit note
that an Edge TPU cannot replace the cloud model (D17).

**A — re-closed after the pivot.** The foundation files were written against
D1–D18 and went stale the moment D19–D26 landed, so chunk A was DONE against a
contract that no longer existed. Brought current before G starts, because G
writes the backend that reads this config:

- `pyproject.toml`: `anthropic` extra replaced by `agent = ["claude-agent-sdk>=0.2.132"]`.
  Optional, because `mock` is the default backend and the suite must run with no
  CLI and no credentials (D9, D24).
- `nomad.toml` + `core/config.py`: added `[agent].backend` (`mock` default),
  `[agent.claude_cli]` (cli path, pinned version, OAuth **env var name** — never
  the token), `[agent.remote_llm]` (empty, tailnet placeholder), `[apps]` (D25),
  `[settings]` (D26), `[input].extra_actions` (D26 — `ASSISTANT` is registered
  config, not a special case).
- **`[ai]` removed entirely.** `ai.provider` and `agent.backend` were two names
  for one choice, and `ai.cloud.api_key_env = "ANTHROPIC_API_KEY"` pointed at the
  exact variable D20 requires to be *unset*. Leaving it would have been a config
  surface that bills per token by following its own documentation. D17's routing
  question is deferred until a second backend exists to route between.

`NomadConfig` is `extra="forbid"`, so the file and the model cannot drift apart
silently — a stale key is a startup failure, not a silent default. Verified this
session: `load_config()` returns the new fields, full suite **183 passed in
62.74s**, ruff clean. No code referenced the removed `ai` config (grepped).

**B — DONE, then consolidated.** Four docs were written against D1–D18. Three
were deleted and folded into `ARCHITECTURE.md` (hardware, protocol) and this
file (roadmap, gaps), because six documents meant six places for the contract to
drift and `ARCHITECTURE.md` had already drifted — it described
`Tool.run(grant, target, params)` and a broker living in `agent/`, neither of
which was ever true. It is now written from the actual source signatures, with a
note saying so.

The **wire format remains a draft**, not a decision. Ratify it into DECISIONS.md
once chunk E implements it — do not freeze a wire format on paper before code
exists.

**C — DONE.** `NomadError` tree, structured console/JSON logging, layered
TOML→Pydantic config with injectable env, async `EventBus` with bounded
per-subscriber queues that drop rather than apply backpressure (the load-bearing
D6 property), `Component`/`ComponentRegistry` with reverse-order shutdown and
start-failure rollback, SQLite on a single worker thread with numbered
migrations. Migration 001 also creates the `grants` and `pending_authorizations`
tables chunk D uses.

Verified by the coordinating session, not self-reported: `pytest` on the four
core test files → **30 passed in 5.60s**; ruff clean. (The subagent's own report
was truncated before it stated a result, so the run was repeated here.)

**D — DONE.** Targets with `LocalTarget` real and SSH/HID raising
`NotImplementedError`; capability checks reject a file tool aimed at HID before
permission logic runs; workspace boundary defeats `..`, absolute paths and
symlink escape; four-stage pipeline where `ToolExecutor.run(grant, request)`
recomputes scope from the request so an approved workspace write cannot be
replayed against `/etc`; `never_auto` enforced both before mode branching and
again in `mint_grant()`. Verified: full suite **183 passed in 33.09s**, ruff
clean.

Open items raised by chunk D, still unresolved:

- Config gaps handled with constructor defaults rather than TOML keys:
  `agent.authorization_timeout_seconds`, `agent.grant_ttl_seconds`.
  `agent.context_window` is mooted — Claude Code owns the context window now.
- `Workspace.resolve` is check-then-open, so TOCTOU-racy if an untrusted process
  ever gains write access inside the workspace. Documented in-module.
- Judgement call to review: switching mode *to* `manual` revokes standing
  session grants by default. D14 does not specify this.
- D14-vs-D15 tension on paths outside the workspace is now settled by D21: the
  workspace root is a policy line, not a hard wall, and `never_auto` carries the
  weight.

**PIVOT — after chunk D, before chunk E.** User direction changed the target:
run on the Max-plan subscription rather than per-token API billing, match Claude
Code's coding competence, and eventually self-modify. All three resolve to
running Claude Code headless as the loop. Recorded as D19–D23. Verified against
CLI 2.1.224 *before* designing: `--bare` would have broken this outright (it
never reads OAuth), `setup-token` and `CLAUDE_CODE_OAUTH_TOKEN` are real,
`claude-agent-sdk` 0.2.132 is on PyPI.

Then corrected once more before any code was written: "swap to a local LLM over
Tailscale" means Claude Code cannot *be* the loop, it has to be *a* loop behind
an interface (D24), and "self-upgrading" means authoring apps and changing
settings (D25, D26), not rewriting core. Cheap to fix on paper; expensive once
the SDK is threaded through the codebase.

## What comes after the skeleton

Ordered by dependency, not calendar. Deliberately no dates.

| Phase | Depends on | Content |
|---|---|---|
| Real ESP32-S3 transport + display | chunk E's `protocol` | `serial` transport kind (pyserial-asyncio), real `esp32` display driver, reconnect/resync exercised against a real MCU reboot rather than the mock |
| Real input hardware | above + D13 | Firmware reporting touch/joystick/button; deadzone and repeat timing validated at real event rates |
| UI shell with controller navigation | real display + input | Navigable UI driven entirely by the logical action stream; on-device view of session state and pending grants |
| Apps and games in anger | UI shell | D25's authoring path used for real; games need hold-to-move `REPEAT` where menus need edge-triggering, both on the same stream without forking it |
| SSH target | chunk G's tool path | Real implementation behind the `SSH` stub with its own remote identity. Because tools were written against `Target` from day one, this should not touch the agent loop — that is the whole bet of D12 |
| HID target | above + RP2040 link | Real `rp2040` driver sending `hid.key`/`hid.pointer`; `never_auto` exercised against a real external host |
| Camera / sensors | D9 driver pattern | `picamera2` driver, IMU/ToF as selected, with tools declared at appropriate risk — no special-casing capture in the broker |
| `remote_llm` backend | D24 | A model on the tailnet. Note the asymmetry: it brings no loop, no tools, no compaction, so Nomad must supply all three — roughly what the retired `agent/loop.py` did |

## Known gaps

Deliberately deferred, not forgotten, and not scheduled above.

- **The `api` package has no authentication** — and no code, which is why this
  is still a gap rather than a bug. The *view* surface is now authenticated and
  remote by default (D40, shared token in `var/view-token`); the API must not
  inherit that by proximity when it is written. Solve it there or keep it on
  loopback.
- **Plugin entry-point discovery.** Registries take explicit registration only.
- **Multi-session / multi-user concurrency.** One session per device;
  `AgentSession` is not designed for concurrent sessions sharing a device.
- **OTA firmware update** for the ESP32-S3 and RP2040. Today firmware is a
  manual flash step, not a Pi-driven protocol message.
- **Encryption at rest** for SQLite (D7). Turn history, tool results and grants
  are plaintext on the Pi's storage.
