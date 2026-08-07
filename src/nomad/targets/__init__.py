"""Targets: where tool actions land (D12)."""

from nomad.targets.base import (
    Capability,
    CommandResult,
    DirEntry,
    ExecOps,
    FileStat,
    FilesystemOps,
    GrepMatch,
    HidOps,
    Target,
    TargetKind,
    require_capabilities,
    require_capability,
    require_exec,
    require_filesystem,
    require_hid,
)
from nomad.targets.hid import HidTarget
from nomad.targets.local import LocalTarget
from nomad.targets.registry import TargetRegistry
from nomad.targets.ssh import SshTarget

__all__ = [
    "Capability",
    "CommandResult",
    "DirEntry",
    "ExecOps",
    "FileStat",
    "FilesystemOps",
    "GrepMatch",
    "HidOps",
    "HidTarget",
    "LocalTarget",
    "SshTarget",
    "Target",
    "TargetKind",
    "TargetRegistry",
    "require_capabilities",
    "require_capability",
    "require_exec",
    "require_filesystem",
    "require_hid",
]
