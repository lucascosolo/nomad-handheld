"""Target registry: resolve a target id to a `Target` (D12).

Explicit registration only. An unknown target id is a `TargetError`, never a
silent fallback to `local` — "the model named a target we do not have" must
fail loudly rather than quietly aiming an action somewhere else.
"""

from __future__ import annotations

from nomad.core.errors import TargetError
from nomad.core.logging import get_logger
from nomad.targets.base import Capability, Target, TargetKind

logger = get_logger(__name__)


class TargetRegistry:
    """Ordered registry of targets, keyed by id."""

    def __init__(self) -> None:
        self._targets: dict[str, Target] = {}

    def register(self, target: Target) -> None:
        if target.id in self._targets:
            raise TargetError(f"Target '{target.id}' is already registered", {"target": target.id})
        self._targets[target.id] = target
        logger.debug(
            "Registered target",
            extra={
                "target": target.id,
                "kind": str(target.kind),
                "capabilities": sorted(str(c) for c in target.capabilities),
            },
        )

    def get(self, target_id: str) -> Target:
        target = self._targets.get(target_id)
        if target is None:
            raise TargetError(
                f"Unknown target '{target_id}'",
                {"target": target_id, "known": sorted(self._targets)},
            )
        return target

    def try_get(self, target_id: str) -> Target | None:
        return self._targets.get(target_id)

    def has(self, target_id: str) -> bool:
        return target_id in self._targets

    def ids(self) -> list[str]:
        return list(self._targets)

    def list_targets(self) -> list[Target]:
        return list(self._targets.values())

    def of_kind(self, kind: TargetKind) -> list[Target]:
        return [t for t in self._targets.values() if t.kind is kind]

    def with_capability(self, capability: Capability) -> list[Target]:
        return [t for t in self._targets.values() if capability in t.capabilities]

    def describe(self) -> list[dict[str, object]]:
        """Serializable summary — what the model and the UI are shown."""
        return [
            {
                "id": t.id,
                "kind": str(t.kind),
                "capabilities": sorted(str(c) for c in t.capabilities),
            }
            for t in self._targets.values()
        ]
