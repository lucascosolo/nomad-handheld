"""Notes, and why they are not memories.

Nomad already has a durable store of small facts, so a second one needs a
reason. The reason is *whose text it is*. A memory is something the device
concluded and will inject into its own future prompts, which is why it is
capped at 280 characters, deduplicated by normalized text, refused when it
looks like a credential, and scarce by design (D33). A note is something the
operator wrote down: it is theirs, it is as long as they made it, two notes
saying the same thing are two notes, and nothing here is ever injected into a
prompt. Folding one into the other would have meant either notes inheriting a
context budget they do not spend, or memories losing the scarcity that is the
whole point of them.

Search is a linear scan with the same rationale migration 002 gives for
`memories`: a few hundred rows on a handheld, no FTS5 to be missing from
whatever sqlite a Pi image shipped, nothing to rebuild offline when it corrupts.

Deletion is soft, matching `memories`. There is no undo button on this device,
and a note the operator loses to a mistyped id is the kind of loss that makes
someone stop trusting the thing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nomad.core.config import UtilitiesConfig
from nomad.core.logging import get_logger
from nomad.storage.db import Database
from nomad.utilities.errors import UtilityError

logger = get_logger(__name__)

MAX_NOTE_TITLE_CHARS = 120
MAX_NOTE_BODY_CHARS = 8000


def _now() -> datetime:
    return datetime.now(UTC)


class Note(BaseModel):
    """One thing the operator wrote down."""

    id: str
    title: str = Field(max_length=MAX_NOTE_TITLE_CHARS)
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


def _row_to_note(row: dict[str, Any]) -> Note:
    return Note(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        tags=list(json.loads(row["tags_json"])),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
    )


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = " ".join(tag.split()).casefold()
        if cleaned:
            seen[cleaned] = None
    return list(seen)


class NoteStore:
    """Durable notes over a `Database`. Offline, deterministic, no model."""

    def __init__(
        self,
        db: Database,
        *,
        config: UtilitiesConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config = config or UtilitiesConfig()
        self._clock = clock or _now

    @property
    def preview_chars(self) -> int:
        """How much of a body a search result shows. Config, not a tool literal.

        Search output lands in the model's context on every call, so the
        ceiling belongs with the other context budgets rather than inline at
        the one call site that happens to render it.
        """
        return max(1, self._config.note_preview_chars)

    async def add(
        self, title: str, *, body: str = "", tags: Iterable[str] = ()
    ) -> Note:
        clean_title = " ".join(title.split())
        if not clean_title:
            raise UtilityError("a note needs a title")
        if len(clean_title) > MAX_NOTE_TITLE_CHARS:
            raise UtilityError(
                f"note title is {len(clean_title)} characters; the limit is "
                f"{MAX_NOTE_TITLE_CHARS}. Put the rest in the body.",
                {"limit": MAX_NOTE_TITLE_CHARS},
            )
        if len(body) > MAX_NOTE_BODY_CHARS:
            raise UtilityError(
                f"note body is {len(body)} characters; the limit is {MAX_NOTE_BODY_CHARS}",
                {"limit": MAX_NOTE_BODY_CHARS},
            )
        if await self._count_active() >= max(1, self._config.max_notes):
            raise UtilityError(
                f"there are already {self._config.max_notes} notes; delete one first. "
                "Notes are never evicted automatically — they are the operator's, "
                "not the device's to drop.",
                {"max_notes": self._config.max_notes},
            )
        now = self._clock()
        note = Note(
            id=uuid.uuid4().hex,
            title=clean_title,
            body=body,
            tags=_normalize_tags(tags),
            created_at=now,
            updated_at=now,
        )
        await self._db.execute(
            "INSERT INTO notes (id, title, body, tags_json, created_at, updated_at, "
            "deleted_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                note.id,
                note.title,
                note.body,
                json.dumps(note.tags),
                note.created_at.isoformat(),
                note.updated_at.isoformat(),
            ),
        )
        logger.info("Added a note", extra={"note_id": note.id})
        return note

    async def get(self, note_id: str) -> Note | None:
        row = await self._db.fetch_one("SELECT * FROM notes WHERE id = ?", (note_id,))
        return _row_to_note(row) if row is not None else None

    async def update(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        append: str | None = None,
        tags: Iterable[str] | None = None,
    ) -> Note:
        """Edit in place. `append` exists because that is what a note is for.

        Appending through a read-modify-write at the call site would race with
        itself and lose a line; here it is one statement against one row on the
        single database thread.
        """
        existing = await self.get(note_id)
        if existing is None or existing.deleted_at is not None:
            raise UtilityError(f"no note with id {note_id}", {"note_id": note_id})
        new_body = existing.body if body is None else body
        if append:
            new_body = f"{new_body}\n{append}".strip() if new_body else append.strip()
        if len(new_body) > MAX_NOTE_BODY_CHARS:
            raise UtilityError(
                f"that would make the note {len(new_body)} characters; the limit is "
                f"{MAX_NOTE_BODY_CHARS}",
                {"limit": MAX_NOTE_BODY_CHARS},
            )
        now = self._clock()
        updated = existing.model_copy(
            update={
                "title": " ".join(title.split()) if title else existing.title,
                "body": new_body,
                "tags": _normalize_tags(tags) if tags is not None else existing.tags,
                "updated_at": now,
            }
        )
        await self._db.execute(
            "UPDATE notes SET title = ?, body = ?, tags_json = ?, updated_at = ? WHERE id = ?",
            (
                updated.title,
                updated.body,
                json.dumps(updated.tags),
                now.isoformat(),
                note_id,
            ),
        )
        return updated

    async def delete(self, note_id: str) -> bool:
        """Soft delete. False if it was unknown or already deleted."""
        existing = await self.get(note_id)
        if existing is None or existing.deleted_at is not None:
            return False
        now = self._clock().isoformat()
        await self._db.execute(
            "UPDATE notes SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, note_id),
        )
        return True

    async def search(self, query: str = "", *, limit: int | None = None) -> list[Note]:
        """Substring match over title, body and tags. Newest first.

        An empty query is not an error: it means "what have I written down",
        and it answers with the most recent notes rather than nothing.
        """
        size = max(1, limit if limit is not None else self._config.note_search_limit)
        rows = await self._db.fetch_all(
            "SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY updated_at DESC"
        )
        notes = [_row_to_note(row) for row in rows]
        needle = " ".join(query.split()).casefold()
        if needle:
            notes = [
                note
                for note in notes
                if needle in note.title.casefold()
                or needle in note.body.casefold()
                or any(needle in tag for tag in note.tags)
            ]
        return notes[:size]

    async def _count_active(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM notes WHERE deleted_at IS NULL"
        )
        return int(row["n"]) if row else 0
