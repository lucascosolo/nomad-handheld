"""The output vocabulary of `nomad.input`: `InputAction`, `TouchEvent` and
`ChoiceSelection`.

Three shapes, not one, because they answer different questions. `InputAction`
is what a button or the joystick resolved to *logically* — it never carries a
position. `TouchEvent` is a position with no logical meaning attached; D13 is
explicit that touch must not be forced into synthesized navigation, so it
passes through as coordinates and a phase, nothing more.

`InputAction.phase` is what lets one stream serve both a menu (edge-triggered:
`PRESS` and `RELEASE` only) and a game (hold-to-move: also consumes `REPEAT`)
without forking the pipeline — see `mapper.py` for how the phases are
produced.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from nomad.protocol.messages import TouchPhase


class ActionPhase(StrEnum):
    PRESS = "press"
    RELEASE = "release"
    REPEAT = "repeat"


class InputSource(StrEnum):
    """What physical control produced the action, for logging/debugging only
    — never a `ButtonId` or axis, per D13."""

    BUTTON = "button"
    JOYSTICK = "joystick"


class InputAction(BaseModel):
    """A logical action edge or repeat, timestamped on an injectable clock."""

    action: str
    phase: ActionPhase
    ts: float
    source: InputSource

    model_config = {"frozen": True}


class TouchEvent(BaseModel):
    """A raw touch position, passed through untouched (D13: no synthesized
    navigation from touch)."""

    x: int
    y: int
    phase: TouchPhase
    ts: float

    model_config = {"frozen": True}


class ChoiceSelection(BaseModel):
    """An option of a `choice` screen, picked by whoever drew the screen.

    The third shape, and it is not a `TouchEvent` that got promoted. It never
    carries a position, because the position was already resolved into a
    meaning by the only side that could resolve it correctly — the panel, which
    owns the fonts and the layout. Nothing on the Pi hit-tests coordinates, so
    D13 holds: navigation is still never synthesized from touch here.

    It is not an `InputAction` either. An action is `NAV_UP` or `CONFIRM` —
    something a *control* did, with no idea what is on screen. This names an
    option of a specific question, so it is only meaningful to a consumer that
    asked one, and a consumer that has moved on must be able to tell that it is
    stale. `option` is what makes that possible.
    """

    index: int
    #: The label the sender believes it drew. Empty means "not stated".
    option: str
    ts: float

    model_config = {"frozen": True}
