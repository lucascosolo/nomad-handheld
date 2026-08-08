# Nomad — project rules

Nomad is a modular pocket computer: a Raspberry Pi 4 running a **persistent AI
agent session** — a pocket Claude Code that stays alive as long as the device is
powered — with an ESP32-S3 touchscreen, joystick and buttons as its face, an
RP2040-Zero as a USB HID output, and a PiSugar S Plus for power.

`docs/DECISIONS.md` is the contract. If code and that file disagree, one of them
is a bug — fix it, don't work around it.

**There are four documents and there should stay four.** `CLAUDE.md` (rules),
`docs/DECISIONS.md` (the contract), `docs/ARCHITECTURE.md` (module map,
interfaces, hardware, wire protocol), `docs/BUILD_LEDGER.md` (state, next
action, roadmap, known gaps). Hardware, protocol and roadmap files existed once
and were deleted into these — more files meant more places for the contract to
drift, and it did. Add to a file rather than starting one.

## The seven rules that matter most

1. **Never execute a tool without a `Grant`.** `ToolExecutor.run()` takes a
   grant, not a request. Auto-approval *mints* a grant; it does not skip one.
   (D4)
2. **Tools act on a `Target`, never on the filesystem directly.** Permissions are
   evaluated per `(tool, target)` pair. (D12)
3. **No application code touches a GPIO pin, button index, or raw key code.**
   Everything consumes logical input actions. (D13)
4. **Transports move `bytes`.** Only a `Codec` knows what a message means. (D2)
5. **Mock is the default everywhere** — drivers *and* the agent backend. The
   whole system must run and test on a laptop with no hardware, no CLI and no
   credentials. (D9, D24)
6. **`claude-agent-sdk` is imported in exactly one module**,
   `agent/backends/claude_cli.py`, and no SDK type crosses that boundary. This
   is what keeps the swap to a local model over Tailscale cheap. (D24)
7. **Nomad never edits its own running source tree.** Self-upgrading means
   authoring apps under `var/apps/` (D25) and validated settings changes (D26).
   Core changes go through the scratch-worktree path in D22.

## Layering

```
api  →  agent  →  tools  →  targets/hardware  →  protocol  →  core
```

Dependencies point one way only. `core` and `protocol` depend on nothing above
them. **Nothing imports `api`.** `tests/test_layering.py` enforces this — if it
fails, the fix is the import, not the test.

## Conventions

- Python 3.11+, async-first (D1). Blocking work goes to `asyncio.to_thread`.
- Pydantic v2 models for anything crossing a boundary — config, wire frames,
  tool params, API bodies.
- Structured logging via `nomad.core.logging`. Never bare `print`.
- Type hints on every public function. `X | None`, not `Optional[X]`.
- Errors derive from `nomad.core.errors.NomadError`.
- Follow the conventions already in the file you are editing.

## Running things

```bash
pip install -e ".[dev]"          # hardware extras are optional, never required
python -m nomad                  # start core + API on localhost
pytest                           # full suite, no hardware needed
ruff check src tests
```

**This laptop is small (2 CPUs, ~3.8 GB RAM).** Pin test runners to one worker
and `nice` anything heavy. Never run two heavy commands at once.

## Security posture — non-negotiable

- **Every Claude Code tool call routes through `can_use_tool` into Nomad's
  broker.** Claude Code keeps its full toolset — capability was never the thing
  to cripple — but the broker is now the only thing between the model and the
  device. A bridge that errors, times out, or cannot classify **denies**. Fail
  closed, always. (D21)
- `never_auto` overrides the permission mode, always, in every mode including
  `auto`: any HID output, anything on an SSH target, anything `DESTRUCTIVE`, and
  any write to Nomad's own running source tree. A mode switch must never be able
  to turn this device into a keystroke injector. (D14, D21)
- Because Claude Code is not confined by `--add-dir`, **the workspace root is a
  policy line, not a hard wall.** Say that plainly rather than implying a
  sandbox that isn't there. A resolved-path check still beats a prompt
  instruction wherever a path is resolved at all. (D15, D21)
- `ANTHROPIC_API_KEY` is stripped from the backend's child environment. If it
  leaks through, the CLI bills per token instead of the subscription. Never
  `--bare` — it never reads OAuth. (D20)
- Every decision, grant, and execution is persisted and auditable.

## Hardware reality check

- The ESP32-S3 is a **display and input peripheral**, not a co-processor. Its
  microSD slot is not primary storage.
- The RP2040 is a keyboard/mouse to whatever it is plugged into. Treat it as an
  output weapon: `EXTERNAL_DEVICE` risk, never auto.
- **An Edge TPU cannot run an LLM.** Do not design around one replacing the
  cloud model. See D17 before proposing onboard inference.

## Scope discipline

Build the clean foundation, not the feature. SSH and HID targets are interfaces
with stubs on purpose — adding them later must not require touching the agent
loop. If a change would, say so before writing it.

The same applies to the backend: Claude Code is *an* implementation, not the
architecture. Any code that assumes the backend brings its own loop, tools, or
compaction belongs behind a `BackendCapability` check, because a raw local model
brings none of the three. (D24)
