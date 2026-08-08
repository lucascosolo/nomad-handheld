# Architecture

Module map, layering, and how a request actually flows through the system.
This document is written against `docs/DECISIONS.md`; if the two disagree,
DECISIONS.md wins and this file has a bug.

**Interfaces below are copied from the source, not paraphrased.** An earlier
draft of this file invented plausible signatures that never matched the code
(`Tool.run(grant, target, params)`, a broker living in `agent/`). If you are
reading this after a context compaction, trust `src/` over this file and fix
the file.

## Build state

`git log` is the authority; this is the map.

| Package | State |
|---|---|
| `core` | **Built** — errors, logging, config, events, lifecycle |
| `storage` | **Built** — SQLite WAL, migrations, grant/event/conversation repos |
| `targets` | **Built** — `LocalTarget` real; SSH and HID raise `NotImplementedError` by design |
| `tools` | **Built** — spec/registry/workspace + the whole permission pipeline |
| `agent` | **Being rewritten** by chunk G — `loop.py` and `context.py` are retired by D19 |
| `protocol`, `hardware`, `input` | Not built — chunk E |
| `apps`, `settings` | Not built — chunk I (D25, D26) |
| `mcp` | Not built — chunk G (hardware exposed to the model, D19) |
| `api`, `app.py`, `__main__.py` | Not built — chunk F |
| `selfupdate` | Not built — chunk H (D22) |

## Layering

```
api  →  agent  →  tools  →  targets/hardware  →  protocol  →  core
```

Dependencies point one way only. `core` sits at the bottom and depends on
nothing above it; `protocol` depends only on `core`. **Nothing imports
`api`** — not `agent`, not `tools`, not `core`. `api` is the outermost
consumer, never a dependency of anything else. `tests/test_layering.py`
enforces this mechanically (chunk F).

Two additional import rules, each enforced by a test rather than by
convention:

- **`claude-agent-sdk` may be imported in exactly one module**,
  `agent/backends/claude_cli.py` (D24). This is what keeps the eventual swap
  to a local model over Tailscale cheap.
- **No module above `input` may reference a GPIO pin, button index, or raw
  key code** (D13).

## Packages

### `core` — lifecycle, events, config, logging, errors

Owns: the `Component`/`ComponentRegistry` lifecycle (reverse-order shutdown,
start-failure rollback), the in-process `EventBus` (D6), layered TOML config
loading and validation (D8), structured logging, and the `NomadError`
hierarchy.

`NomadConfig` is `extra="forbid"`: a key in `nomad.toml` with no field on the
model is a startup failure, not a silent default. This is deliberate — it is
what stops the config file and the config model from drifting apart.

Must NOT know about: hardware, tools, the agent, HTTP/WebSocket.

### `storage` — durable state

Owns: the single SQLite database (WAL mode, D7) on one worker thread,
numbered migrations with a `schema_version` table, and repositories for
events, conversations, and grants.

Must NOT know about: transports, targets, or the agent's in-flight
reasoning. It persists what it is handed and answers queries — it does not
decide what is worth persisting.

### `protocol` — framing, codec, transport, wire types

Owns: `Framing` (byte-stream delimiting), `Codec` (`Message` ↔ bytes),
`Transport` (moves bytes only), and the message types exchanged over the two
hardware links. The wire format is specified under "Protocol" below and is
**still a draft** — it gets ratified as a decision once chunk E implements
it, not before.

Must NOT know about: what a message *means*. A `Transport` that inspected a
payload to make a decision would violate D2.

### `targets` — where tool actions actually land

Owns: the `Target` abstraction (D12) — `LOCAL`, `SSH`, `HID` — and their
capability sets. Capability is checked *before* permission logic runs, so a
filesystem tool aimed at an HID target fails on capability grounds and never
reaches the broker.

Must NOT know about: permission policy or the model. A target executes what
it is given; it does not decide whether it should. Note the comment in
`FilesystemOps`: paths arriving at a target are **already** resolved and
boundary-checked by the tool layer — a target does not enforce the workspace.

### `tools` — declared capabilities, and the permission pipeline

