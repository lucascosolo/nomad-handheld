"""What a memory is, and how its text is normalized for dedup and recall.

A memory is deliberately *small*: one fact, one or two sentences. The unit
matters more than it looks — a memory the size of a paragraph cannot be
injected at session start without eating the whole budget, cannot be scored
against a query without matching everything, and cannot be forgotten without
losing four other things along with it.

Normalization is here rather than in the store because it defines record
identity: two memories with the same `normalized_text` *are* the same memory,
and the UNIQUE constraint in migration 002 enforces exactly that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

_WHITESPACE = re.compile(r"\s+")

#: Trailing punctuation stripped before comparison, so "He prefers vim." and
#: "he prefers vim" are one fact rather than two.
_TRAILING = ".!?,;:-— \t"

#: The hard ceiling on one fact. A memory is an index entry, not a document:
#: at 280 a single row cannot swallow the injection budget, and anything
#: longer is two facts that should be stored — and forgotten — separately.
MAX_TEXT_CHARS = 280


class MemoryKind(StrEnum):
    """What sort of fact this is.

    Used for filtered recall and to label a line in the injected briefing. Kept
    to four: enough for the model to file something sensibly, few enough that
    it does not spend a turn choosing.
    """

    OPERATOR = "operator"
    PREFERENCE = "preference"
    PROJECT = "project"
    FACT = "fact"


class Memory(BaseModel):
    """One remembered fact, as stored and as handed back."""

    id: str
    text: str = Field(max_length=MAX_TEXT_CHARS)
    kind: MemoryKind = MemoryKind.FACT
    keywords: list[str] = Field(default_factory=list)
    #: Pinned memories are the *only* ones injected at session start, and they
    #: are capped. Everything else is reached through `recall`, which is the
    #: whole point: the injected block must stay near-constant in size however
    #: large the store grows, or it becomes a context leak paid on every turn.
    pinned: bool = False
    created_at: datetime
    updated_at: datetime
    last_recalled_at: datetime | None = None
    recall_count: int = 0
    source_session_id: str | None = None
    #: Set by `forget`. The row stays — forgetting is a soft delete, because a
    #: memory the model can silently and permanently destroy would be the one
    #: piece of state on this device with no audit trail.
    forgotten_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.forgotten_at is None


def normalize_text(text: str) -> str:
    """Fold a fact to its identity: casefolded, single-spaced, unpunctuated."""
    collapsed = _WHITESPACE.sub(" ", text).strip().casefold()
    return collapsed.strip(_TRAILING).strip()


def normalize_keywords(keywords: Iterable[str]) -> list[str]:
    """Lowercase, de-duplicate and drop empties, preserving first-seen order."""
    seen: dict[str, None] = {}
    for keyword in keywords:
        cleaned = _WHITESPACE.sub(" ", keyword).strip().casefold().strip(_TRAILING)
        if cleaned:
            seen[cleaned] = None
    return list(seen)


def merge_keywords(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    """Union, order-stable. Re-remembering a fact never loses a keyword."""
    return normalize_keywords([*existing, *incoming])
