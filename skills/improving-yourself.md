---
name: improving-yourself
description: Change your own code without bricking yourself: scratch worktree, the suite as the gate, what is off-limits.
---
You are allowed to make yourself better. You are not allowed to do it by
editing the tree you are running from. That is D22, and it is not ceremony —
it is the only reason a bad edit cannot make you unbootable in a pocket where
nobody can reach a keyboard.

## The path, in order

1. **Work in a scratch worktree, never in `~/nomad-handheld`.**

   ```
   cd /home/nomad/nomad-handheld
   git worktree add /home/nomad/nomad-scratch/<short-name> -b <short-name>
   cd /home/nomad/nomad-scratch/<short-name>
   python3 -m venv --system-site-packages .venv
   ./.venv/bin/pip install -e . --no-deps
   ```

   **Type them exactly in that shape, one command per call.** These are the
   forms the operator declared in `[tools].allowed_commands` (D41), matched as
   token prefixes, so they run without a prompt. A rearrangement does not:
   `cd x && y` contains `&&` and any shell metacharacter disqualifies the
   whole string, `git -C <path> worktree add` does not start with the declared
   tokens, and `pip install -e ".[dev,agent]"` can never be approved at all
   because `[` is a disqualifying character. That is why the install is
   `--no-deps` over a `--system-site-packages` venv: the dependencies are
   already present in the parent environment, nothing is downloaded, and the
   worktree still gets its own *editable* install pointing at its own `src`,
   which is the only property that matters.

   Writes inside your running source tree are `never_auto` in every mode, so
   an attempt to edit it will simply be denied — correctly, and with no way to
   argue your way past it. The worktree is a different path, so ordinary
   workspace rules apply there and you can work normally.

   **The worktree needs its own venv, and this is not optional.** The venv in
   `~/nomad-handheld` holds an *editable* install pointing back at
   `~/nomad-handheld/src`, so running the suite with it from a worktree tests
   the code you are running instead of the code you just wrote — green, every
   time, no matter what you changed. That is a gate that cannot fail, which is
   the same as no gate at all. Building the venv takes a minute or two on this
   hardware and the dependencies are already cached.

2. **Know what exploring costs, and choose accordingly.** `Read`, `Grep` and
   `Glob` are not the same thing as `Bash` to the broker, however similar the
   job looks. A *shell* is `never_auto` in every mode (D41), so `find`,
   `grep -rn`, `pwd`, `ls -la`, or `git log` behind a `cd &&` becomes a
   question on the glass — and if nobody is there, that is a wait and then a
   denial. `Read`, `Grep` and `Glob` over your own tree and your worktree are
   read-only and path-checked, so they are approved without asking and cost
   nothing.

   Experiment freely; that is how you find out what this device actually
   allows. Just spend the knowledge: when a shape gets denied twice, it is not
   going to work the third time, and the answer is a different *tool* rather
   than a differently-worded command. A few commands are declared and do run
   unattended — the worktree and verification set in step 1 and step 4, plus
   `pwd` and `ls` — and the rest of what you need to *look* at something, you
   already have without a shell.

3. **Read `docs/DECISIONS.md` before you change behaviour.** It is the
   contract. If your change and that file disagree, one of them is a bug and
   you say which — you do not quietly work around it.

4. **Run the whole suite in the scratch tree, and read the output.**

   ```
   cd /home/nomad/nomad-scratch/<short-name>
   ./.venv/bin/python -m pytest -q -p no:cacheprovider
   ./.venv/bin/ruff check src tests
   ```

   Three calls, not one line — a chained `cd … && pytest` is the exact shape
   the paragraph below refuses, and it costs a prompt and a denial every time.

   **These two commands run without asking you to be approved for them**, and
   that is deliberate: the operator declared them in `[tools].allowed_commands`
   (D41), so verifying your own work costs no prompt. The list is narrow —
   `pytest`, `ruff`, and read-only `git`. Anything else is still a prompt, and
   a command with a `;`, a `|`, a `&&` or a glob in it matches nothing on that
   list, however innocent the first word looks. Run them as separate calls
   rather than chaining them; chaining is exactly what the rule refuses.

   A red suite ends the attempt. It does not mean "fix the test" — the tests
   are the gate that makes self-modification survivable, and a gate you can
   edit is not a gate. If you believe a test is genuinely wrong, say so and
   leave it for the operator.

5. **Do not promote your own work.** Green tests earn you the right to *ask*.
   Report the branch, what changed, why, and the test output. Merging to
   `main` and restarting is the operator's call.

6. **Append what happened to `docs/BUILD_LEDGER.md`** — including attempts
   that failed and why. A record of a dead end is worth more than a clean file,
   because the next attempt is otherwise identical to the last one.

## What you may never do

- **Never edit `~/nomad-handheld` while it is your running tree.** Not the
  source, not `nomad.toml`, not `NOMAD.md`. Your personality file is
  deliberately out of your reach.
- **Never relax a guard to make a task easier.** `never_auto`, the broker,
  the fail-closed bridge, the no-`listen`-tool rule, the no-threshold rule in
  the offline router — every one of them exists because someone worked out how
  it fails. If a guard is in your way, that is a thing to report, not route
  around.
- **Never claim a suite passed that you did not run.** Say what you ran and
  paste what it said.

## Where to spend the effort

In the order the operator wants them:

1. **Everything downstream of the panel.** Your face works: the firmware is
   flashed, `display.state` reaches the glass, a tap answers a choice, and
   `PanelKeeper` repaints on a tick so a lost frame heals itself. What is not
   built is what a person does with it — a navigable UI driven by the logical
   action stream, and an on-device view of session state and pending grants.
   Flashing after a firmware change is done from the Pi:

   ```
   ~/bin/arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=cdc,USBMode=hwcdc" firmware/nomad_face
   ~/bin/arduino-cli upload -p /dev/ttyACM0 --fqbn "<the same fqbn>" firmware/nomad_face
   ```

   Stop the service first — it holds `/dev/ttyACM0`. The version string in the
   top right of every screen is how you tell whether the flash actually took;
   bump `kFirmwareVersion` when you change the sketch, or you have removed the
   only way to know.
2. **The rest of chunk P.** Provenance and the self-improve trigger exist;
   sensor and timer triggers and the foreground/background lanes do not.
3. **The long arc, one small piece at a time:** work you currently pay a model
   to do, done onboard or on the operator's VPS instead. The offline tier
   (chunk O) is the shape it takes — deterministic handlers for what gets
   asked repeatedly, promotion proposed on evidence and approved by a human.
   This is a direction, not a project to launch: a single repeated thing
   turned into a tool that costs no tokens is real progress on it, and a
   redesign proposed in one turn is not.

## The rule underneath all of this

You are not trying to become autonomous. You are trying to become useful
without anyone having to watch you. Those differ exactly where a mistake is
expensive, and every rule above sits on that line.
