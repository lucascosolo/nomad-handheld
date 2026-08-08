"""`offline_status`, and the tool this module deliberately does not have.

The model is allowed to *see* the offline tier: which phrases the device
answers without it, what it keeps being asked that it cannot answer, and which
promotions are waiting on the operator. That is useful — a model that knows
"what's my battery" is already handled locally can stop offering to check.

**There is no tool that accepts a promotion**, and that is the same shape as
D36's authorization prompt, D37's missing `listen` and D39's missing
`install_skill`. Accepting a promotion is the one act in this package that
changes what the device will do with a phrase in future. A model-callable
acceptor would close the loop the whole design exists to hold open: the device
would be able to propose a change to itself and then approve it, and "operator
approved" would mean nothing. Acceptance goes through `IntentLedger.accept()`,
which is reachable from the operator's paths and not from `can_use_tool`.

Reading is `READ_ONLY` and `device_local` (D35): it touches Nomad's own tables
and transmits nothing, so it auto-runs in every mode. It is still a grant, still
minted, still audited.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from nomad.core.errors import NomadError
from nomad.offline.intents import IntentRouter
from nomad.offline.ledger import IntentLedger
from nomad.tools.base import Risk, Tool, ToolContext, ToolResult, ToolSpec


class OfflineView(StrEnum):
    ROUTES = "routes"
    ASKS = "asks"
    PROPOSALS = "proposals"


class OfflineStatusParams(BaseModel):
    view: OfflineView = Field(
        default=OfflineView.ROUTES,
        description=(
            "routes: what this device answers with no network. asks: what it is "
            "asked most often. proposals: promotions waiting on the operator."
        ),
    )
    limit: int | None = Field(default=None, ge=1, le=50, description="Rows to return.")


class OfflineStatusTool:
    """Report what the offline tier answers, and what it has learned it cannot."""

    spec = ToolSpec(
        name="offline_status",
        device_local=True,
        description=(
            "Report which phrases Nomad answers onboard with no network, which "
            "questions it is asked most often, and which onboard-capability "
            "promotions are waiting for the operator to accept or reject."
        ),
        params_model=OfflineStatusParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, router: IntentRouter, *, ledger: IntentLedger | None = None) -> None:
        self._router = router
        self._ledger = ledger

    async def execute(self, params: OfflineStatusParams, ctx: ToolContext) -> ToolResult:
        if params.view is OfflineView.ROUTES:
            lines = [
                f"- {intent.name} -> {intent.tool}: {intent.summary} "
                f"({len(intent.phrases) + len(intent.templates)} phrases)"
                for intent in self._router.intents
            ]
            return ToolResult.success(
                f"{len(lines)} intent(s) answered onboard:\n" + "\n".join(lines),
                count=len(lines),
                phrases=self._router.phrase_count(),
            )

        if self._ledger is None:
            return ToolResult.failure("this device keeps no offline evidence ledger")

        limit = params.limit or 10
        try:
            if params.view is OfflineView.ASKS:
                asks = await self._ledger.top_asks(limit=limit)
                if not asks:
                    return ToolResult.success("nothing asked yet", count=0)
                listing = "\n".join(
                    f"- {a.sample} — {a.ask_count}x over {a.distinct_days} day(s), "
                    f"{'onboard: ' + a.routed_intent if a.routed_intent else 'goes to the model'}"
                    for a in asks
                )
                return ToolResult.success(f"{len(asks)} ask(s):\n{listing}", count=len(asks))

            proposals = await self._ledger.proposals(limit=limit)
        except NomadError as exc:
            return ToolResult.failure(str(exc))
        if not proposals:
            return ToolResult.success("no promotions are waiting", count=0)
        listing = "\n".join(
            f"- {p.name} ({p.form}): {p.rationale} [{p.id}]" for p in proposals
        )
        return ToolResult.success(
            f"{len(proposals)} promotion(s) awaiting the operator — Nomad cannot "
            f"accept these itself:\n{listing}",
            count=len(proposals),
        )


def build_offline_tools(
    router: IntentRouter | None, *, ledger: IntentLedger | None = None
) -> list[Tool]:
    """No router, no tool. Absent infrastructure means an absent capability."""
    if router is None:
        return []
    return [OfflineStatusTool(router, ledger=ledger)]
