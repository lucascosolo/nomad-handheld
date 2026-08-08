"""Presence: the things Nomad has to say when nobody is looking at it.

The bus (D6) drops slow subscribers so a wedged client cannot freeze the
display. That is why this package exists rather than a `notification.*` event:
the screen is dark most of the time and the process restarts, so anything the
device must still say later has to be a row, not a publish. See `queue.py`.
"""

from nomad.notifications.delivery import (
    NOTIFICATION_WRITER,
    Announce,
    NotificationDelivery,
    ScreenNotificationSink,
)
from nomad.notifications.errors import NotificationDeferred, NotificationRefused
from nomad.notifications.models import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    Notification,
    NotificationKind,
    NotificationState,
    default_dedup_key,
    normalize_dedup_key,
)
from nomad.notifications.queue import NotificationQueue, NotificationSink
from nomad.notifications.repeat import next_occurrence, parse_repeat_rule

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_TITLE_CHARS",
    "NOTIFICATION_WRITER",
    "Announce",
    "Notification",
    "NotificationDeferred",
    "NotificationDelivery",
    "NotificationKind",
    "NotificationQueue",
    "NotificationRefused",
    "NotificationSink",
    "NotificationState",
    "ScreenNotificationSink",
    "default_dedup_key",
    "next_occurrence",
    "normalize_dedup_key",
    "parse_repeat_rule",
]
