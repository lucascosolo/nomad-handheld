# Architecture

Module map, layering, and how a request actually flows through the system.
This document is written against `docs/DECISIONS.md`; if the two disagree,
DECISIONS.md wins and this file has a bug.

## Layering

```
api  →  agent  →  tools  →  targets/hardware  →  protocol  →  core
```

Dependencies point one way only. `core` sits at the bottom and depends on
nothing above it; `protocol` depends only on `core`. **Nothing imports
`api`** — not `agent`, not `tools`, not `core`. `api` is the outermost
consumer, never a dependency of anything else. `tests/test_layering.py`
enforces this mechanically.

## Packages

### `core` — lifecycle, events, config, logging, errors

Owns: process lifecycle (`NomadCore`, boot/shutdown sequencing), the
in-process `EventBus` (D6), layered TOML config loading and validation (D8),
structured logging, and the `NomadError` hierarchy.

Must NOT know about: hardware, tools, the agent loop, HTTP/WebSocket. `core`
is the substrate everything else is built on; it has no upward references.

### `storage` — durable state

Owns: the single SQLite database (WAL mode, D7), numbered migrations,
persistence for turns, tool grants, decisions, and compaction records. It is
a subscriber to the event bus, not a participant in the request path.

Must NOT know about: transports, targets, or the agent's in-flight
reasoning. It persists what the event bus hands it and answers queries —
it does not decide what is worth persisting.

### `protocol` — framing, codec, transport, wire types

Owns: `Framing` (byte-stream delimiting), `Codec` (`Message` ↔ bytes),
`Transport` (moves bytes only), and the `Message` types exchanged over the
two hardware links. See `PROTOCOL.md` for the wire format.

Must NOT know about: what a message *means* to the rest of the system, or
which target/tool triggered it. A `Transport` that inspected payloads to
make a decision would violate D2.

### `targets` — where tool actions actually land

Owns: the `Target` abstraction (D12) — `LOCAL`, `SSH`, `HID` — and their
capability sets. Targets are the only things that touch a real filesystem,
shell, or remote host.

Must NOT know about: permission policy, the model, or the agent loop. A
target executes what it is given; it does not decide whether it should.

### `tools` — declared capabilities the model can request

Owns: `ToolSpec` declarations (risk level, required permissions, target
compatibility), tool implementations, and the permission broker/executor
pipeline (D4, D5).

Must NOT know about: the AI provider's wire format or conversation history.
A tool receives a `Grant` and a `Target`; it has no idea which model, or
which turn, asked for it.

### `hardware` — device drivers

Owns: driver implementations for display, input, battery, camera, sensors,
USB HID, keyed off the `driver = "mock" | "..."` config strings (D9). Every
category has a mock, and mock is the default.

Must NOT know about: tools, targets, or the agent loop. A driver exposes a
narrow device interface (`Component`-shaped) and nothing else.

### `input` — logical input normalization

Owns: translating raw device events (touch, joystick, buttons, future
devices) into the logical `InputEvent` stream (`NAV_UP` … `ACTION_2` with
`PRESS`/`RELEASE`/`REPEAT` phases) per the TOML mapping (D13).

Must NOT know about: what consumes the events. No application code above
`input` may reference a GPIO pin, button index, or raw key code — that
constraint is enforced here, once, so nothing downstream has to.

### `agent` — the persistent session

Owns: `AgentSession` (D11), the provider-facing turn loop, context
compaction (`agent/context.py`, D16), and the permission broker's policy
layer (mode: manual/session/smart/auto, D14).

Must NOT know about: HTTP/WebSocket framing, or the concrete driver behind
a `Target`/tool — it deals in `Tool`, `Target`, and `Grant` abstractions.

### `assistant/providers` — model backends

Owns: `AIProvider` implementations (`mock`, `cloud`, `local`) speaking
Nomad's own message types, and the `ModelRouter` that picks a provider per
turn by task class, network state, and battery (D17, D18).

Must NOT know about: tool execution or targets — a provider proposes
`tool_call`s and observes `ToolResult`s; it never runs anything itself.

### `api` — external views onto the session

Owns: HTTP and WebSocket surfaces. A *view* onto the persistent
`AgentSession` (D11), not its owner — closing a connection does not end
the session.

Must NOT know about: nothing may know about `api` in return — the
constraint is entirely one-directional. `api` may depend on everything
below it.

