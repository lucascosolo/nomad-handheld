# Hardware

Roles, connections, and honest limits for each component. Nomad is a
modular pocket computer: a Raspberry Pi 4 as the brain, an ESP32-S3 as the
face, an RP2040-Zero as an output device, and a PiSugar S Plus for power.

Development does not require any of this hardware to be present (D9) — see
"Development without hardware" below.

## Raspberry Pi 4 (4 GB)

**Role:** the brain. Runs Linux, the persistent `AgentSession` (D11), core
event bus, storage, and all decision-making. Everything in `docs/
ARCHITECTURE.md` except the display/input firmware runs here.

**Connected over:** onboard — this is the host, not a peripheral.

**Not responsible for:** rendering pixels directly to a physical screen
(that's the ESP32-S3's job) or injecting keystrokes into another machine
(that's the RP2040's job). The Pi drives both over USB CDC serial via the
protocol described in `PROTOCOL.md`; it does not bit-bang either device
directly.

## ESP32-S3 touchscreen + joystick + 4 face buttons

**Role:** display and input peripheral. Renders whatever the Pi tells it to
via display frames (draw/blit/backlight) and reports touch, joystick, and
button events back.

**Connected over:** USB CDC serial to the Pi (`transports.esp32` in
`nomad.toml`; mock by default, `serial` at 921600 baud on hardware).

**Not responsible for:** running any part of the agent, storing state that
matters, or making decisions. **It is a display/input peripheral, not a
co-processor** — no application logic runs on it. Its microSD slot, if
populated, holds firmware assets at most; **it is not primary storage**.
The Pi's microSD (or attached storage) is the only durable store (D7).

## RP2040-Zero (USB HID output)

**Role:** presents as a USB HID keyboard/mouse to whatever host it is
physically plugged into, driven by commands from the Pi.

**Connected over:** USB CDC serial to the Pi for control (`transports.
rp2040`, 115200 baud on hardware) and USB HID to the external host it
injects into.

**Not responsible for:** anything bidirectional from the Pi's perspective.
This link is **output-only** — the Pi sends keystroke/pointer commands, the
RP2040 only reports status back.

> **WARNING — keystroke-injection device by construction.**
> The RP2040 exists to type into another computer. That is not a side
> effect to be careful about; it is the entire feature. Accordingly:
> - Every command sent to it is `HID` target kind, `EXTERNAL_DEVICE` risk
>   (D5, D12).
> - `EXTERNAL_DEVICE` / HID output is `never_auto` **in every permission
>   mode, including `auto`** (D14). A mode switch must never turn this
>   device into an unattended keystroke injector.
> - There is no filesystem capability on an `HID` target at all (D12) — a
>   file tool cannot be pointed at it even by accident; the capability
>   check fails before permission logic runs.

## PiSugar S Plus (battery + power)

**Role:** battery power and telemetry — charge percentage, charging state,
button/power events — feeding the Pi's battery policy (D18).

**Connected over:** I2C to the Pi for telemetry; physically sits under/
alongside the Pi as a HAT-style power board.

**Not responsible for:** any decision-making. It reports numbers; `core`
and `agent` decide what to do with them (defer background work below
`low_threshold`, park the session below `critical_threshold` — see D18 and
the Power budget section below).

## Storage

The Pi's own microSD (or attached SSD/USB storage) is primary storage:
SQLite database (D7), workspace root for filesystem tools (D15), config,
logs. Nothing else in the system holds durable state that matters. The
ESP32-S3's microSD slot, if present, is not this.

## Future expansion: camera, IMU, ToF, SDR, GPS

Not present in the MVP. Each will follow the same pattern as existing
devices: a driver behind a `Component`-shaped interface, a mock
implementation, a `driver = "mock" | "..."` config key, and a `Target`/tool
surface if the agent needs to act on it (e.g. `capture_photo`). None of
these are co-processors — they are sensors/peripherals feeding data to the
Pi, same as the ESP32-S3 and PiSugar.

Expected connections (subject to revision once hardware is selected):
camera via the Pi's CSI connector (`picamera2`, already an optional
dependency), IMU/ToF via I2C, SDR/GPS via USB.

## Onboard inference — an honest section (D17)

This hardware **cannot** host a capable coding-agent model locally. Be
specific about why, because "add an accelerator" sounds like it solves
this and does not:

- **An Edge TPU (Coral) or Hailo NPU is an int8 vision accelerator.** It
  has single-digit megabytes of on-chip memory. It was designed for image
  classification/detection kernels, not autoregressive transformer
  decoding. It **cannot** host a useful language model, full stop — not
  "a small one," none.
- **Realistic onboard inference on this hardware is CPU-only:** a 1–3B
  parameter model quantized to Q4, run via `llama.cpp` on the Pi 4's four
  cores. Budget roughly 1–2 GB RAM and a few tokens/second. That is
  useful for wake-word detection, intent classification, and an offline
  fallback when there's no network — it is **not** useful for agentic
  coding work, which is what the `cloud` provider is for (D17).
- **If onboard agentic capability ever becomes a goal**, the fix is more
  RAM and a better SBC — a Pi 5 with 8–16 GB, or an SBC with a real
  NPU sized for LLM inference (tens to hundreds of MB of usable memory
  bandwidth, not an accelerator stick bolted onto a Pi 4). An accelerator
  stick is not on that path; it solves a different problem (vision).

`ModelRouter` reflects this: `local` is explicitly for intent
classification/offline fallback/short answers, `cloud` is the default for
capable agentic work (D17).

## Power budget and battery policy (D18)

| Signal | Source | Policy response |
|---|---|---|
| `battery.low_threshold` (default 20%) | PiSugar via I2C | Defer background work (compaction, non-urgent polling); prefer `local` model routing where task class allows. |
| `battery.critical_threshold` (default 8%) | PiSugar via I2C | Park the session cleanly — persist in-flight turn state, stop accepting new turns — before shutdown forces the issue. |

The failure mode being designed against is a corrupted partial write mid-
turn, not just data loss. Turn state persists *before* execution (D11), so
even an unplanned power-off — not just the clean-park path above — resumes
or aborts on next boot rather than leaving a half-applied edit.

## Development without hardware (D9)

Every driver category (`display`, `usb_hid`, `battery`, `camera`,
`sensors`, and both transports `esp32`/`rp2040`) has a `mock` implementation
of the same interface as the real driver, and **mock is the config default**
in `nomad.toml`. The full system — including the test suite — runs and
passes on a development laptop with zero hardware attached. Switching a
category to real hardware is a one-line config change
(`driver = "mock"` → `driver = "esp32"`, etc.), not a code change.