Owns: `ToolSpec` declarations (risk, required permissions, compatible target
kinds), the `Workspace` boundary check (D15), tool implementations, and —
this is the part the old draft got wrong — **the entire permission pipeline
lives here, in `tools/permissions.py`, not in `agent/`.** `PermissionBroker`,
`AuthorizationQueue`, and `ToolExecutor` are all `tools` code.

That placement is load-bearing: it is why the broker survived the D19 pivot
untouched while the agent loop above it was thrown away.

Must NOT know about: the model's wire format or conversation history.

### `hardware` — device drivers

Owns: drivers for display, input, battery, camera, sensors, USB HID, keyed
off `driver = "mock" | "..."` config strings (D9). Every category has a mock,
and **mock is the default** — the full suite runs on a laptop with no
hardware, no CLI, and no credentials.

### `input` — logical input normalization

Owns: translating raw device events (touch, joystick, buttons, future
devices) into the logical action stream per the TOML mapping (D13).

The core action set — `NAV_UP`/`NAV_DOWN`/`NAV_LEFT`/`NAV_RIGHT`, `CONFIRM`,
`BACK`, `ACTION_1`, `ACTION_2` — is always present and cannot be removed.
The set is **extensible**: `[input].extra_actions` registers additional
actions such as `ASSISTANT`, which is what makes "remap B to a dedicated AI
button" a typed settings change rather than a special case (D26).

Must NOT know about: what consumes the events.

### `agent` — the persistent session and the swappable backend

Owns: `AgentSession` (D11) and the `AgentBackend` interface (D24). Nomad does
**not** implement its own think→tool→observe loop any more; Claude Code does
that (D19).

```
agent/
  session.py            # AgentSession — persistent, survives UI disconnect (D11)
  backends/
    base.py             # AgentBackend Protocol + AgentEvent + BackendCapability
    claude_cli.py       # the ONLY module allowed to import claude-agent-sdk
    remote_llm.py       # planned: a model on the tailnet
    mock.py             # no subprocess, no network — the default
  permission_bridge.py  # can_use_tool → PermissionBroker (D21)
```

Backends declare `capabilities: frozenset[BackendCapability]` — `OWN_LOOP`,
`OWN_TOOLS`, `OWN_COMPACTION`. Claude Code brings all three; a raw local LLM
brings none, so Nomad must supply what a backend lacks. This is why the
interface must not assume the backend is agentic (D24).

Must NOT know about: HTTP/WebSocket framing, or the concrete driver behind a
`Target`.

### `mcp` — Nomad's hardware, exposed to the model

Owns: the in-process MCP server that gives the model the tools Claude Code
has no equivalent for — display, HID, battery, input, sensors. Claude Code
brings its own filesystem, shell, search, and web tools; Nomad's retired
built-in file tools were duplicates of better ones. `get_system_info`
survives.

### `apps` — self-authored capabilities (D25)

Owns: the `AppManifest` schema, the registry, and the supervisor for apps
living under `var/apps/<app_id>/` — **outside the repo**, because D21 forbids
writes to Nomad's own source tree.

Registration is **gated, not automatic**: manifest validates, module imports
cleanly, entry point exists, smoke-launch survives N seconds. Fail any step
and the app is quarantined with the error surfaced, never registered. Apps
run as supervised asyncio tasks; a crash returns the user to the home screen
and must never take Nomad down.

Apps consume logical input actions only and draw through the display
abstraction. This is the payoff for D13's cost: a self-authored game gets
working controller input for free.

### `settings` — self-configuration (D26)

Owns: `SettingsService` — typed, validated mutations of Nomad's own config.
Every change validates against the Pydantic model *before* it is written,
lands in an audit log with before/after, and is reversible via `revert(n)`.
Writes go to `nomad.local.toml`, never the committed defaults.

The model does not free-edit TOML. An invalid write bricks the device on next
boot; validate-then-write plus a revert path is the difference between a
reconfigurable device and an unbootable one.

### `api` — external views onto the session

Owns: HTTP and WebSocket surfaces. A *view* onto the persistent
`AgentSession` (D11), not its owner — closing a connection does not end the
session. Bound to `127.0.0.1` until it has auth (see ROADMAP).

## The permission pipeline (D4)

Four stages, never collapsed into one call:

