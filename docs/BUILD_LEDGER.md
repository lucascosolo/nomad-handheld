# Build ledger

Chunk-by-chunk record of the initial build. Append outcomes as they actually
happen; never record a result before the verify command has been run and its
output read. After a context compaction, trust this file and `git log` over
recollection, and never re-dispatch a chunk marked DONE.

| Chunk | Scope | Owns (paths) | Verify | Status |
|---|---|---|---|---|
| A | Foundation: pyproject, gitignore, CLAUDE.md, DECISIONS.md, nomad.toml, ledger | root files, `docs/DECISIONS.md`, `src/nomad/core/config.py` | `load_config()` parses `nomad.toml` under `extra="forbid"`; full `pytest` | **DONE** (re-closed post-pivot) |
| B | Remaining docs written against DECISIONS.md | `docs/ARCHITECTURE.md`, `docs/HARDWARE.md`, `docs/PROTOCOL.md`, `docs/ROADMAP.md` | files exist, no contradiction with D1–D18 | **DONE** |
| C | Core: errors, logging, config, events, lifecycle, storage | `src/nomad/core/**`, `src/nomad/storage/**`, `tests/test_events.py`, `tests/test_config.py`, `tests/test_storage.py`, `tests/test_lifecycle.py` | `pytest tests/test_events.py tests/test_config.py tests/test_storage.py tests/test_lifecycle.py` | **DONE** |
| D | Security layer: targets, tools, permissions, agent session | `src/nomad/targets/**`, `src/nomad/tools/**`, `src/nomad/agent/**`, `tests/test_tools.py`, `tests/test_permissions.py`, `tests/test_targets.py`, `tests/test_agent_loop.py` | `pytest tests/{test_targets,test_tools,test_permissions,test_agent_loop,test_workspace}.py` | **DONE** |
| G | **PIVOT (D19–D21, D24):** swappable `AgentBackend`; Claude CLI backend; broker becomes `can_use_tool`; hardware as MCP | `src/nomad/agent/**` (rewrite), `src/nomad/tools/builtin/**` (retire fs tools), `src/nomad/mcp/**`, `tests/test_backend_*.py`, `tests/test_permission_bridge.py`, `tests/test_mcp_hardware.py` | `pytest tests/{test_backend_claude,test_permission_bridge,test_mcp_hardware,test_permissions}.py` | TODO |
| I | **Self-upgrade (D25, D26):** app registry + manifest + supervisor; settings service with validation, audit, revert; extensible action set | `src/nomad/apps/**`, `src/nomad/settings/**`, `tests/test_apps.py`, `tests/test_settings.py` | `pytest tests/{test_apps,test_settings}.py` | TODO |
| E | Peripherals: protocol, transports, hardware, input | `src/nomad/protocol/**`, `src/nomad/hardware/**`, `src/nomad/input/**`, `tests/test_protocol.py`, `tests/test_hardware.py`, `tests/test_input.py` | `pytest tests/{test_protocol,test_hardware,test_input}.py` | TODO |
| F | Wire-up: API, `__main__`, composition root, layering test, README | `src/nomad/api/**`, `src/nomad/app.py`, `src/nomad/__main__.py`, `README.md`, `tests/test_api.py`, `tests/test_layering.py` | full `pytest` | TODO |
| H | Delivery (D22, D23): `scripts/setup.sh`, systemd unit, self-update with rollback | `scripts/**`, `src/nomad/selfupdate/**`, `tests/test_selfupdate.py` | `pytest tests/test_selfupdate.py`, shellcheck | TODO |

Ordering: A → B → C → D → **G** → E → I → F → H. Sequential only — this laptop
cannot afford concurrent subagents (2 CPUs, ~1.8 GB free). I needs E (apps draw
to the display and consume logical input).

**Chunk G supersedes part of D.** `agent/loop.py` and `agent/context.py` are
retired by D19; the permission pipeline, targets and `core` all carry over intact.
Do not treat the retirement as lost work — the expensive half of D was the broker,
and it survives with more weight than before (D21).

## Notes

**A — DONE.** Design settled in conversation before any code was written. Key
departures from the original brief, all recorded in DECISIONS.md: persistent
agent session (D11), target abstraction with SSH/HID stubs (D12), logical input
layer (D13), Claude-Code-shaped permission modes (D14), and an explicit note that
an Edge TPU cannot replace the cloud model (D17).

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

**D — DONE.** Targets with `LocalTarget` real and SSH/HID raising
`NotImplementedError`; capability checks reject a file tool aimed at HID before
permission logic runs; workspace boundary defeats `..`, absolute paths and symlink
escape; four-stage pipeline where `ToolExecutor.run(grant, request)` recomputes
scope from the request so an approved workspace write cannot be replayed against
`/etc`; `never_auto` enforced both before mode branching and again in
`mint_grant()`. Verified by this session: full suite **183 passed in 33.09s**,
ruff clean.

Open items raised by chunk D, still unresolved:
- `ARCHITECTURE.md` disagrees with the built shapes (shows `Tool.run(grant, target,
  params)` and puts the broker in `agent/`). **Doc needs correcting in chunk F.**
- Config gaps handled with constructor defaults rather than TOML edits:
  `agent.context_window`, `agent.authorization_timeout_seconds`,
  `agent.grant_ttl_seconds`. Several are mooted by D19 (Claude Code owns the
  context window now).
- `Workspace.resolve` is check-then-open, so TOCTOU-racy if an untrusted process
  ever gains write access inside the workspace. Documented in-module.
- Judgement call to review: switching mode *to* `manual` revokes standing session
  grants by default. D14 does not specify this.

**PIVOT — after chunk D, before chunk E.** User direction changed the target:
Nomad should run on the Max-plan subscription rather than per-token API billing,
should match Claude Code's coding competence, and should eventually modify itself.
All three resolve to running Claude Code headless as the loop. Recorded as D19–D23.
Verified against CLI 2.1.224 before designing: `--bare` would have broken this
outright (it never reads OAuth), `setup-token` and `CLAUDE_CODE_OAUTH_TOKEN` are
real, `claude-agent-sdk` 0.2.132 is on PyPI. User chose full Claude Code tools
gated by Nomad's broker (not `--add-dir` confinement), and to pivot immediately
rather than finish E/F against the old shape.
