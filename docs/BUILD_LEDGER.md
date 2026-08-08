# Build ledger

Chunk-by-chunk record of the build, plus what comes after it. Append outcomes
as they actually happen; **never record a result before the verify command has
been run and its output read.** After a context compaction, trust this file and
`git log` over recollection, and never re-dispatch a chunk marked DONE.

## Continuation record

Read this first after a compaction.

**Objective:** the software skeleton for Nomad — a self-upgrading pocket AI
companion. Claude Code is the agent loop *today*, behind a swappable interface,
with a local LLM over Tailscale as the eventual backend (D19, D24).

**Where things stand:** branch `main`. `core`, `storage`, `targets`, `tools`
(the whole permission pipeline), and now `agent/` and `mcp/` are built and green
— **292 tests passing, ruff clean**, verified by the coordinating session rather
than self-reported. Chunks A–D, G and G2 are DONE. No hardware is wired up yet,
and that is the plan: the entire stack is built and tested against mock drivers,
and the peripherals get soldered on once the software is ready.

**Next action:** chunk E, with two additions the original brief did not have.
First, a **headless display surface** alongside the mock, so app authoring and
the UI shell are demoable with no hardware attached. Second, a **richer display
vocabulary** than `display_text` — `display_card`, `display_list`,
`display_choice` — because that tool schema is the ceiling on what the model can
imagine its own face doing, and every self-authored app inherits it. Also settle
the audio path before ratifying the wire format: PCM does not fit on the display
link (see the review below).

**Machine constraints, learned the hard way:** this laptop has 2 CPUs and
~3.8 GB RAM. One subagent at a time, never two heavy commands at once, and run
tests as `nice -n 19 .venv/bin/python -m pytest <files> -q -p no:cacheprovider`
— no xdist, no type-checker. `pip install claude-agent-sdk` runs alone in the
foreground when chunk G needs it. The `.venv` already has every current
dependency plus `nomad` as an editable install.

**Docs are consolidated to four files** — `CLAUDE.md`, `docs/DECISIONS.md`,
`docs/ARCHITECTURE.md`, `docs/BUILD_LEDGER.md`. `HARDWARE.md`, `PROTOCOL.md`,
`ROADMAP.md` and `start_here.txt` were deleted and folded in; do not recreate
them. More files meant more places for the contract to drift.

## Chunks

| Chunk | Scope | Owns (paths) | Verify | Status |
|---|---|---|---|---|
| A | Foundation: pyproject, gitignore, CLAUDE.md, DECISIONS.md, nomad.toml, ledger | root files, `docs/**`, `src/nomad/core/config.py` | `load_config()` parses `nomad.toml` under `extra="forbid"`; full `pytest` | **DONE** (re-closed post-pivot) |
| B | Remaining docs written against DECISIONS.md | `docs/ARCHITECTURE.md` | consistent with D1–D26 | **DONE** (consolidated) |
| C | Core: errors, logging, config, events, lifecycle, storage | `src/nomad/core/**`, `src/nomad/storage/**`, matching tests | `pytest tests/{test_events,test_config,test_storage,test_lifecycle}.py` | **DONE** |
| D | Security layer: targets, tools, permissions, agent session | `src/nomad/targets/**`, `src/nomad/tools/**`, `src/nomad/agent/**`, matching tests | `pytest tests/{test_targets,test_tools,test_permissions,test_agent_loop,test_workspace}.py` | **DONE** |
| G | **PIVOT (D19–D21, D24):** swappable `AgentBackend`; Claude CLI backend; broker becomes `can_use_tool`; hardware as MCP | `src/nomad/agent/**` (rewrite), `src/nomad/tools/builtin/**` (retire fs tools), `src/nomad/mcp/**`, `tests/test_backend_*.py`, `tests/test_permission_bridge.py`, `tests/test_mcp_hardware.py` | `pytest tests/{test_backend_claude,test_permission_bridge,test_mcp_hardware,test_permissions}.py` | **DONE** |
| G2 | **Review fallout (D27, D28):** egress classification of shell commands; Nomad's identity appended to the preset | `src/nomad/tools/egress.py`, `src/nomad/agent/identity.py`, `NOMAD.md`, `tests/test_egress.py` | `pytest tests/{test_egress,test_permission_bridge,test_backend_claude}.py` | **DONE** |
| E1 | **Protocol (D30):** framing with resync, JSON codec, transports, `Link` with reboot detection | `src/nomad/protocol/**`, `tests/test_protocol.py` | `pytest tests/test_protocol.py` | **DONE** |
| E2 | Hardware drivers: headless + ESP32 display, PiSugar battery, driver selection; display vocabulary (`display_card`/`display_list`/`display_choice`), `get_context` | `src/nomad/hardware/**`, `src/nomad/mcp/hardware.py`, `tests/test_hardware.py` | `pytest tests/{test_hardware,test_mcp_hardware}.py` | **DONE** |
| E3 | Input: logical action layer, deadzone + hysteresis, repeat vs edge-trigger on one stream, extensible action set (D13, D26) | `src/nomad/input/**`, `tests/test_input.py` | `pytest tests/test_input.py` | **DONE** |
| M | **Memory Nomad owns:** durable owner memory + `remember`/`recall`/`forget` MCP tools; session rollover carries it forward | `src/nomad/memory/**`, migration, `tests/test_memory.py` | `pytest tests/test_memory.py` | TODO |
| N | **Presence:** durable notification queue (never the lossy `EventBus`), ambient-context tool (time, battery, charging, network, motion) | `src/nomad/notifications/**`, `src/nomad/mcp/context.py`, matching tests | `pytest tests/{test_notifications,test_context}.py` | TODO |
| V | **Voice:** `Recorder`/`Speaker`/`Transcriber` protocols with mock defaults, `push_to_talk` action, audio on the Pi — never the ESP32 link | `src/nomad/audio/**`, `tests/test_audio.py` | `pytest tests/test_audio.py` | TODO |
| P | **Proactivity:** turn provenance (`user`/`timer`/`sensor`/`self`), `AgentSession.impulse()`, trigger layer, foreground/background lanes | `src/nomad/triggers/**`, `src/nomad/agent/session.py`, `tests/test_triggers.py` | `pytest tests/{test_triggers,test_agent_session}.py` | TODO |
| I | **Self-upgrade (D25, D26, D29):** app registry + manifest + **out-of-process** supervisor; settings service with validation, audit, revert | `src/nomad/apps/**`, `src/nomad/settings/**`, `tests/test_apps.py`, `tests/test_settings.py` | `pytest tests/{test_apps,test_settings}.py` | TODO |
| F | Wire-up: API, `__main__`, composition root, layering test, README | `src/nomad/api/**`, `src/nomad/app.py`, `src/nomad/__main__.py`, `README.md`, `tests/test_api.py`, `tests/test_layering.py` | full `pytest` | TODO |
| H | Delivery (D22, D23): `scripts/setup.sh`, systemd unit, self-update with rollback | `scripts/**`, `src/nomad/selfupdate/**`, `tests/test_selfupdate.py` | `pytest tests/test_selfupdate.py`, shellcheck | TODO |

