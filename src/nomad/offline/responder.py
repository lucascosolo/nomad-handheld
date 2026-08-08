"""Answering onboard, through the broker, or not at all.

"Onboard" describes **where** a call runs. It has never described whether the
call was allowed, and the moment those two ideas share a code path the device
has a fast lane that skips its own security layer — at exactly the moment
nobody is watching, since the whole appeal of this tier is that it answers
while the screen is dark and the operator is walking.

So this module owns no execution. It holds a router that can only *propose* a
call, and a broker and executor it borrows from the same pipeline every other
tool call uses (D4). The sequence is the ordinary one, unshortened:

    ToolRequest -> broker.decide -> broker.authorize -> executor.run

with three consequences that are all deliberate:

- **A `never_auto` or denied action is not answered offline.** The responder
  does not prompt and does not queue an authorization; it reports that it did
  not handle the utterance and the ordinary path takes it, prompt and all.
  There is one authorization UI on this device and it is not this one (D36).
- **A failed tool result is not a handled utterance.** If the tool ran and said
  no — an unknown timezone, a malformed id — nothing happened, and the model
  can do better with the same sentence. The execution is still audited: it was
  a real call with a real grant, and pretending otherwise is what an audit log
  exists to prevent.
- **The evidence ledger is never allowed to break answering.** A storage fault
  while counting an ask is logged and dropped. Losing a count costs a slightly
  later promotion; raising here would mean the device stops answering "what's
  my battery" because a table is locked.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nomad.core.config import PermissionMode
from nomad.core.errors import NomadError
from nomad.core.logging import get_logger
from nomad.offline.intents import IntentMatch, IntentRouter
from nomad.offline.ledger import IntentLedger
from nomad.tools.permissions import PermissionBroker, ToolExecutor, ToolRequest

logger = get_logger(__name__)


class OfflineOutcome(BaseModel):
    """What the offline tier did with one utterance.

    `handled=False` is not a failure. It is the design's normal answer, and the
    caller's only correct response to it is to send the turn to the model.
    """

    handled: bool
    reason: str
    intent: str | None = None
    tool: str | None = None
    content: str = ""
    #: Proof this went through the pipeline. `None` whenever nothing ran.
    grant_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class OfflineResponder:
    """Route an utterance onboard when — and only when — that is unambiguous."""

    def __init__(
        self,
        *,
        router: IntentRouter,
        broker: PermissionBroker,
        executor: ToolExecutor,
        ledger: IntentLedger | None = None,
        target_id: str = "local",
    ) -> None:
        self._router = router
        self._broker = broker
        self._executor = executor
        self._ledger = ledger
        self._target_id = target_id

    @property
    def router(self) -> IntentRouter:
        return self._router

    async def handle(
        self,
        utterance: str,
        *,
        session_id: str,
        mode: PermissionMode,
        turn_id: str | None = None,
    ) -> OfflineOutcome:
        match = self._router.match(utterance)
        if match is None:
            await self._count(utterance, routed_intent=None)
            return OfflineOutcome(handled=False, reason="no exact intent match")

        outcome = await self._run(match, session_id=session_id, mode=mode, turn_id=turn_id)
        # A call the broker refused, or a tool that failed, is evidence that
        # this phrase still costs a round trip — count it as a miss so the
        # promotion path sees the same reality the operator does.
        await self._count(utterance, routed_intent=match.intent if outcome.handled else None)
        return outcome

    async def _run(
        self,
        match: IntentMatch,
        *,
        session_id: str,
        mode: PermissionMode,
        turn_id: str | None,
    ) -> OfflineOutcome:
        request = ToolRequest(
            tool=match.tool,
            target_id=self._target_id,
            params=dict(match.params),
            session_id=session_id,
            turn_id=turn_id,
        )
        decision = await self._broker.decide(request, mode)
        if not decision.allowed:
            logger.info(
                "Offline intent was not authorized; deferring to the model",
                extra={"intent": match.intent, "tool": match.tool, "reason": decision.reason},
            )
            return OfflineOutcome(
                handled=False,
                reason=f"not authorized offline: {decision.reason}",
                intent=match.intent,
                tool=match.tool,
            )

        grant = await self._broker.authorize(request, decision)
        result = await self._executor.run(grant, request)
        if not result.ok:
            return OfflineOutcome(
                handled=False,
                reason=f"tool failed: {result.error}",
                intent=match.intent,
                tool=match.tool,
                grant_id=grant.id,
            )
        return OfflineOutcome(
            handled=True,
            reason="answered onboard",
            intent=match.intent,
            tool=match.tool,
            content=result.content,
            grant_id=grant.id,
            metadata=dict(result.metadata),
        )

    async def _count(self, utterance: str, *, routed_intent: str | None) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.record(utterance, routed_intent=routed_intent)
        except NomadError as exc:
            logger.warning("Could not record an offline ask", extra={"error": str(exc)})