```
┌─────────────┐     ┌──────────┐     ┌─────────────────────┐     ┌────────────┐
│ ToolRequest │ ──▶ │ Decision │ ──▶ │ AuthorizationGrant  │ ──▶ │ ToolResult │
│ (model asks)│     │ (broker) │     │ (auto or human)     │     │ (executor) │
└─────────────┘     └──────────┘     └─────────────────────┘     └────────────┘
```

- **ToolRequest** — the model names a tool, params, and target.
- **Decision** — the broker computes a *scope* from the request, evaluates
  `never_auto` first, then the permission mode (D14) and any standing session
  grants. Outcome: auto-grant, prompt, or deny.
- **AuthorizationGrant** — a persisted object, minted either by policy or by
  human approval. Never synthesized inline at call time.
- **ToolResult** — `ToolExecutor.run(grant, request)` **recomputes the scope
  from the request** and re-checks it against the grant, so an approved
  workspace write cannot be replayed against `/etc`.

Defense in depth, both halves deliberate: `never_auto_reason()` is consulted
before any mode branching *and* again inside `mint_grant()`. A mode switch
must never be able to turn this device into a keystroke injector.

Note what `ToolContext` deliberately omits: the grant, the conversation, and
the model. A tool that could read its grant could be tempted to reason about
it. Validation is the executor's job and is finished before `execute` is
reached.

### Where the broker sits after D19

Claude Code runs with its **full** toolset and is not confined by `--add-dir`.
Every call it makes routes through `can_use_tool` into the same
`PermissionBroker`.

```
Claude Code ──can_use_tool──▶ permission_bridge ──▶ PermissionBroker ──▶ touchscreen
```