Ordering: A → B → C → D → G → G2 → **E** → M → N → V → P → I → F → H.
**Sequential only** — this laptop cannot afford concurrent subagents. I needs E,
because apps draw to the display and consume logical input. M, N, V and P were
added after the adversarial review below and are not optional polish: without
them the finished device is a Claude Code terminal in a Game Boy shell, and the
shell makes it worse than the laptop.

## The adversarial review (2026-08-08)

An adversarial critic read the four docs and the source after chunk G landed and
was asked one question: would a stranger holding the finished device think it
was science fiction? **Verdict: no.** The reasoning is worth keeping, because it
is a critique of the *plan*, not of the code, and the code is fine:

> They press a button. Nothing accepts text — D13's logical actions cannot
> express a character, so they thumb an on-screen keyboard on a 320×240 screen
> with a joystick. Then they wait a full Claude Code turn for "what time is it".
> Then the answer arrives as laptop-sized prose in the one display tool that
> exists. Then it goes in a pocket and does nothing, ever, until poked again. It
> never speaks first, has no memory of them it owns, and does not know the time.

The device-specific surface — the only thing a laptop cannot do — was three
tools: `display_text`, `read_battery`, `hid_type_text`.

What that produced, in order of leverage:

1. **Voice (chunk V).** A keyboardless device with no voice has no input method;
   every other improvement is downstream of the operator being able to speak.
   Audio runs on the Pi, **not** over the ESP32 link — 16 kHz mono is 256 kbps
   before framebuffer deltas, and ARCHITECTURE.md's draft protocol currently puts
   `audio.mic_stream` on the same 921600-baud CDC link as `display.draw`. Decide
   the audio path before ratifying that table.
2. **Memory Nomad owns (chunk M, D30).** D19 retired `agent/context.py` because
   Claude Code compacts better. True for a coding session, false for "remembers
   the owner across two years". Today the owner's long-term memory *is* the
   Claude Code transcript — compacted by rules Nomad does not control, stored
   outside Nomad's SQLite, and destroyed by a session-id rotation or a CLI
   upgrade. That was recorded as one decision and it was two.
3. **Identity and a real display vocabulary (D28, done).** Half of this shipped
   in G2. The other half remains: `display_text` is the whole face, and a tool
   schema is the ceiling on what the model can imagine its face doing. Add
   `display_card`, `display_list`, `display_choice` in chunk E.
