# Roadmap

Phased, ordered by dependency — not by calendar. No dates or time estimates
are given here on purpose; see `docs/DECISIONS.md` for why each phase's
foundations are shaped the way they are.

## Phase 0 — this build: the MVP vertical slice

What ships first, and what it deliberately does not:

- `NomadCore` lifecycle, event bus (D6), layered TOML config (D8),
  structured logging, SQLite storage with migrations (D7).
- The full permission pipeline (D4): `ToolRequest → Decision →
  AuthorizationGrant → ToolResult`, with every stage persisted, and all
  four permission modes (D14) implemented against it.
- `Target` abstraction with three kinds (D12): `LOCAL` implemented,
  `SSH` and `HID` defined with stubs that raise — present in the type
  system and permission logic now so retrofitting them later doesn't touch
  the agent loop.
- `AgentSession` as a persistent component (D11) surviving UI disconnect
  and replaying state on reconnect; turn state persisted before execution.
- Every hardware category (display, input, battery, camera, sensors, both
  transports) behind a mock driver, mock as the default (D9) — the whole
  system runs and tests with zero hardware attached.
- `AIProvider` abstraction with a `mock` provider; `cloud` and `local`
  defined but not necessarily wired to a real backend yet.
- Logical input contract (D13) and its TOML mapping, exercised against
  mock input sources.
- HTTP API on localhost only, no authentication (see Known gaps).

Not in Phase 0: real hardware drivers, real AI provider traffic, any UI
shell, SSH/HID actually working, games, camera/sensor capture, local model
inference.

## Phase 1 — real ESP32-S3 serial transport + display

Depends on: Phase 0's `protocol` package (Framing/Codec/Transport, D2/D3)
and `hardware` display driver interface.

- Real `serial` transport kind for `transports.esp32` (pyserial-asyncio,
  already an optional dependency).
- Real `esp32` display driver implementing `display.draw`/`blit`/
  `backlight` against actual firmware.
- Reconnect/resync handling exercised against a real MCU reboot, not just
  the mock.

## Phase 2 — real input hardware

Depends on: Phase 1's transport (touch/joystick/button events arrive over
the same link as display) and Phase 0's logical input contract (D13).

- ESP32-S3 firmware reporting `input.touch`/`input.joystick`/
  `input.button`.
- Physical → logical mapping validated against real device event rates
  (joystick deadzone, repeat timing in `[input.joystick]`).

## Phase 3 — real AI provider + agent tools

Depends on: Phase 0's `AIProvider` abstraction, `ToolSpec`/permission
pipeline, and `Target` abstraction (D4, D5, D12, D17).

- `cloud` provider wired to a real backend (`ai.cloud` config, API key
  from environment per D8).
- Core agentic tool set: read, write, edit, grep, glob, `run_command`
  (declared `never_auto`, disabled by default per D5).
- Tools exercised against the `LOCAL` target under all four permission
  modes, including `never_auto` enforcement at the broker (D14).

## Phase 4 — UI shell with controller navigation

Depends on: Phase 2's real input, Phase 1's real display, and D13's
logical-input-only rule (no GPIO/button-index references in UI code).

- A navigable menu/UI rendered to the ESP32-S3, driven entirely by the
  logical input stream.
- Live view of `AgentSession` state (turns, pending grants) on-device, not
  just over HTTP/WebSocket.

## Phase 5 — SSH target

Depends on: Phase 3's tool set (tools already written against `Target`,
not the filesystem directly, per D12) and the permission broker treating
`(tool, target)` as the decision unit.

- Real implementation behind the `SSH` `TargetKind` stub, with its own
  identity/auth on the remote host.
- Exercises `never_auto` for any action on an `SSH` target (D14) end to
  end — this was designed for from Phase 0, so this phase should not touch
  the agent loop.

## Phase 6 — HID target

Depends on: Phase 3's tool set and the RP2040 link from `PROTOCOL.md`.

- Real implementation behind the `HID` `TargetKind` stub.
- `usb_hid` driver (`rp2040`) sending `hid.key`/`hid.pointer` over the
  Pi ↔ RP2040 link.
- `EXTERNAL_DEVICE` risk and `never_auto` enforcement (D14) exercised
  against a real external host, not just the mock.

## Phase 7 — camera / sensors

Depends on: Phase 0's driver/mock pattern (D9) extended to new device
categories (`docs/HARDWARE.md`, "Future expansion").

- Real `picamera2`-backed camera driver (already an optional dependency).
- IMU/ToF drivers as they're selected.
- Corresponding tools (e.g. `capture_photo`) declared with appropriate
  `Risk` and exposed through the same permission pipeline as any other
  tool — no special-casing hardware capture in the broker.

## Phase 8 — local model routing

Depends on: Phase 3's `AIProvider`/`ModelRouter` abstraction (D17) already
being live with `cloud`.

- Real `local` provider: 1–3B model, Q4 quantization, `llama.cpp` on CPU
  (see `HARDWARE.md` for why this is the realistic ceiling on this
  hardware — not an accelerator-based approach).
- `ModelRouter` actually switching on task class, network availability,
  and battery state (D18) rather than always resolving to `cloud`.
- Scope: intent classification, wake-word, offline fallback, short
  answers. Not agentic coding — that stays `cloud` per D17 until the
  hardware assumption changes (more RAM, a different SBC).

## Phase 9 — games

Depends on: Phase 4's UI shell and Phase 2's real input with working
`REPEAT` phase timing (D13) — games need hold-to-move where menus need
edge-triggering, and both must ride the same logical input stream without
forking it.

## Known gaps

Carried verbatim in intent from DECISIONS.md's "Deliberately deferred" —
not forgotten, not yet scheduled to a phase above:

- **HTTP API has no authentication and binds to localhost only.** This
  **must** be solved before the API is exposed on any network beyond the
  device itself. No phase above closes this yet; it is a prerequisite for
  any phase that would expose the API off-device (e.g. a companion mobile
  app), not an incidental cleanup.
- **Plugin entry-point discovery.** The tool/driver registry takes
  explicit registration only; dynamic discovery via Python entry points is
  not implemented.
- **Multi-session / multi-user concurrency.** One session per device.
  `AgentSession` (D11) is not designed for concurrent sessions sharing a
  device.
- **OTA firmware update** for the ESP32-S3 and RP2040-Zero. Firmware
  updates today are a manual flash step, not a Pi-driven protocol message.
- **Encryption at rest** for the SQLite database (D7). Turn history, tool
  results, and grants are stored in plaintext on the Pi's storage.
