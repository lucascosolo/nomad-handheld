"""Logical input normalization (D13).

Physical events — touch, joystick, buttons, whatever ships later — cross
into the logical action stream here and nowhere else. No module above this
one may reference a `ButtonId`, an axis, or a raw key code; everything
consumes `InputAction`/`TouchEvent` and the action-name strings from
`nomad.input.actions`.
"""

from __future__ import annotations

from nomad.input.actions import CORE_ACTIONS, ActionRegistry, UnknownActionError
from nomad.input.events import ActionPhase, InputAction, InputSource, TouchEvent
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream

__all__ = [
    "CORE_ACTIONS",
    "ActionPhase",
    "ActionRegistry",
    "InputAction",
    "InputMapper",
    "InputSource",
    "InputStream",
    "TouchEvent",
    "UnknownActionError",
]