4. **Proactivity (chunk P).** `AgentSession.send()` is the only way a turn
   begins, so nothing can start one the operator did not type — and if it could,
   the transcript could not tell the difference. Turn provenance is cheap now and
   expensive once the transcript format is set.
5. **Durable notifications (chunk N).** D6 makes `EventBus` drop slow
   subscribers on purpose. Correct for a display, catastrophic for "your build
   failed". Notifications must be a durable row, and nothing currently
   distinguishes the two.
6. **A designed escape hatch for `never_auto` Bash.** `never_auto=True` is
   hard-coded on the `Bash` spec and cannot be relaxed by config at all. On day
   three of real use somebody will delete that line, and they will delete it
   badly. Design the relaxation as a reviewable `CommandPolicy` — argv[0]
   allowlist, no shell metacharacters, cwd inside the workspace — consulted
   inside `tools/permissions.py`. **It must consult D27's egress classifier**,
   or relaxing Bash reopens the SSH hole G2 just closed.
7. **Ambient context (chunk N).** "Understands my needs" is mostly knowing what
   time it is and whether the operator is moving. Nearly free, fully mockable,
   and it is what lets a trigger fire sensibly instead of at random.
8. **Out-of-process apps (D29, decided).** In-process model-authored apps void
   the entire broker. Fixed on paper before chunk I writes a line.
9. **Demoable with no hardware.** The ledger put I after E, which parks the
   flagship demo — "make me a game" — behind unbuilt hardware. Chunk E must ship
   a headless/web display surface so app authoring is exercisable today.
10. **An offline path.** No network means the device is a brick. A small local
    model for wake-word, timers, notes and notification readout is the
    difference between "alive" and "off". Note that deleting `[ai]` in chunk A's
    re-close removed the bad `api_key_env` **and** the model-routing concept;
    routing now has no home in the config model.

Two findings were code, not plan, and are fixed: the SSH `never_auto` bypass
(D27) and the unimplemented identity (D28). Both shipped in G2 with tests.

**Chunk G supersedes part of D.** `agent/loop.py` and `agent/context.py` are
retired by D19; the permission pipeline, targets and all of `core` carry over
intact. Do not treat the retirement as lost work — the expensive half of D was
the broker, and it survives with *more* weight than before (D21).

## Notes

**A — DONE.** Design settled in conversation before any code was written. Key
departures from the original brief, all recorded in DECISIONS.md: persistent
agent session (D11), target abstraction with SSH/HID stubs (D12), logical input
layer (D13), Claude-Code-shaped permission modes (D14), and an explicit note
that an Edge TPU cannot replace the cloud model (D17).

**A — re-closed after the pivot.** The foundation files were written against
D1–D18 and went stale the moment D19–D26 landed, so chunk A was DONE against a
contract that no longer existed. Brought current before G starts, because G
writes the backend that reads this config:

- `pyproject.toml`: `anthropic` extra replaced by `agent = ["claude-agent-sdk>=0.2.132"]`.
  Optional, because `mock` is the default backend and the suite must run with no
  CLI and no credentials (D9, D24).
- `nomad.toml` + `core/config.py`: added `[agent].backend` (`mock` default),
  `[agent.claude_cli]` (cli path, pinned version, OAuth **env var name** — never
  the token), `[agent.remote_llm]` (empty, tailnet placeholder), `[apps]` (D25),
  `[settings]` (D26), `[input].extra_actions` (D26 — `ASSISTANT` is registered
  config, not a special case).
- **`[ai]` removed entirely.** `ai.provider` and `agent.backend` were two names
  for one choice, and `ai.cloud.api_key_env = "ANTHROPIC_API_KEY"` pointed at the
  exact variable D20 requires to be *unset*. Leaving it would have been a config
  surface that bills per token by following its own documentation. D17's routing
  question is deferred until a second backend exists to route between.

`NomadConfig` is `extra="forbid"`, so the file and the model cannot drift apart
silently — a stale key is a startup failure, not a silent default. Verified this
session: `load_config()` returns the new fields, full suite **183 passed in
62.74s**, ruff clean. No code referenced the removed `ai` config (grepped).

**B — DONE, then consolidated.** Four docs were written against D1–D18. Three
were deleted and folded into `ARCHITECTURE.md` (hardware, protocol) and this
file (roadmap, gaps), because six documents meant six places for the contract to
drift and `ARCHITECTURE.md` had already drifted — it described
`Tool.run(grant, target, params)` and a broker living in `agent/`, neither of
which was ever true. It is now written from the actual source signatures, with a
note saying so.

The **wire format remains a draft**, not a decision. Ratify it into DECISIONS.md
once chunk E implements it — do not freeze a wire format on paper before code
exists.