The consequence, stated plainly: **the workspace root stops being a hard wall
and becomes a policy line** (D21). The broker is now the only thing between
the model and the device, so `never_auto` carries weight it did not carry
before. In *every* mode including `auto` it denies: any HID output, any SSH
action, anything `DESTRUCTIVE`, and any write to Nomad's own running source
tree (those go through D22's self-update path instead).

A bridge that errors, times out, or cannot classify **denies**. Fail closed,
always.

Because the bridge sits *above* the backend, gating is backend-independent —
a local model over Tailscale gets policed by exactly the same broker.

## End-to-end vertical slice

User asks: *"What system are you running on?"*

1. **api** receives the message on the active view and hands it to the running
   `AgentSession` (D11) — it does not create a new one.
2. **agent** forwards it to the configured `AgentBackend.send()`.
3. The backend (`claude_cli`) writes the prompt to the Claude Code process and
   reads `stream-json` events back. Claude Code owns the loop, the context
   window, and compaction — Nomad does not.
4. Claude Code proposes a tool call. Before it runs, `can_use_tool` fires into
   **permission_bridge** → `PermissionBroker.decide()`.
5. `get_system_info` is `READ_ONLY` on the local target — auto-granted in every
   mode (D14), with a `Grant` still minted and persisted. Auto-approval mints
   grants faster; it never skips minting one.
6. `ToolExecutor.run(grant, request)` re-derives the scope, resolves the
   `local` `Target` (D12), and calls `execute(params, ctx)`.
7. The `ToolResult` is published to the `EventBus` (D6) — error-isolated,
   non-blocking, and **dropping rather than backpressuring** a slow subscriber.
   **storage** persists the call and result; subscribed **api** views get a
   live update.
8. The result goes back to Claude Code as the tool's return value; it produces
   final text, which streams out to the connected view(s) and to the display.

No step imports `api`.

## Surviving UI disconnect and reboot

- **UI disconnect (D11):** the WebSocket/HTTP layer is a view, not the session
  owner. A dropped connection does not touch `AgentSession` state. On
  reconnect, the view replays recent turns from `storage`.
- **Reboot / power cut (D11, D18):** turn state is persisted *before*
  execution begins, not after, so a power cut mid-turn leaves a durable record
  of what was in flight. Session identity is an explicit persisted UUID passed
  as `--session-id` and resumed with `--resume` — **never `--continue`**, which
  resolves to "most recent conversation in this directory", ambient state a
  long-lived daemon must not depend on (D20).
- **Battery (D18):** low/critical battery defers new background work and parks
  the session cleanly *before* power loss forces the reactive path.

## Key interfaces

Copied from source as of the current build. Shapes marked *(planned)* do not
exist yet and are the contract chunk G/E/I must meet.

```python
# core/lifecycle.py
class Component(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

# core/events.py — note publish is async, and subscribe takes a pattern
class EventBus:
    def subscribe(self, pattern: str, handler: Handler) -> Unsubscribe: ...
    async def publish(self, event: Event) -> None: ...

# tools/base.py
@dataclass(frozen=True)
class ToolContext:
    target: Target
    workspace: Workspace
    session_id: str
    turn_id: str | None
    logger: LoggerAdapter
    timeout_seconds: float = 120.0

class Tool(Protocol):
    spec: ToolSpec  # name, risk, required Permissions, compatible TargetKinds
    async def execute(self, params: BaseModel, ctx: ToolContext) -> ToolResult: ...

# tools/permissions.py
class PermissionBroker:
    async def decide(self, request: ToolRequest, mode: PermissionMode) -> Decision: ...
    async def mint_grant(...) -> AuthorizationGrant: ...

class ToolExecutor:
    async def run(self, grant: AuthorizationGrant, request: ToolRequest) -> ToolResult: ...

def never_auto_reason(spec: ToolSpec, target: Target, scope: str) -> str | None: ...

# targets/base.py
class Target(Protocol):
    id: str
    kind: TargetKind                      # LOCAL | SSH | HID
    capabilities: frozenset[Capability]   # FILESYSTEM | EXEC | HID_OUTPUT

# agent/backends/base.py  (planned, D24)
class AgentBackend(Protocol):
    name: str
    capabilities: frozenset[BackendCapability]   # OWN_LOOP | OWN_TOOLS | OWN_COMPACTION
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, text: str, *, session_id: str) -> None: ...
    def events(self) -> AsyncIterator[AgentEvent]: ...
    async def interrupt(self) -> None: ...

# protocol  (planned, chunk E)
class Transport(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes: ...

class Codec(Protocol):
    def encode(self, message: Message) -> bytes: ...
    def decode(self, data: bytes) -> Message: ...
```

---

# Hardware

Roles and honest limits. **No part of this is required to develop or test**
(D9) — every driver category has a mock and mock is the config default.

| Component | Role | Link to the Pi | Explicitly not |
|---|---|---|---|
| **Raspberry Pi 4 (4 GB)** | The brain. Linux, `AgentSession`, event bus, storage, all decisions. | — it *is* the host | Rendering pixels or injecting keystrokes itself |
| **ESP32-S3** + touchscreen, joystick, 4 buttons, **mic and speaker** | Display, input and audio peripheral | USB CDC serial, 921600 baud (control) **+ a separate USB Audio Class interface** (sound, D37) | **A co-processor.** No application logic runs on it, and no speech is recognised or synthesised on it. Its microSD is not primary storage |
| **RP2040-Zero** | USB HID keyboard/mouse into an external host | USB CDC serial, 115200 baud (control); USB HID (output) | Bidirectional. Output-only from the Pi's view |
| **PiSugar S Plus** | Battery power and telemetry | I2C | A decision-maker. It reports numbers; D18 policy decides |

The Pi's own storage is the only durable store that matters: SQLite (D7),
workspace root (D15), config, logs.

**Parts actually in hand, as of 2026-08-08.** Only the display module exists
physically; everything else in the table above is still a mock driver, which is
the plan (D9) and not a gap.

| Part | Status |
|---|---|
| Hosyond ESP32-S3 touchscreen module, UPC 712490971738 | Connected by USB-C, powered, **running its factory demo** — i.e. stock firmware, not D30's wire format. Not yet addressable by Nomad |
| Raspberry Pi 4 | Powered down while fans are fitted; it ran hot |
| RP2040-Zero, PiSugar S Plus | Not yet acquired/wired |

Before any firmware is written, confirm against the module itself rather than
this table: panel resolution, touch controller IC, USB-serial bridge, and
whether its mic and amplifier can be exposed as USB Audio Class alongside CDC
(D37). Guessing any of them wastes a flash cycle.

> **WARNING — the RP2040 is a keystroke-injection device by construction.**
> Typing into another computer is not a side effect to be careful about; it
> is the entire feature. Therefore: every command to it is `HID` kind,
> `EXTERNAL_DEVICE` risk (D5, D12); HID output is `never_auto` **in every
> mode including `auto`** (D14); and an `HID` target has no filesystem
> capability at all, so a file tool aimed at it fails the capability check
> before permission logic even runs.

## Onboard inference — the honest section (D17)

This hardware **cannot** host a capable coding model. Be specific, because
"add an accelerator" sounds like it solves this and does not:

- **An Edge TPU (Coral) or Hailo NPU is an int8 vision accelerator** with
  single-digit megabytes of on-chip memory, designed for classification and
  detection kernels — not autoregressive transformer decoding. It cannot
  host a useful language model. Not "a small one." None.
- **Realistic onboard inference here is CPU-only:** a 1–3B model at Q4 via
  `llama.cpp` on the Pi 4's four cores — roughly 1–2 GB RAM and a few
  tokens/second. Useful for wake-word, intent classification, and offline
  fallback. Not useful for agentic coding.
- **If onboard agentic capability ever becomes the goal**, the fix is more
  RAM and a better SBC (Pi 5 with 8–16 GB, or an SBC with an NPU actually
  sized for LLM inference) — not an accelerator stick bolted onto a Pi 4.

This is why D24's `remote_llm` backend reaches a model **on a workstation
over Tailscale** rather than running one on the device.

## Power budget (D18)

| Signal | Policy response |
|---|---|
| `battery.low_threshold` (20%) | Defer background work; prefer cheaper model routing where task class allows |
| `battery.critical_threshold` (8%) | Park the session cleanly — persist in-flight turn state, stop accepting turns — before shutdown forces it |

The failure mode being designed against is a corrupted partial write
mid-turn, not merely data loss. Turn state persists *before* execution
(D11), so even an unplanned power cut resumes or aborts on next boot.

---

# Protocol

**Status: ratified as D30**, and the implementation corrected the draft in
three places — see D30 for what changed and why. Freezing a wire format on
paper before code exists is how you ship a bad one, which is precisely what
nearly happened here: the drafted framing could not recover from a corrupt
length prefix. The `type`/`id`/`seq`/`payload` envelope (D3) survived
unchanged.

Both links are USB CDC serial: Pi ↔ ESP32-S3 (display/input) and Pi ↔
RP2040-Zero (HID output).

## Three concerns, three objects (D2)

| Object | Owns | Knows nothing about |
|---|---|---|
| `Framing` | Delimiting a byte stream into frames | Message meaning |
| `Codec` | `Message` ↔ `bytes` | How bytes move |
| `Transport` | Moving `bytes` | Message meaning, framing internals |

JSON is the starting codec because it is trivial to debug over a serial
console and cheap on both MCUs. It is explicitly **not** load-bearing: when
the display link needs binary framebuffer deltas, the `Codec` is swapped for
a binary/CBOR one and no `Transport` or driver code changes, because they
only ever dealt in `bytes` and `Message` objects respectively.

## Frame structure (D3)

Every frame carries `type`, `id`, `seq`, `payload`.

**Why both `id` and `seq`:** microcontrollers reboot and USB re-enumerates.
`id` alone cannot distinguish a late response from a wrong one after a
reboot resets counters. `seq` resetting to a low value *is* the signal the
Pi uses to detect that a reboot happened — it then discards pending requests
on that link and re-establishes state via `system.hello`/`system.status`
rather than assuming continuity.

Framing on the wire (**ratified as D30** when chunk E1 implemented it — this
is no longer a draft, and it is not what the draft said):

| Bytes | Field | Notes |
|---|---|---|
| 2 | `SYNC` (`a7 5e`) | Resync anchor |
| 4 | `length` (u32 LE) | Length of `frame_body` only |
| `length` | `frame_body` | Codec-encoded `Message` |
| 4 | `checksum` (CRC32 LE) | Over the `length` field **and** `frame_body` |

Length-prefixed rather than sentinel-delimited, so a binary codec never has
to escape a delimiter appearing in the body. A checksum failure is treated
as frame loss — it bumps expected `seq`, it does not kill the link.

The `SYNC` preamble and the checksum's coverage of `length` are the
correction D30 records: without them, a single flipped bit in the `length`
prefix makes the parser skip the wrong number of bytes and **every**
subsequent frame is garbage, permanently — on exactly the flaky-USB failure
mode this format exists to survive. Together they collapse both corruption
cases into one rule: *after a checksum failure, never trust `length` — scan
forward for the next `SYNC`.* Frames are also capped (`max_frame_bytes`,
default 65536) so a corrupt length cannot make the Pi allocate hundreds of
megabytes.

## Message catalogue (draft)

**Pi ↔ ESP32-S3.** `type` follows a `<group>.<action>` convention.

| Type | Direction | Payload sketch |
|---|---|---|
| `display.state` | Pi → ESP32 | `{kind, title, body, rows, items, selectable, question, options}` — a whole screen, structurally |
| `display.draw` | Pi → ESP32 | `{x, y, w, h, pixels}` — raw or RLE region |
| `display.blit` | Pi → ESP32 | `{x, y, region_id}` |
| `display.backlight` | Pi → ESP32 | `{level: 0-255}` |
| `input.touch` | ESP32 → Pi | `{x, y, phase: down\|move\|up}` |
| `input.joystick` | ESP32 → Pi | `{x: -1.0..1.0, y: -1.0..1.0}` |
| `input.button` | ESP32 → Pi | `{button: a\|b\|x\|y, phase: press\|release}` |
| `system.hello` | either | `{firmware_version, capabilities}` — on connect |
| `system.status` | ESP32 → Pi | `{uptime_ms, free_heap, last_seq_seen}` |
| `system.error` | either | `{code, message}` |

**`display.state` carries Nomad's display vocabulary; `display.draw` carries
pixels.** The first driver written against this catalogue packed a JSON blob
into `display.draw`'s `pixels` field — it worked, and it was a lie on the
wire, since anything reading a capture would treat that field as an image.
Structure is also the cheaper design: a card is a few dozen bytes against
8–11 KB rasterised, on a link shared with input events, and layout belongs on
the side that owns the panel and its fonts. `display.draw` stays for the
cases that really are pixels — an app's framebuffer, an image.

**Audio is deliberately absent from this link.** An earlier draft carried
`audio.mic_stream` and `audio.speaker` here. It does not fit: 16 kHz 16-bit
mono is ~256 kbit/s of raw PCM before any framebuffer traffic, and the link
is 921600 baud shared with `display.draw`. Voice is the device's primary
input method (chunk V), so it does not get to contend with the screen for
bandwidth. Microphone and speaker hang off the **Pi** — USB or I2S — and
`nomad.audio` never touches this transport.

The `input.*` values above are physical and device-local. They are
normalized into logical actions immediately on receipt by `nomad.input` and
are never referenced by button or pin identity above that layer (D13).

**Pi ↔ RP2040-Zero** — output-only. Every non-status message is
`EXTERNAL_DEVICE` risk and `never_auto` in every mode.

| Type | Direction | Payload sketch |
|---|---|---|
| `hid.key` | Pi → RP2040 | `{keycode, phase: press\|release}` |
| `hid.pointer` | Pi → RP2040 | `{dx, dy, buttons}` |
| `system.status` | RP2040 → Pi | `{uptime_ms, host_connected}` |

No `input.*` flows on this link — it has no sensors.

## Extending without breaking older firmware

1. **New message types are additive.** An unrecognized `type` is ignored
   after framing validation, optionally reported via `system.error`. It must
   not crash or desync `seq`.
2. **New payload fields are additive and optional.** JSON gives this for
   free; a future binary codec must preserve it deliberately, via explicit
   field tags rather than positional encoding.
3. **Never repurpose a `type` string for a different payload shape.** Mint
   `display.draw_v2` instead, and let `system.hello`'s `capabilities` tell
   the Pi which version the firmware speaks.
4. **Envelope or framing changes require both sides in lockstep.** There is
   no negotiation below the envelope, which is exactly why D3 rates this
   high-cost and why the path is deliberately rare.
