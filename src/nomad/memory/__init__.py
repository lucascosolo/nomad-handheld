"""A memory Nomad owns: small facts, durably stored, bounded on injection.

Nomad is a persistent companion, and until this package existed it forgot
everything at a session boundary. `NOMAD.md` said so out loud.

The shape is deliberate and it is the opposite of "carry the context along":
memory is a **searchable index that is retrieved from**, not a payload. Only
pinned memories — capped, scarce, deliberate — are injected at session start,
alongside one constant-size line naming what else is retrievable. Everything
else is reached with `recall`. A briefing that grew with the store would be a
context leak charged on every turn, and an overloaded window degrades the work
the device exists to do.

Layering: this package imports `core` and `storage` and nothing above them
(D-layering in `CLAUDE.md`). The MCP tools that expose it live in
`mcp/memory.py`; the injection wiring lives in `agent/`.
"""

from nomad.memory.briefing import HEADING, compose_briefing, format_index
from nomad.memory.errors import MemoryRefused
from nomad.memory.models import (
    MAX_TEXT_CHARS,
    Memory,
    MemoryKind,
    normalize_keywords,
    normalize_text,
)
from nomad.memory.redaction import looks_like_secret
from nomad.memory.rollover import RolloverDecision, should_roll
from nomad.memory.store import MemoryStore

__all__ = [
    "HEADING",
    "MAX_TEXT_CHARS",
    "Memory",
    "MemoryKind",
    "MemoryRefused",
    "MemoryStore",
    "RolloverDecision",
    "compose_briefing",
    "format_index",
    "looks_like_secret",
    "normalize_keywords",
    "normalize_text",
    "should_roll",
]
