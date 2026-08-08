"""The NomadError hierarchy.

Every error raised anywhere in the project derives from `NomadError`. This
module imports nothing else from `nomad` — it sits below everything.
"""

from __future__ import annotations

from typing import Any


class NomadError(Exception):
    """Base class for every error raised by Nomad.

    Args:
        message: Human-readable description of what went wrong.
        details: Optional structured context (offending key, path, etc.).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


class ConfigError(NomadError):
    """Configuration failed to load or validate."""


class StorageError(NomadError):
    """A storage/database operation failed."""


class EventError(NomadError):
    """The event bus encountered an error it could not isolate."""


class LifecycleError(NomadError):
    """A component failed to start or stop cleanly."""


class PermissionDenied(NomadError):
    """A tool call was denied by the permission broker."""


class ToolError(NomadError):
    """A tool failed during execution."""


class TargetError(NomadError):
    """A target could not be resolved or acted upon."""


class TransportError(NomadError):
    """A transport failed to send or receive bytes."""


class ProtocolError(NomadError):
    """A message failed to encode, decode, or frame correctly."""


class AgentError(NomadError):
    """An agent backend failed to start, send, or produce a response (D24).

    Replaces the pre-pivot `ProviderError`: after D19 there is no `AIProvider`
    to fail, only a backend.
    """
