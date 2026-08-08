"""Refusal as an answer, not a crash.

`NotificationRefused` exists for the same reason `MemoryRefused` does: the
caller is usually a tool wrapping this in a `ToolResult.failure`, and a model
that is told *what tripped* picks something else, while a model handed a
traceback rephrases the identical request and tries again.
"""

from __future__ import annotations

from nomad.core.errors import NomadError


class NotificationRefused(NomadError):
    """The queue declined to store or transition a notification.

    An empty title, a due time the caller cannot have meant, a repeat rule the
    queue cannot parse, or a transition out of a state that is already
    terminal.
    """
