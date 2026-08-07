# Build ledger

Chunk-by-chunk record of the initial build. Append outcomes as they actually
happen; never record a result before the verify command has been run and its
output read. After a context compaction, trust this file and `git log` over
recollection, and never re-dispatch a chunk marked DONE.

| Chunk | Scope | Owns (paths) | Verify | Status |
|---|---|---|---|---|
| A | Foundation: pyproject, gitignore, CLAUDE.md, DECISIONS.md, nomad.toml, ledger | root files, `docs/DECISIONS.md` | files exist | **DONE** |
| B | Remaining docs written against DECISIONS.md | `docs/ARCHITECTURE.md`, `docs/HARDWARE.md`, `docs/PROTOCOL.md`, `docs/ROADMAP.md` | files exist, no contradiction with D1–D18 | **DONE** |
| C | Core: errors, logging, config, events, lifecycle, storage | `src/nomad/core/**`, `src/nomad/storage/**`, `tests/test_events.py`, `tests/test_config.py`, `tests/test_storage.py`, `tests/test_lifecycle.py` | `pytest tests/test_events.py tests/test_config.py tests/test_storage.py tests/test_lifecycle.py` | **DONE** |
| D | Security layer: targets, tools, permissions, agent session | `src/nomad/targets/**`, `src/nomad/tools/**`, `src/nomad/agent/**`, `tests/test_tools.py`, `tests/test_permissions.py`, `tests/test_targets.py`, `tests/test_agent_loop.py` | `pytest tests/test_tools.py tests/test_permissions.py tests/test_targets.py tests/test_agent_loop.py` | TODO |
| E | Peripherals: protocol, transports, hardware, input | `src/nomad/protocol/**`, `src/nomad/hardware/**`, `src/nomad/input/**`, `tests/test_protocol.py`, `tests/test_hardware.py`, `tests/test_input.py` | `pytest tests/test_protocol.py tests/test_hardware.py tests/test_input.py` | TODO |
| F | Wire-up: assistant providers, API, `__main__`, README, layering test | `src/nomad/assistant/**`, `src/nomad/api/**`, `src/nomad/app.py`, `src/nomad/__main__.py`, `README.md`, `tests/test_api.py`, `tests/test_assistant_flow.py`, `tests/test_layering.py` | full `pytest` | TODO |

Ordering: A → (B ‖ C) → (D ‖ E) → F. B and C own disjoint paths, as do D and E.
D and E both require C.

## Notes

**A — DONE.** Design settled in conversation before any code was written. Key
departures from the original brief, all recorded in DECISIONS.md: persistent
agent session (D11), target abstraction with SSH/HID stubs (D12), logical input
layer (D13), Claude-Code-shaped permission modes (D14), and an explicit note that
an Edge TPU cannot replace the cloud model (D17).

**B — DONE.** Four docs written against D1–D18. The agent flagged three genuine
gaps rather than inventing resolutions: the ESP32/RP2040 **message catalogue** and
the **framing byte layout** are both first defined in PROTOCOL.md and are not yet
ratified decisions, and the `Component` protocol shape was inferred from D1/D9.
Treat PROTOCOL.md's catalogue and framing as a first draft. **Action: ratify as a
new decision after chunk E implements them** — do not freeze a wire format on
paper before code exists.

**C — DONE.** `NomadError` tree, structured console/JSON logging, layered
TOML→Pydantic config with injectable env, async `EventBus` with bounded
per-subscriber queues that drop rather than apply backpressure (the load-bearing
D6 property), `Component`/`ComponentRegistry` with reverse-order shutdown and
start-failure rollback, SQLite on a single worker thread with numbered migrations.
Migration 001 also creates the `grants` and `pending_authorizations` tables that
chunk D will use.

Verified by the coordinating session, not self-reported: `pytest tests/test_events.py
tests/test_config.py tests/test_storage.py tests/test_lifecycle.py` → **30 passed
in 5.60s**; `ruff check src tests` → clean. (The subagent's own report was
truncated before it stated a result, so the run was repeated here.)