## The permission pipeline (D4)

Four stages, never collapsed into one call:

```
┌─────────────┐     ┌──────────┐     ┌────────────────────┐     ┌────────────┐
│ ToolRequest │ ──▶ │ Decision │ ──▶ │ AuthorizationGrant  │ ──▶ │ ToolResult │
│ (model asks)│     │ (broker) │     │ (auto or human)     │     │ (executor) │
└─────────────┘     └──────────┘     └────────────────────┘     └────────────┘
```

- **ToolRequest** — the model names a tool, params, and target.
- **Decision** — the broker evaluates `(tool, target)` against `Risk`,
  required `Permission`s, the session's `never_auto` rules (D14), and any
  standing session grants (D14). It decides: auto-grant, prompt, or deny.
- **AuthorizationGrant** — a persisted object, minted either by policy
  (auto-approval) or by a human/model-judged approval. It is never
  synthesized inline at call time by `ToolExecutor`.
- **ToolResult** — `ToolExecutor.run()` accepts a `Grant`, never a bare
  request. **There is no code path that executes a tool without one.**
  Auto-run modes mint grants faster; they do not skip minting one.

Every Decision and Grant is persisted (`storage`) for audit, regardless of
which permission mode produced it.

## End-to-end vertical slice

User asks: *"What system are you running on?"*

1. **api** receives the message on the active WebSocket/HTTP view and hands
   it to the running `AgentSession` (D11) — it does not create a new one.
2. **agent** appends the turn, checks the context budget (D16), and calls
   the routed `AIProvider` (D17).
3. **assistant/providers** sends the turn to the model; the model returns a
   `tool_call` for `get_system_info` against target `local`.
4. **agent**'s broker evaluates the request: `get_system_info` is
   `READ_ONLY` — auto-grant, no prompt, in every permission mode (D14).
5. **tools**' `ToolExecutor.run()` receives the `AuthorizationGrant`, resolves
   the `local` `Target` (D12), and executes `get_system_info`.
6. The result is wrapped as a `ToolResult` and published to `core`'s
   `EventBus` (D6).
7. The bus fans out, error-isolated and non-blocking: **storage** persists
   the tool call and result; any subscribed WebSocket **api** views receive
   a live update.
8. **assistant/providers** observes the `ToolResult`, feeds it back to the
   model as the tool's return value.
9. The model produces final text; **agent** appends it to the turn,
   **storage** persists it, and **api** streams it to the connected view(s).

No step above imports `api`; step 1 is the only place `api` appears as a
caller, consistent with the layering rule.

## Surviving UI disconnect and reboot

- **UI disconnect (D11):** the WebSocket/HTTP layer is a view, not the
  session owner. A dropped connection does not touch `AgentSession`
  state. On reconnect, the view replays recent turns from `storage`
  rather than starting a new conversation.
- **Reboot / power cut (D11, D18):** turn state is persisted *before*
  execution begins, not after. A power cut mid-turn leaves a durable
  record of what was in flight — on restart, `NomadCore` boots
  `AgentSession` against that record and resumes or aborts cleanly. There
  is no window where an edit is half-applied with no trace. D18's battery
  policy is the proactive half of this: low/critical battery defers new
  background work and parks the session cleanly *before* power loss
  forces the reactive path.

## Key interfaces

```python
# core
class Component(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[Event], Awaitable[None]]) -> Subscription: ...

# tools
class Tool(Protocol):
    spec: ToolSpec  # name, risk, required Permissions, compatible TargetKinds

    async def run(self, grant: AuthorizationGrant, target: Target, params: dict) -> ToolResult: ...

# targets
class Target(Protocol):
    id: str
    kind: TargetKind          # LOCAL | SSH | HID
    capabilities: frozenset[Capability]

# protocol
class Transport(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes: ...

class Codec(Protocol):
    def encode(self, message: Message) -> bytes: ...
    def decode(self, data: bytes) -> Message: ...

# assistant/providers
class AIProvider(Protocol):
    async def send_turn(self, turn: Turn) -> ProviderResponse: ...

# input
class InputSource(Protocol):
    async def events(self) -> AsyncIterator[InputEvent]: ...  # logical only, never raw
```

These are illustrative shapes, not the literal source — see the packages
under `src/nomad/` for the implementations.
