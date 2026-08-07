"""Token budget and summarize-and-compact (D16).

A session that runs for days has unbounded history by construction. On a 4 GB
Pi that is a correctness concern, not an optimization, so compaction is a core
component rather than a nice-to-have.

**Token estimation is an approximation, on purpose.** Adding a tokenizer means
adding a vendor dependency and a model download to a device that is short of
both RAM and disk, in order to make a *budget* slightly less conservative. The
estimator here is ~4 characters per token plus a fixed per-message overhead —
the well-known rule of thumb for English and code with BPE tokenizers. It runs
about 10-20% low on dense code and high on prose. `compact_at` defaults to
0.75, which is a wide enough margin to absorb that error; if a provider ever
reports real usage, feed it back via `observe_usage()` and the estimate
self-corrects.

Compaction records are persisted as durable artifacts, never discarded (D16).
They are written to the `messages` table with role `compaction`, because
migration 001 has no dedicated table and an audit trail that lives only in a
log file is not an audit trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from nomad.agent.provider import MessageRole, ProviderMessage
from nomad.core.events import Event, EventBus
from nomad.core.logging import get_logger
from nomad.storage.repositories.conversations import ConversationsRepository

logger = get_logger(__name__)

#: Characters per token. See the module docstring for why this is a constant.
CHARS_PER_TOKEN = 4
#: Rough per-message framing overhead (role markers, delimiters).
TOKENS_PER_MESSAGE = 4
DEFAULT_CONTEXT_WINDOW = 150_000
DEFAULT_KEEP_RECENT = 8

COMPACTION_ROLE = "compaction"
EVENT_COMPACTED = "agent.context_compacted"

#: Injected. Chunk F can back this with the model; the fallback is mechanical.
Summarizer = Callable[[Sequence[ProviderMessage]], Awaitable[str]]


class CompactionRecord(BaseModel):
    """A durable record of what was compacted away, and into what."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    turn_id: str | None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    messages_compacted: int
    tokens_before: int
    tokens_after: int
    summary: str
    summarizer_failed: bool = False


def estimate_tokens(messages: Sequence[ProviderMessage]) -> int:
    """Approximate token count. See the module docstring — this is a budget."""
    total = 0
    for message in messages:
        chars = len(message.content)
        total += TOKENS_PER_MESSAGE + (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
    return total


def _mechanical_summary(messages: Sequence[ProviderMessage]) -> str:
    """Deterministic fallback used when the summarizer is absent or fails.

    Losing history silently would be worse than a crude summary, so this never
    fails and never returns nothing.
    """
    lines = [f"[compacted {len(messages)} earlier messages]"]
    for message in messages:
        excerpt = " ".join(message.content.split())[:160]
        lines.append(f"- {message.role}: {excerpt}" if excerpt else f"- {message.role}: (empty)")
    return "\n".join(lines)


class ContextManager:
    """Owns the token budget and the compaction cycle for one session."""

    def __init__(
        self,
        *,
        conversations: ConversationsRepository | None = None,
        bus: EventBus | None = None,
        max_tokens: int = DEFAULT_CONTEXT_WINDOW,
        compact_at: float = 0.75,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ) -> None:
        if not 0.0 < compact_at <= 1.0:
            raise ValueError("compact_at must be in (0, 1]")
        self._conversations = conversations
        self._bus = bus
        self._max_tokens = max_tokens
        self._compact_at = compact_at
        self._keep_recent = keep_recent
        self._observed_ratio: float | None = None

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def threshold_tokens(self) -> int:
        return int(self._max_tokens * self._compact_at)

    def estimate_tokens(self, messages: Sequence[ProviderMessage]) -> int:
        raw = estimate_tokens(messages)
        if self._observed_ratio is None:
            return raw
        return int(raw * self._observed_ratio)

    def observe_usage(self, messages: Sequence[ProviderMessage], reported_tokens: int) -> None:
        """Calibrate the estimator against a provider's reported usage."""
        raw = estimate_tokens(messages)
        if raw > 0 and reported_tokens > 0:
            self._observed_ratio = reported_tokens / raw

    def usage_fraction(self, messages: Sequence[ProviderMessage]) -> float:
        return self.estimate_tokens(messages) / self._max_tokens if self._max_tokens else 0.0

    def should_compact(self, messages: Sequence[ProviderMessage]) -> bool:
        return self.estimate_tokens(messages) >= self.threshold_tokens

    async def compact(
        self,
        messages: Sequence[ProviderMessage],
        *,
        session_id: str,
        turn_id: str | None,
        summarizer: Summarizer | None = None,
    ) -> tuple[list[ProviderMessage], CompactionRecord | None]:
        """Summarize the older half of the history and persist the record.

        System messages and the most recent `keep_recent` messages are always
        preserved verbatim — the model needs its instructions and its
        immediate working state intact.
        """
        preserved = [m for m in messages if m.role is MessageRole.SYSTEM]
        body = [m for m in messages if m.role is not MessageRole.SYSTEM]
        if len(body) <= self._keep_recent:
            return list(messages), None

        recent = body[-self._keep_recent :]
        older = body[: -self._keep_recent]

        failed = False
        summary = ""
        if summarizer is not None:
            try:
                summary = await summarizer(older)
            except Exception as exc:  # noqa: BLE001 - never lose history to a summarizer bug
                logger.warning("Summarizer failed; falling back", extra={"error": str(exc)})
                failed = True
        if not summary:
            summary = _mechanical_summary(older)
            failed = failed or summarizer is not None

        summary_message = ProviderMessage(
            role=MessageRole.SYSTEM,
            content=f"Summary of earlier conversation:\n{summary}",
        )
        compacted = [*preserved, summary_message, *recent]

        record = CompactionRecord(
            session_id=session_id,
            turn_id=turn_id,
            messages_compacted=len(older),
            tokens_before=estimate_tokens(messages),
            tokens_after=estimate_tokens(compacted),
            summary=summary,
            summarizer_failed=failed,
        )
        await self._persist(record)
        logger.info(
            "Compacted context",
            extra={
                "session_id": session_id,
                "messages_compacted": record.messages_compacted,
                "tokens_before": record.tokens_before,
                "tokens_after": record.tokens_after,
            },
        )
        return compacted, record

    async def _persist(self, record: CompactionRecord) -> None:
        if self._conversations is not None and record.turn_id is not None:
            await self._conversations.add_message(
                turn_id=record.turn_id,
                session_id=record.session_id,
                role=COMPACTION_ROLE,
                content=record.model_dump(mode="json"),
                message_id=record.id,
            )
        elif self._conversations is not None:
            logger.warning(
                "Compaction record not persisted: no turn id",
                extra={"session_id": record.session_id},
            )
        if self._bus is not None:
            await self._bus.publish(
                Event(
                    type=EVENT_COMPACTED,
                    source="agent_context",
                    payload={
                        "record_id": record.id,
                        "session_id": record.session_id,
                        "turn_id": record.turn_id,
                        "messages_compacted": record.messages_compacted,
                        "tokens_before": record.tokens_before,
                        "tokens_after": record.tokens_after,
                        "summarizer_failed": record.summarizer_failed,
                    },
                )
            )
