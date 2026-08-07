"""Built-in tools and the default registry."""

from __future__ import annotations

from nomad.core.config import NomadConfig
from nomad.tools.builtin.files import (
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from nomad.tools.builtin.shell import RunCommandTool
from nomad.tools.builtin.system import GetSystemInfoTool
from nomad.tools.registry import ToolRegistry

__all__ = [
    "GetSystemInfoTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "RunCommandTool",
    "WriteFileTool",
    "build_default_registry",
]


def build_default_registry(config: NomadConfig) -> ToolRegistry:
    """Register every built-in tool, honouring `[tools]` config.

    `run_command` is registered but disabled unless `tools.enable_run_command`
    is true (D5). Registering-then-disabling rather than skipping means the
    broker can explain *why* it refused instead of reporting an unknown tool.
    """
    registry = ToolRegistry()
    registry.register(GetSystemInfoTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ListDirTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(RunCommandTool(), enabled=config.tools.enable_run_command)
    return registry