**C — DONE.** `NomadError` tree, structured console/JSON logging, layered
TOML→Pydantic config with injectable env, async `EventBus` with bounded
per-subscriber queues that drop rather than apply backpressure (the load-bearing
D6 property), `Component`/`ComponentRegistry` with reverse-order shutdown and
start-failure rollback, SQLite on a single worker thread with numbered
migrations. Migration 001 also creates the `grants` and `pending_authorizations`
tables chunk D uses.

Verified by the coordinating session, not self-reported: `pytest` on the four
core test files → **30 passed in 5.60s**; ruff clean. (The subagent's own report
was truncated before it stated a result, so the run was repeated here.)

**D — DONE.** Targets with `LocalTarget` real and SSH/HID raising
`NotImplementedError`; capability checks reject a file tool aimed at HID before
permission logic runs; workspace boundary defeats `..`, absolute paths and
symlink escape; four-stage pipeline where `ToolExecutor.run(grant, request)`
recomputes scope from the request so an approved workspace write cannot be
replayed against `/etc`; `never_auto` enforced both before mode branching and
again in `mint_grant()`. Verified: full suite **183 passed in 33.09s**, ruff
clean.

Open items raised by chunk D, still unresolved:

- Config gaps handled with constructor defaults rather than TOML keys:
  `agent.authorization_timeout_seconds`, `agent.grant_ttl_seconds`.
  `agent.context_window` is mooted — Claude Code owns the context window now.
- `Workspace.resolve` is check-then-open, so TOCTOU-racy if an untrusted process
  ever gains write access inside the workspace. Documented in-module.
- Judgement call to review: switching mode *to* `manual` revokes standing
  session grants by default. D14 does not specify this.
- D14-vs-D15 tension on paths outside the workspace is now settled by D21: the
  workspace root is a policy line, not a hard wall, and `never_auto` carries the
  weight.

**PIVOT — after chunk D, before chunk E.** User direction changed the target:
run on the Max-plan subscription rather than per-token API billing, match Claude
Code's coding competence, and eventually self-modify. All three resolve to
running Claude Code headless as the loop. Recorded as D19–D23. Verified against
CLI 2.1.224 *before* designing: `--bare` would have broken this outright (it
never reads OAuth), `setup-token` and `CLAUDE_CODE_OAUTH_TOKEN` are real,
`claude-agent-sdk` 0.2.132 is on PyPI.

Then corrected once more before any code was written: "swap to a local LLM over
Tailscale" means Claude Code cannot *be* the loop, it has to be *a* loop behind
an interface (D24), and "self-upgrading" means authoring apps and changing
settings (D25, D26), not rewriting core. Cheap to fix on paper; expensive once
the SDK is threaded through the codebase.

## What comes after the skeleton

Ordered by dependency, not calendar. Deliberately no dates.

| Phase | Depends on | Content |
|---|---|---|
| Real ESP32-S3 transport + display | chunk E's `protocol` | `serial` transport kind (pyserial-asyncio), real `esp32` display driver, reconnect/resync exercised against a real MCU reboot rather than the mock |
| Real input hardware | above + D13 | Firmware reporting touch/joystick/button; deadzone and repeat timing validated at real event rates |
| UI shell with controller navigation | real display + input | Navigable UI driven entirely by the logical action stream; on-device view of session state and pending grants |
| Apps and games in anger | UI shell | D25's authoring path used for real; games need hold-to-move `REPEAT` where menus need edge-triggering, both on the same stream without forking it |
| SSH target | chunk G's tool path | Real implementation behind the `SSH` stub with its own remote identity. Because tools were written against `Target` from day one, this should not touch the agent loop — that is the whole bet of D12 |
| HID target | above + RP2040 link | Real `rp2040` driver sending `hid.key`/`hid.pointer`; `never_auto` exercised against a real external host |
| Camera / sensors | D9 driver pattern | `picamera2` driver, IMU/ToF as selected, with tools declared at appropriate risk — no special-casing capture in the broker |
| `remote_llm` backend | D24 | A model on the tailnet. Note the asymmetry: it brings no loop, no tools, no compaction, so Nomad must supply all three — roughly what the retired `agent/loop.py` did |

## Known gaps

Deliberately deferred, not forgotten, and not scheduled above.

- **The HTTP API has no authentication and binds to localhost only.** This
  **must** be solved before the API is exposed on any network beyond the device.
  It is a prerequisite for anything off-device (a companion app), not an
  incidental cleanup.
- **Plugin entry-point discovery.** Registries take explicit registration only.
- **Multi-session / multi-user concurrency.** One session per device;
  `AgentSession` is not designed for concurrent sessions sharing a device.
- **OTA firmware update** for the ESP32-S3 and RP2040. Today firmware is a
  manual flash step, not a Pi-driven protocol message.
- **Encryption at rest** for SQLite (D7). Turn history, tool results and grants
  are plaintext on the Pi's storage.
