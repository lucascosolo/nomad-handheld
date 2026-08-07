"""The provider interface the agent calls (D17). No implementations here.

`assistant/providers` (chunk F) implements `AIProvider`; the agent only ever
sees this Protocol, so swapping cloud for local, or one vendor for another,
never reaches the turn loop.

These types are Nomad's own, deliberately not shaped like any one vendor's
wire format (D17). An adapter translates.

The contract, in full:

* `complete()` receives a `ProviderRequest` — an object rather than keyword
  arguments, so chunk F can gain fields (streaming handles, routing hints,
  a battery-aware budget per D18) without changing every caller.
* It returns a `ProviderResponse`. Exactly one of two things is true:
  `tool_calls` is non-empty and `stop_reason` is `TOOL_USE`, or the turn is
  over. The loop treats a response with both text and tool calls as "say this,
  then run these".
* Failure is reported as `stop_reason=ERROR` with `error` set, or by raising
  `ProviderError`. The loop handles both; it never sees a vendor exception.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ProviderMessage(BaseModel):
    """One conversation message, in Nomad's own vocabulary."""

    role: MessageRole
    content: str = ""
    #: Set on TOOL messages: the provider's id for the call being answered.
    tool_call_id: str | None = None
    #: Set on TOOL messages: the tool's name, for providers that want it.
    name: str | None = None

    def to_storage(self) -> dict[str, Any]:
        return {"text": self.content, "tool_call_id": self.tool_call_id, "name": self.name}

    @classmethod
    def from_storage(cls, role: str, content: dict[str, Any]) -> ProviderMessage:
        return cls(
            role=MessageRole(role),
            content=str(content.get("text", "")),
            tool_call_id=content.get("tool_call_id"),
            name=content.get("name"),
        )


class ProviderToolCall(BaseModel):
    """A tool the model wants run. `target_id` defaults to the local machine."""

    id: str
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    target_id: str = "local"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    ERROR = "error"


class ProviderRequest(BaseModel):
    """Everything a provider needs for one round trip."""

    messages: list[ProviderMessage]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    system: str | None = None
    max_tokens: int | None = None


class ProviderResponse(BaseModel):
    text: str = ""
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    error: str | None = None
    #: Free-form counters, e.g. {"input_tokens": .., "output_tokens": ..}.
    usage: dict[str, int] = Field(default_factory=dict)


@runtime_checkable
class AIProvider(Protocol):
    """What chunk F implements."""

    name: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...
