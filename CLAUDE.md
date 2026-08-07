# Nomad — project rules

Nomad is a modular pocket computer: a Raspberry Pi 4 running a **persistent AI
agent session** — a pocket Claude Code that stays alive as long as the device is
powered — with an ESP32-S3 touchscreen, joystick and buttons as its face, an
RP2040-Zero as a USB HID output, and a PiSugar S Plus for power.

`docs/DECISIONS.md` is the contract. If code and that file disagree, one of them
is a bug — fix it, don't work around it.

## The five rules that matter most

1. **Never execute a tool without a `Grant`.** `ToolExecutor.run()` takes a
   grant, not a request. Auto-approval *mints* a grant; it does not skip one.
   (D4)
2. **Tools act on a `Target`, never on the filesystem directly.** Permissions are
   evaluated per `(tool, target)` pair. (D12)
3. **No application code touches a GPIO pin, button index, or raw key code.**
   Everything consumes logical input actions. (D13)
4. **Transports move `bytes`.** Only a `Codec` knows what a message means. (D2)
5. **Mock is the default driver.** The whole system must run and test on a
   laptop with no hardware attached. (D9)

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

- The model never receives an unrestricted shell. `run_command` is a declared
  tool, disabled by default in config, and `never_auto` in every mode. (D5)
- Filesystem tools resolve paths and reject anything outside the workspace root.
  A prompt instruction is not a boundary; a resolved-path check is. (D15)
- `never_auto` overrides the permission mode, always: writes or exec outside the
  workspace, anything on an SSH target, any HID output, anything `DESTRUCTIVE`.
  A mode switch must never be able to turn this device into a keystroke
  injector. (D14)
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
