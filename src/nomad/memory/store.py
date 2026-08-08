"""The memory Nomad owns: remember, recall, forget (D7 storage conventions).

Async, over a `Database`, in the style of `storage/repositories/`. It imports
`core` and `storage` and nothing above them.

**Scoring is Python, not SQL, and not FTS5.** The corpus is a few hundred rows
on a Pi; a linear scan over that is microseconds, and the ranking rules —
keyword hits beat body substring hits, pinned breaks ties — are easier to read
and to test as code than as a relevance expression. FTS5 was rejected
explicitly: it may be absent from a system sqlite build, the failure appears
only on the device, and rebuilding an index offline on a handheld is exactly
the maintenance no one will do. See the module docstring in
`storage/migrations.py` for the schema side of the same decision.

**Two ways a memory leaves.** `forget` is the model's, and it is a soft delete
— the row keeps its text and gains a `forgotten_at`, so every belief the
device ever held stays auditable. Eviction under the configured cap is the
store's, and it is a real delete: it is bookkeeping the model did not ask for,
it must free the UNIQUE slot, and it is logged at INFO when it happens. A
pinned memory is never evicted; if every row is pinned the store refuses to
take a new one rather than quietly dropping something.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from nomad.core.config import MemoryConfig
from nomad.core.logging import get_logger
from nomad.memory.errors import MemoryRefused
from nomad.memory.models import (
    MAX_TEXT_CHARS,
    Memory,
    MemoryKind,
    merge_keywords,
    normalize_keywords,
    normalize_text,
)
from nomad.memory.redaction import looks_like_secret
from nomad.storage.db import Database

logger = get_logger(__name__)

_KEYWORD_HIT = 3
_TEXT_HIT = 1
#: A query whose every token hit somewhere is a better answer than one that
#: matched half of itself twice. Now that nothing unpinned is injected,
#: retrieval has to actually substitute for carrying the fact along.
_ALL_TOKENS_BONUS = 2


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_memory(row: dict[str, Any]) -> Memory:
    return Memory(
        id=row["id"],
        text=row["text"],
        kind=MemoryKind(row["kind"]),
        keywords=list(json.loads(row["keywords_json"])),
        pinned=bool(row["pinned"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_recalled_at=_parse(row["last_recalled_at"]),
        recall_count=int(row["recall_count"]),
        source_session_id=row["source_session_id"],
        forgotten_at=_parse(row["forgotten_at"]),
    )


class MemoryStore:
    """Durable, deduplicated, capped storage for small facts."""

    def __init__(
        self,
        db: Database,
        *,
        config: MemoryConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config = config or MemoryConfig()
        self._clock = clock or _now

    @property
    def config(self) -> MemoryConfig:
        return self._config

    # -- writing -----------------------------------------------------------

    async def remember(
        self,
        text: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        keywords: Iterable[str] = (),
        pinned: bool = False,
        session_id: str | None = None,
    ) -> Memory:
        """Store one fact, or update the one that says the same thing.

        Raises `MemoryRefused` when the text looks like a credential, when it
        is empty, or when the store is full and everything in it is pinned.
        """
        if len(text.strip()) > MAX_TEXT_CHARS:
            raise MemoryRefused(
                f"this memory is {len(text.strip())} characters; the limit is "
                f"{MAX_TEXT_CHARS}. Split it into separate facts — a memory is an "
                "index entry, not a document.",
                {"length": len(text.strip()), "limit": MAX_TEXT_CHARS},
            )

        matched = looks_like_secret(text)
        if matched is not None:
            raise MemoryRefused(
                f"refusing to remember this: it contains {matched}. "
                "Stored memories are injected into future prompts, so secrets "
                "must live in a credential store, not here.",
                {"matched": matched},
            )

        normalized = normalize_text(text)
        if not normalized:
            raise MemoryRefused("refusing to remember an empty memory")

        now = self._clock()
        existing = await self._find_normalized(normalized)
        if existing is not None:
            if pinned and not existing.pinned:
                await self._enforce_pin_cap()
            return await self._update_existing(
                existing, text=text, kind=kind, keywords=keywords, pinned=pinned, now=now
            )

        if pinned:
            await self._enforce_pin_cap()
        await self._enforce_cap()
        memory = Memory(
            id=uuid.uuid4().hex,
            text=text.strip(),
            kind=kind,
            keywords=normalize_keywords(keywords),
            pinned=pinned,
            created_at=now,
            updated_at=now,
            source_session_id=session_id,
        )
        await self._db.execute(
            "INSERT INTO memories (id, text, normalized_text, kind, keywords_json, pinned, "
            "created_at, updated_at, last_recalled_at, recall_count, source_session_id, "
            "forgotten_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, NULL)",
            (
                memory.id,
                memory.text,
                normalized,
                str(memory.kind),
                json.dumps(memory.keywords),
                int(memory.pinned),
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.source_session_id,
            ),
        )
        logger.info(
            "Remembered", extra={"memory_id": memory.id, "kind": str(kind), "pinned": pinned}
        )
        return memory

    async def _update_existing(
        self,
        existing: Memory,
        *,
        text: str,
        kind: MemoryKind,
        keywords: Iterable[str],
        pinned: bool,
        now: datetime,
    ) -> Memory:
        """Re-remembering the same fact updates it; it never inserts a twin.

        A session that stays alive for weeks re-states the same fact daily.
        Without this the table fills with near-identical rows and the injected
        briefing degenerates into one sentence repeated. `created_at` is kept
        (this is the same belief, first held then), `pinned` only ever goes up
        (a re-mention must not silently unpin), and a forgotten memory that is
        remembered again comes back — otherwise the UNIQUE row would block the
        device from ever re-learning something it once dropped.
        """
        updated = existing.model_copy(
            update={
                "text": text.strip(),
                "kind": kind,
                "keywords": merge_keywords(existing.keywords, keywords),
                "pinned": existing.pinned or pinned,
                "updated_at": now,
                "forgotten_at": None,
            }
        )
        await self._db.execute(
            "UPDATE memories SET text = ?, kind = ?, keywords_json = ?, pinned = ?, "
            "updated_at = ?, forgotten_at = NULL WHERE id = ?",
            (
                updated.text,
                str(updated.kind),
                json.dumps(updated.keywords),
                int(updated.pinned),
                updated.updated_at.isoformat(),
                updated.id,
            ),
        )
        logger.debug("Updated an existing memory", extra={"memory_id": updated.id})
        return updated

    async def _enforce_pin_cap(self) -> None:
        """Refuse a new pin once the pinned set is full. Scarcity is the feature.

        Pinned memories are the only ones injected, so the pinned set *is* the
        size of the context tax memory charges on every turn. Growing it
        silently would make that tax invisible. The refusal names the
        least-recalled pin so the model has a concrete thing to unpin rather
        than a policy to argue with.
        """
        limit = max(0, self._config.max_pinned)
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM memories WHERE pinned = 1 AND forgotten_at IS NULL"
        )
        if (int(row["n"]) if row else 0) < limit:
            return
        weakest = await self._db.fetch_one(
            "SELECT id, text FROM memories WHERE pinned = 1 AND forgotten_at IS NULL "
            "ORDER BY recall_count ASC, updated_at ASC LIMIT 1"
        )
        hint = f' Least used: "{weakest["text"]}" ({weakest["id"]}).' if weakest else ""
        raise MemoryRefused(
            f"already at the limit of {limit} pinned memories. Pinned memories are "
            f"injected into every prompt, so unpin one before pinning another."
            f"{hint} Storing it unpinned instead keeps it findable with recall.",
            {"max_pinned": limit},
        )

    async def _enforce_cap(self) -> None:
        """Make room for one more row, or refuse."""
        limit = max(1, self._config.max_memories)
        while await self._count() >= limit:
            row = await self._db.fetch_one(
                "SELECT id, text FROM memories WHERE pinned = 0 "
                # Already-forgotten rows go first: they are the least valuable
                # thing here and the operator has already seen them dropped.
                "ORDER BY (forgotten_at IS NULL) ASC, recall_count ASC, updated_at ASC LIMIT 1"
            )
            if row is None:
                raise MemoryRefused(
                    f"memory is full ({limit}) and every memory is pinned; "
                    "unpin or forget one before remembering something new",
                    {"max_memories": limit},
                )
            await self._db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
            logger.info(
                "Pruned the least valuable memory to stay under the cap",
                extra={"memory_id": row["id"], "max_memories": limit},
            )

    async def forget(self, memory_id: str) -> bool:
        """Soft-delete. Returns False if it was unknown or already forgotten."""
        existing = await self.get(memory_id)
        if existing is None or existing.forgotten_at is not None:
            return False
        now = self._clock().isoformat()
        await self._db.execute(
            "UPDATE memories SET forgotten_at = ?, updated_at = ? WHERE id = ?",
            (now, now, memory_id),
        )
        logger.info("Forgot a memory", extra={"memory_id": memory_id})
        return True

    # -- reading -----------------------------------------------------------

    async def get(self, memory_id: str) -> Memory | None:
        """Fetch by id, forgotten or not — this is the audit read."""
        row = await self._db.fetch_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return _row_to_memory(row) if row is not None else None

    async def recall(
        self,
        query: str = "",
        *,
        kind: MemoryKind | None = None,
        limit: int | None = None,
    ) -> list[Memory]:
        """Score active memories against `query` and return the best.

        An empty query is not an error: it means "what do you know", and it
        answers with the most valuable recent memories rather than nothing.
        Returned rows have their recall counters bumped, which is what later
        makes eviction able to tell a load-bearing memory from a stale one.
        """
        size = limit if limit is not None else self._config.recall_limit
        size = max(1, size)
        rows = await self._active_rows(kind=kind)
        memories = [_row_to_memory(row) for row in rows]

        tokens = [t for t in normalize_text(query).split(" ") if t]
        if tokens:
            scored = [(self._score(m, tokens), m) for m in memories]
            scored = [(s, m) for s, m in scored if s > 0]
        else:
            scored = [(0, m) for m in memories]

        scored.sort(key=lambda pair: (-pair[0], not pair[1].pinned, -pair[1].updated_at.timestamp(),
                                      pair[1].id))
        chosen = [m for _, m in scored[:size]]
        return await self._bump(chosen)

    async def for_briefing(self, *, limit: int | None = None) -> list[Memory]:
        """The pinned memories, and only those. Never bumps recall counts.

        Pinned-only is the amendment that keeps injection O(1) in store size:
        an unpinned tier would grow the block with the corpus and charge the
        whole session for it. Injection is also not retrieval — counting it
        would make every memory look equally used and destroy the signal
        eviction and the pin-cap refusal both depend on.
        """
        size = limit if limit is not None else self._config.injection_max_memories
        rows = await self._db.fetch_all(
            "SELECT * FROM memories WHERE forgotten_at IS NULL AND pinned = 1 "
            "ORDER BY updated_at DESC LIMIT ?",
            (max(1, size),),
        )
        return [_row_to_memory(row) for row in rows]

    async def unpinned_counts_by_kind(self) -> dict[str, int]:
        """How much is retrievable but not injected, per kind.

        This is what the briefing's one-line index is built from. Counts, never
        contents — the line has to stay the same size whether the store holds
        thirty rows or three thousand.
        """
        rows = await self._db.fetch_all(
            "SELECT kind, COUNT(*) AS n FROM memories "
            "WHERE forgotten_at IS NULL AND pinned = 0 GROUP BY kind"
        )
        return {row["kind"]: int(row["n"]) for row in rows}

    async def count_active(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM memories WHERE forgotten_at IS NULL"
        )
        return int(row["n"]) if row else 0

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _score(memory: Memory, tokens: Sequence[str]) -> int:
        haystack = normalize_text(memory.text)
        keywords = set(memory.keywords)
        score = 0
        hits = 0
        for token in tokens:
            if token in keywords:
                score += _KEYWORD_HIT
                hits += 1
            elif token in haystack:
                score += _TEXT_HIT
                hits += 1
        if score and hits == len(tokens):
            score += _ALL_TOKENS_BONUS
        return score

    async def _bump(self, memories: list[Memory]) -> list[Memory]:
        if not memories:
            return []
        now = self._clock()
        placeholders = ", ".join("?" for _ in memories)
        await self._db.execute(
            f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            (now.isoformat(), *[m.id for m in memories]),
        )
        return [
            m.model_copy(update={"recall_count": m.recall_count + 1, "last_recalled_at": now})
            for m in memories
        ]

    async def _count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM memories")
        return int(row["n"]) if row else 0

    async def _find_normalized(self, normalized: str) -> Memory | None:
        row = await self._db.fetch_one(
            "SELECT * FROM memories WHERE normalized_text = ?", (normalized,)
        )
        return _row_to_memory(row) if row is not None else None

    async def _active_rows(self, *, kind: MemoryKind | None) -> list[dict[str, Any]]:
        if kind is None:
            return await self._db.fetch_all(
                "SELECT * FROM memories WHERE forgotten_at IS NULL"
            )
        return await self._db.fetch_all(
            "SELECT * FROM memories WHERE forgotten_at IS NULL AND kind = ?", (str(kind),)
        )
