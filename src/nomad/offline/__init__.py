"""The offline tier: answer onboard when it is certain, defer when it is not.

A device with no network is a brick, and a device that pays a datacentre round
trip to say "82%, discharging" is a slow brick. This package is the difference,
and its two halves are `intents.py` (what is answered, and why the matching
cannot be loosened) and `responder.py` (how it is authorized, which is: exactly
like everything else).

Nothing here is registered anywhere by itself. The composition root builds a
router, hands it the same broker and executor the agent uses, and registers
`PromotionAnalyst` as an opportunistic workload (D38).
"""

from __future__ import annotations

from nomad.offline.catalog import DEFAULT_INTENTS
from nomad.offline.errors import OfflineError
from nomad.offline.intents import (
    Intent,
    IntentMatch,
    IntentRouter,
    SlotKind,
    normalize,
)
from nomad.offline.ledger import (
    AskRecord,
    IntentLedger,
    PromotionForm,
    PromotionProposal,
    PromotionState,
)
from nomad.offline.promotion import PromotionAnalyst, render_skill_draft
from nomad.offline.responder import OfflineOutcome, OfflineResponder


def default_router() -> IntentRouter:
    """The shipped phrase set. One line so a caller cannot half-build it."""
    return IntentRouter(DEFAULT_INTENTS)


__all__ = [
    "DEFAULT_INTENTS",
    "AskRecord",
    "Intent",
    "IntentLedger",
    "IntentMatch",
    "IntentRouter",
    "OfflineError",
    "OfflineOutcome",
    "OfflineResponder",
    "PromotionAnalyst",
    "PromotionForm",
    "PromotionProposal",
    "PromotionState",
    "SlotKind",
    "default_router",
    "normalize",
    "render_skill_draft",
]
