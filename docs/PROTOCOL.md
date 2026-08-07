# Protocol

The wire format and message catalogue for Nomad's two hardware links: Pi ↔
ESP32-S3 (display/input) and Pi ↔ RP2040-Zero (HID output). Both run over
USB CDC serial.

## Three concerns, three objects (D2)

| Object | Owns | Knows nothing about |
|---|---|---|
| `Framing` | Delimiting a byte stream into discrete frames | Message meaning |
| `Codec` | `Message` ↔ `bytes` | How bytes move |
| `Transport` | Moving `bytes` | Message meaning, framing internals |

A `Transport` never inspects payload content to make a routing decision —
that would collapse the separation and re-couple the link to a specific
encoding.

**Why JSON, and why it's not permanent:** JSON is the starting point
because it's trivial to debug over a serial console and cheap to implement
on both the Pi and the ESP32-S3/RP2040 firmware. It is explicitly *not*
load-bearing as a long-term choice (D2) — when the display link needs
binary framebuffer deltas, or bandwidth pressure on USB CDC becomes real,
the `Codec` is swapped for a binary/CBOR one. No `Transport` or device
driver code changes, because they only ever dealt in `bytes` and
`Message` objects respectively.

## Frame structure (D3)

Every frame carries four fields:

| Field | Purpose |
|---|---|
| `type` | Message type string (see catalogues below) |
| `id` | Correlates a request with its response |
| `seq` | Monotonic per direction; detects loss or an MCU reboot mid-stream |
| `payload` | Type-specific body |

**Why both `id` and `seq`:** microcontrollers reboot and USB re-enumerates.
`id` alone can't tell a late response from a wrong one after a reboot reset
its counters; `seq` resets to a low value on MCU reboot, which is itself
the signal the Pi uses to detect it happened.

**Reconnect / resync:** on transport reconnect (USB re-enumeration or
explicit `hello`), the Pi treats a `seq` that is lower than the last seen
value as evidence of an MCU reboot, discards any requests it was waiting
on for that link as unanswered, and re-establishes state via the `hello`/
`status` exchange (see catalogues) rather than assuming continuity.

**Timeouts and retry safety:** every request has a bounded timeout. Retries
are only safe because `id` correlates response to request — a duplicate
send after a timeout is distinguishable from the original if both
responses eventually arrive. Idempotent requests (e.g. `status`) may be
retried freely; requests with side effects on the MCU are not retried
blindly without checking `seq` continuity first.

## Framing format: length-prefixed, checksummed, over USB CDC

| Bytes | Field | Notes |
|---|---|---|
| 4 | `length` (u32, LE) | Length of `frame_body` in bytes, not including this field or the checksum |
| `length` | `frame_body` | Codec-encoded `Message` (JSON today, per D2) |
| 4 | `checksum` (CRC32, LE) | Computed over `frame_body` only |

Delimiting is by length prefix, not a sentinel byte — this avoids the need
to escape the delimiter if it appears in `frame_body` (relevant if/when the
codec goes binary). The checksum catches USB CDC corruption; a checksum
failure is treated as frame loss (bumps expected `seq`, does not crash the
link).

## Message catalogue: Pi ↔ ESP32-S3

Grouped by concern. `type` strings are illustrative of the convention
(`<group>.<action>`), not a frozen enumeration.

### Display

| Type | Direction | Payload sketch |
|---|---|---|
| `display.draw` | Pi → ESP32 | `{ x, y, w, h, pixels }` — raw or RLE region |
| `display.blit` | Pi → ESP32 | `{ x, y, region_id }` — pre-sent region to a screen position |
| `display.backlight` | Pi → ESP32 | `{ level: 0-255 }` |

### Input

| Type | Direction | Payload sketch |
|---|---|---|
| `input.touch` | ESP32 → Pi | `{ x, y, phase: down\|move\|up }` |
| `input.joystick` | ESP32 → Pi | `{ x: -1.0..1.0, y: -1.0..1.0 }` |
| `input.button` | ESP32 → Pi | `{ button: a\|b\|x\|y, phase: press\|release }` |

Raw values above are physical/device-local; they are normalized into the
logical `InputEvent` stream (D13) immediately on receipt by `nomad.input`
and never referenced by button/pin identity above that layer.

### Audio

| Type | Direction | Payload sketch |
|---|---|---|
| `audio.mic_stream` | ESP32 → Pi | `{ seq, samples }` — chunked PCM |
| `audio.speaker` | Pi → ESP32 | `{ seq, samples }` — chunked PCM |

### System

| Type | Direction | Payload sketch |
|---|---|---|
| `system.hello` | either | `{ firmware_version, capabilities }` — sent on connect/reconnect |
| `system.ping` | either | `{}` |
| `system.status` | ESP32 → Pi | `{ uptime_ms, free_heap, last_seq_seen }` |
| `system.error` | either | `{ code, message }` |

## Message catalogue: Pi ↔ RP2040-Zero

This link is **output-only from the Pi's perspective** — the Pi issues
commands, the RP2040 only reports status. Every non-status message here is
`EXTERNAL_DEVICE` risk and `never_auto` in every permission mode (D14),
because its entire function is injecting input into whatever host it's
plugged into (see `HARDWARE.md`).

| Type | Direction | Payload sketch |
|---|---|---|
| `hid.key` | Pi → RP2040 | `{ keycode, phase: press\|release }` |
| `hid.pointer` | Pi → RP2040 | `{ dx, dy, buttons }` |
| `system.status` | RP2040 → Pi | `{ uptime_ms, host_connected: bool }` |

No `input.*` messages flow on this link — it has no sensors of its own.

## The logical input contract (D13)

Raw device events (`input.touch`, `input.joystick`, `input.button` above,
plus any future device) are normalized at the `nomad.input` boundary into:

```
NAV_UP | NAV_DOWN | NAV_LEFT | NAV_RIGHT | CONFIRM | BACK | ACTION_1 | ACTION_2
```

each with a phase: `PRESS | RELEASE | REPEAT` (hold support for games,
edge-triggering for menus).

The physical → logical mapping lives in TOML (`[input.buttons]`,
`[input.joystick]` in `nomad.toml`). **No application code above the input
layer may reference a GPIO pin, a button index, or a raw key code** — UI
and games consume the logical stream exclusively. This is what lets the
same menu code run against a touchscreen, a keyboard, or a future
controller without a fork.

## Extending the protocol without breaking older firmware

1. **New message types are additive.** A receiver that doesn't recognize a
   `type` string ignores the frame (after checksum/framing validation) and
   optionally reports it via `system.error` — it does not crash or desync
   `seq`.
2. **New fields on an existing payload are additive and optional.** Old
   firmware parsing a payload with an unknown trailing field ignores it
   (this is a property the codec must preserve — plain JSON does this for
   free; a future binary codec must be designed to preserve it too, e.g.
   via explicit field tags rather than positional encoding).
3. **Never repurpose an existing `type` string for a different payload
   shape.** Mint a new type string (`display.draw_v2`) instead, and let
   `system.hello`'s `capabilities` field tell the Pi which version the
   connected firmware speaks.
4. **Breaking wire changes** (D3's `type`/`id`/`seq`/`payload` envelope
   itself, or the framing format) are the one thing that requires updating
   both sides in lockstep — there is no negotiation below the envelope
   level, so this path is deliberately rare and costly (D3: "High — it is
   a wire format change on both sides of two links").
