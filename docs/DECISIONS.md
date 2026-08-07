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

## Deliberately deferred

Not in the MVP, recorded so nobody assumes they were forgotten:

- **HTTP API authentication.** Binds to localhost initially. Must be solved
  before the API is exposed on a network — tracked in ROADMAP.
- **Plugin discovery via entry points.** The registry takes explicit
  registration first.
- **Multi-user / multi-session concurrency.** One session per device for now.
- **OTA firmware update** for the ESP32 and RP2040.
- **Encryption at rest** for the SQLite database.
