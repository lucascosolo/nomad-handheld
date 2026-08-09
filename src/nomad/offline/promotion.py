"""Noticing what the device keeps paying for, and asking before changing it.

Two things make this the interesting half of chunk O, and they pull against
each other. The device should get better at the questions its operator actually
asks — that is the whole argument for an offline tier over a fixed list. And it
must never quietly change what a phrase means, because a device that does that
is one you stop being able to predict, and an unpredictable pocket computer is
one you leave at home.

The resolution is that this file **proposes and stops**. It reads counts, it
writes a proposal, and there is no path from here to the router's catalog, to a
`ToolRegistry`, or to a skills directory. Nothing it produces takes effect
until an operator acts on it (D26). The strongest statement of that is
structural: this module imports no registry, no library and no writer.

**It proposes a skill, not a tool, and that is not a shortcut (D39).** A skill
is instructions — it carries no capability, needs no `ToolSpec`, no risk level
and no permission decision, so a bad one is bad advice rather than a new way to
reach the world. What this analyst holds as evidence is a phrase and two
integers. That is enough to argue "the operator asks this a lot"; it is nowhere
near enough to argue "and therefore this device should grow a new capability at
some risk level". `PromotionForm.TOOL` exists so the vocabulary can express the
case D39 admits, and this analyst cannot reach it, because the evidence it
collects cannot justify it.

**It is an `OpportunisticWorkload` (D38)** because it is exactly the shape D38
describes: work that makes the device better *later*, and later is always
negotiable. It yields at every candidate and waits through `ctx.sleep`, so a
turn arriving mid-analysis parks it at a checkpoint rather than competing with
the model for two cores. Registering it into a governor is the composition
root's job — this class deliberately does not find one for itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nomad.core.config import OfflineConfig
from nomad.core.logging import get_logger
from nomad.offline.errors import OfflineError
from nomad.offline.ledger import AskRecord, IntentLedger, PromotionForm, PromotionProposal
from nomad.resources.workload import OpportunisticWorkload, YieldContext

logger = get_logger(__name__)

#: Ceiling on a generated skill name, matching `SkillCard`'s own limit. Kept as
#: a number here rather than imported: `offline` does not depend on `skills`,
#: and a promotion draft is text until an operator installs it — at which point
#: the library validates it properly, which is where that check belongs.
MAX_DRAFT_NAME_CHARS = 64
MAX_DRAFT_DESCRIPTION_CHARS = 200


def _now() -> datetime:
    return datetime.now(UTC)


def draft_name(normalized: str) -> str:
    """A stable, readable slug for one phrase."""
    slug = "-".join(normalized.split())[: MAX_DRAFT_NAME_CHARS - len("offline-")]
    slug = "".join(char for char in slug if char.isalnum() or char == "-").strip("-")
    return f"offline-{slug or 'phrase'}"


def render_skill_draft(record: AskRecord) -> str:
    """The Markdown a skill would be, if the operator says yes.

    Written to be *read* before it is installed. It states what was observed
    and says plainly that it grants nothing, because the body of a skill is
    untrusted text entering the model's context (D39) and a draft that reads
    like an instruction to acquire a capability is a draft nobody should paste.
    """
    description = (
        f'What the operator means by "{record.sample.strip()}", asked {record.ask_count} times.'
    )[:MAX_DRAFT_DESCRIPTION_CHARS]
    return (
        "---\n"
        f"name: {draft_name(record.normalized)}\n"
        f"description: {description}\n"
        "---\n"
        f'The operator has asked "{record.sample.strip()}" {record.ask_count} times '
        f"across {record.distinct_days} days, and every one of those went to the "
        "model because Nomad's offline router does not recognise the phrase.\n\n"
        "Answer it with the tools already available on the device, in as few "
        "calls as possible, and lead with the fact rather than a preamble — the "
        "screen is small and the operator is usually walking.\n\n"
        "This skill grants nothing. Every action it describes is an ordinary "
        "tool call, decided by the broker exactly as if the operator had asked "
        "for it directly.\n"
    )


class PromotionAnalyst(OpportunisticWorkload):
    """Turns repeated misses into proposals an operator can accept or refuse."""

    def __init__(
        self,
        ledger: IntentLedger,
        *,
        config: OfflineConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        name: str = "offline-promotion-analyst",
    ) -> None:
        super().__init__(name)
        self._ledger = ledger
        self._config = config or OfflineConfig()
        self._clock = clock or _now

    async def run(self, ctx: YieldContext) -> None:
        """Analyse, sleep, repeat — parking whenever a turn is live.

        The loop never ends on its own: the device keeps being asked things for
        as long as it is powered, so there is no completion state to reach. The
        governor cancels it at shutdown, which is the contract `OpportunisticWorkload`
        already sets out.
        """
        while True:
            await ctx.checkpoint()
            try:
                await self.analyse_once(ctx)
            except OfflineError as exc:  # pragma: no cover - defensive
                logger.warning("Promotion analysis failed", extra={"error": str(exc)})
            await ctx.sleep(max(1.0, self._config.analysis_interval_seconds))

    async def analyse_once(self, ctx: YieldContext | None = None) -> list[PromotionProposal]:
        """One pass. Returns what it newly proposed, which is often nothing.

        `ctx` is optional so the pass is testable and callable from a one-shot
        maintenance path; when it is present, every candidate is a yield point.
        """
        open_proposals = await self._ledger.proposals()
        room = self._config.max_open_proposals - len(open_proposals)
        if room <= 0:
            # An operator staring at a backlog of proposals stops reading them,
            # and a proposal nobody reads is worse than none: it trains the
            # habit of dismissing whatever the device suggests.
            return []

        proposed: list[PromotionProposal] = []
        for record in await self._ledger.candidates():
            if len(proposed) >= room:
                break
            if ctx is not None:
                await ctx.checkpoint()
            proposal = await self._ledger.propose(
                normalized=record.normalized,
                form=PromotionForm.SKILL,
                name=draft_name(record.normalized),
                rationale=(
                    f"asked {record.ask_count} times across {record.distinct_days} "
                    "days and answered by the model every time"
                ),
                draft=render_skill_draft(record),
                evidence_asks=record.ask_count,
                evidence_days=record.distinct_days,
            )
            proposed.append(proposal)
        if proposed:
            logger.info(
                "Offline promotion proposals are waiting for the operator",
                extra={"count": len(proposed)},
            )
        return proposed
