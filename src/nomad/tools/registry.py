"""Tool registry: explicit registration, lookup, and config-driven filtering.

Entry-point plugin discovery is deliberately deferred (see "Deliberately
deferred" in DECISIONS.md). A tool the operator has not registered does not
exist, and a tool disabled in config is registered but never runnable — the
distinction matters, because the broker must be able to say *why* it refused
rather than pretending the tool was never there.
"""

from __future__ import annotations

from typing import Any

from nomad.core.errors import ToolError
from nomad.core.logging import get_logger
from nomad.tools.base import Tool, ToolSpec

logger = get_logger(__name__)


class ToolRegistry:
    """Ordered registry of tools, keyed by spec name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()

    def register(self, tool: Tool, *, enabled: bool = True) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ToolError(f"Tool '{name}' is already registered", {"tool": name})
        self._tools[name] = tool
        if not enabled:
            self._disabled.add(name)
        logger.debug(
            "Registered tool",
            extra={"tool": name, "risk": str(tool.spec.risk), "enabled": enabled},
        )

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._tools:
            raise ToolError(f"Unknown tool '{name}'", {"tool": name})
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'", {"tool": name, "known": sorted(self._tools)})
        return tool

    def try_get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self, *, enabled_only: bool = False) -> list[str]:
        if enabled_only:
            return [n for n in self._tools if n not in self._disabled]
        return list(self._tools)

    def specs(self, *, enabled_only: bool = True) -> list[ToolSpec]:
        return [self._tools[n].spec for n in self.names(enabled_only=enabled_only)]

    def model_schemas(self) -> list[dict[str, Any]]:
        """What the model is shown: enabled tools only.

        A disabled tool is not advertised, so the model does not waste a turn
        asking for something that can only be denied.
        """
        return [spec.to_model_schema() for spec in self.specs(enabled_only=True)]
