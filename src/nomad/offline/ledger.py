"""What gets asked, how often, and over how many days.

Promotion has to be driven by evidence rather than by a hunch, and evidence
that lives in memory is evidence that resets every time the process restarts —
which, on a device whose session rolls over by design (D34), is often. So the
counts are rows.

Three shapes matter here and each one is a decision:

- **The key is the normalized utterance**, the same canonical form the router
  matches on. "What's my battery?" and "whats my battery" are one row, because
  they are one question, and a promotion argument built on a corpus that splits
  them would never reach a count worth acting on.
- **Misses are the interesting half.** An utterance the router already answers
  is counted so the operator can see what the tier is earning; an utterance that
  went to the model is counted *as a candidate*. The thing worth promoting is by
  definition the thing not yet promoted.
- **A proposal is a row with a state, and nothing else.** Accepting one records
  that the operator accepted it and hands back the draft. It installs nothing,
  writes no skill file and touches no catalog — the device cannot change what a
  phrase means to it without a human doing the changing, and that is enforced
  by there being no code here that could.

Rejection is remembered as firmly as acceptance. A candidate the operator has
already said no to is never proposed again, because a device that asks the same
question every week has found a way to make "no" cost more than "yes".
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from nomad.core.config import OfflineConfig
from nomad.core.logging import get_logger
from nomad.offline.errors import OfflineError
from nomad.offline.intents import normalize
from nomad.storage.db import Database

logger = get_logger(__name__)

#: How many distinct days one row remembers. Stability only ever asks "at least
#: N", so a bounded window answers it exactly and keeps the row small.
MAX_OBSERVED_DAYS = 30


def _now() -> datetime:
    return datetime.now(UTC)


class AskRecord(BaseModel):
    """One phrase the operator has said, and the evidence about it."""

    id: str
    normalized: str
    sample: str
    #: The intent that answered it onboard, or `None` if it went to the model.
    routed_intent: str | None = None
    ask_count: int = 1
    observed_days: list[str] = Field(default_factory=list)
    first_asked_at: datetime
    last_asked_at: datetime

    @property
    def distinct_days(self) -> int:
        return len(self.observed_days)


class PromotionForm(StrEnum):
    """What a promotion would create.

    `TOOL` exists in the vocabulary because D39 admits the case, not because
    this package can reach it: see `promotion.py` for why the evidence a phrase
    counter holds can never justify a `ToolSpec`.
    """

    SKILL = "skill"
    TOOL = "tool"


class PromotionState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class PromotionProposal(BaseModel):
    """An offer, not a change. Inert until an operator acts on it.

    There is deliberately no `apply()`, no `install()` and no handle to a skill
    library on this type. The draft is text; what happens to the text is the
    operator's business, through the settings path that already has a human in
    it (D26, D39).
    """

    id: str
    normalized: str
    form: PromotionForm
    name: str
    rationale: str
    draft: str
    evidence_asks: int
    evidence_days: int
    state: PromotionState = PromotionState.PROPOSED
    proposed_at: datetime
    decided_at: datetime | None = None


def _row_to_ask(row: dict[str, Any]) -> AskRecord:
    return AskRecord(
        id=row["id"],
        normalized=row["normalized"],
        sample=row["sample"],
        routed_intent=row["routed_intent"],
        ask_count=int(row["ask_count"]),
        observed_days=list(json.loads(row["observed_days_json"])),
        first_asked_at=datetime.fromisoformat(row["first_asked_at"]),
        last_asked_at=datetime.fromisoformat(row["last_asked_at"]),
    )


def _row_to_proposal(row: dict[str, Any]) -> PromotionProposal:
    return PromotionProposal(
        id=row["id"],
        normalized=row["normalized"],
        form=PromotionForm(row["form"]),
        name=row["name"],
        rationale=row["rationale"],
        draft=row["draft"],
        evidence_asks=int(row["evidence_asks"]),
        evidence_days=int(row["evidence_days"]),
        state=PromotionState(row["state"]),
        proposed_at=datetime.fromisoformat(row["proposed_at"]),
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
    )


class IntentLedger:
    """Durable counts of what gets asked, and the proposals built from them."""

    def __init__(
        self,
        db: Database,
        *,
        config: OfflineConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config = config or OfflineConfig()
        self._clock = clock or _now

    # -- evidence ---------------------------------------------------------

    async def record(self, utterance: str, *, routed_intent: str | None) -> AskRecord | None:
        """Count one ask. `None` when the utterance is not evidence of anything.

        Long utterances are dropped rather than stored: a paragraph is not a
        phrase somebody repeats, so it will never reach a count worth acting on,
        and keeping it would be storing the operator's prose in a table whose
        entire purpose is counting short questions.
        """
        key = normalize(utterance)
        if not key or len(key) > self._config.max_ask_chars:
            return None

        now = self._clock()
        today = now.astimezone(UTC).date()
        existing = await self.get(key)
        if existing is None:
            if not await self._make_room():
                logger.debug("Ask ledger is full; not tracking a new phrase")
                return None
            record = AskRecord(
                id=uuid.uuid4().hex,
                normalized=key,
                sample=utterance.strip()[: self._config.max_ask_chars],
                routed_intent=routed_intent,
                ask_count=1,
                observed_days=[today.isoformat()],
                first_asked_at=now,
                last_asked_at=now,
            )
            await self._db.execute(
                "INSERT INTO offline_asks (id, normalized, sample, routed_intent, "
                "ask_count, observed_days_json, first_asked_at, last_asked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.normalized,
                    record.sample,
                    record.routed_intent,
                    record.ask_count,
                    json.dumps(record.observed_days),
                    record.first_asked_at.isoformat(),
                    record.last_asked_at.isoformat(),
                ),
            )
            return record

        days = _with_day(existing.observed_days, today)
        updated = existing.model_copy(
            update={
                "ask_count": existing.ask_count + 1,
                "observed_days": days,
                "last_asked_at": now,
                # A phrase that started as a miss and is now routed stops being
                # a candidate the moment it is answered onboard.
                "routed_intent": routed_intent,
            }
        )
        await self._db.execute(
            "UPDATE offline_asks SET ask_count = ?, observed_days_json = ?, "
            "last_asked_at = ?, routed_intent = ? WHERE normalized = ?",
            (
                updated.ask_count,
                json.dumps(updated.observed_days),
                updated.last_asked_at.isoformat(),
                updated.routed_intent,
                key,
            ),
        )
        return updated

    async def get(self, normalized: str) -> AskRecord | None:
        row = await self._db.fetch_one(
            "SELECT * FROM offline_asks WHERE normalized = ?", (normalized,)
        )
        return _row_to_ask(row) if row is not None else None

    async def top_asks(self, *, limit: int = 20) -> list[AskRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM offline_asks ORDER BY ask_count DESC, last_asked_at DESC LIMIT ?",
            (max(1, limit),),
        )
        return [_row_to_ask(row) for row in rows]

    async def candidates(self) -> list[AskRecord]:
        """Phrases that are both frequent and stable, and not already decided.

        Frequency alone would promote a phrase somebody said eight times in one
        frustrated afternoon and never again. The day count is what separates a
        habit from an incident, which is the difference the operator is actually
        being asked to rule on.
        """
        rows = await self._db.fetch_all(
            "SELECT a.* FROM offline_asks a "
            "LEFT JOIN offline_promotions p ON p.normalized = a.normalized "
            "WHERE a.routed_intent IS NULL AND a.ask_count >= ? AND p.id IS NULL "
            "ORDER BY a.ask_count DESC, a.last_asked_at DESC",
            (max(1, self._config.promote_after_asks),),
        )
        wanted_days = max(1, self._config.promote_after_days)
        return [
            record
            for record in (_row_to_ask(row) for row in rows)
            if record.distinct_days >= wanted_days
        ]

    # -- proposals --------------------------------------------------------

    async def propose(
        self,
        *,
        normalized: str,
        form: PromotionForm,
        name: str,
        rationale: str,
        draft: str,
        evidence_asks: int,
        evidence_days: int,
    ) -> PromotionProposal:
        proposal = PromotionProposal(
            id=uuid.uuid4().hex,
            normalized=normalized,
            form=form,
            name=name,
            rationale=rationale,
            draft=draft,
            evidence_asks=evidence_asks,
            evidence_days=evidence_days,
            state=PromotionState.PROPOSED,
            proposed_at=self._clock(),
        )
        await self._db.execute(
            "INSERT INTO offline_promotions (id, normalized, form, name, rationale, "
            "draft, evidence_asks, evidence_days, state, proposed_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                proposal.id,
                proposal.normalized,
                proposal.form.value,
                proposal.name,
                proposal.rationale,
                proposal.draft,
                proposal.evidence_asks,
                proposal.evidence_days,
                proposal.state.value,
                proposal.proposed_at.isoformat(),
            ),
        )
        logger.info(
            "Proposed an offline promotion",
            extra={"proposal": proposal.id, "form": str(proposal.form)},
        )
        return proposal

    async def proposals(
        self, *, state: PromotionState | None = PromotionState.PROPOSED, limit: int = 20
    ) -> list[PromotionProposal]:
        if state is None:
            rows = await self._db.fetch_all(
                "SELECT * FROM offline_promotions ORDER BY proposed_at DESC LIMIT ?",
                (max(1, limit),),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM offline_promotions WHERE state = ? "
                "ORDER BY proposed_at DESC LIMIT ?",
                (state.value, max(1, limit)),
            )
        return [_row_to_proposal(row) for row in rows]

    async def get_proposal(self, proposal_id: str) -> PromotionProposal | None:
        row = await self._db.fetch_one(
            "SELECT * FROM offline_promotions WHERE id = ?", (proposal_id,)
        )
        return _row_to_proposal(row) if row is not None else None

    async def accept(self, proposal_id: str) -> PromotionProposal:
        """Record the operator's yes and hand back the draft.

        Everything this method does *not* do is the point: no file is written,
        no skill is loaded, no phrase starts being answered differently. The
        operator now holds a draft, and installing it is the same
        human-in-the-loop act it was before they were asked (D26, D39).
        """
        return await self._decide(proposal_id, PromotionState.ACCEPTED)

    async def reject(self, proposal_id: str) -> PromotionProposal:
        """Record the operator's no, permanently. The phrase is never re-proposed."""
        return await self._decide(proposal_id, PromotionState.REJECTED)

    async def _decide(self, proposal_id: str, state: PromotionState) -> PromotionProposal:
        proposal = await self.get_proposal(proposal_id)
        if proposal is None:
            raise OfflineError(
                f"no promotion proposal with id {proposal_id}", {"proposal": proposal_id}
            )
        if proposal.state is not PromotionState.PROPOSED:
            raise OfflineError(
                f"proposal {proposal_id} was already {proposal.state}",
                {"proposal": proposal_id, "state": str(proposal.state)},
            )
        decided_at = self._clock()
        await self._db.execute(
            "UPDATE offline_promotions SET state = ?, decided_at = ? WHERE id = ?",
            (state.value, decided_at.isoformat(), proposal_id),
        )
        logger.info(
            "Operator decided an offline promotion",
            extra={"proposal": proposal_id, "state": state.value},
        )
        return proposal.model_copy(update={"state": state, "decided_at": decided_at})

    # -- housekeeping -----------------------------------------------------

    async def _make_room(self) -> bool:
        """Keep the table bounded; evict a one-off before refusing to learn.

        A handheld's database is not a data warehouse, and an unbounded table of
        every sentence ever said to the device is both a size problem and a
        privacy one. Single-ask rows are the cheapest thing to lose: by
        definition none of them is evidence of a habit yet.
        """
        cap = max(1, self._config.max_tracked_asks)
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM offline_asks")
        if row is None or int(row["n"]) < cap:
            return True
        victim = await self._db.fetch_one(
            "SELECT id FROM offline_asks WHERE ask_count = 1 ORDER BY last_asked_at ASC LIMIT 1"
        )
        if victim is None:
            return False
        await self._db.execute("DELETE FROM offline_asks WHERE id = ?", (victim["id"],))
        return True


def _with_day(days: Sequence[str], today: date) -> list[str]:
    stamp = today.isoformat()
    if stamp in days:
        return list(days)
    return [*days, stamp][-MAX_OBSERVED_DAYS:]
