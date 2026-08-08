"""Views onto the session: the screen, and a way to watch it (D11).

A view owns no conversation state. It subscribes, it draws, and it may be
dropped by the bus mid-turn without the session noticing or the answer being
wrong — `agent.turn_finished` is what makes that trade safe.

Depends on `agent` and `tools` (for the event vocabularies it renders), on
`input` (for the answer half of a prompt, D32), and on the `DisplayDriver`
*protocol*, never on a concrete driver. Nothing depends on this package except
the composition root.

`ScreenOwner` is the one exception to "a view owns no state": it owns *who is
drawing*, because with F2 there is more than one writer and an authorization
prompt has to be able to win (D36).
"""

from __future__ import annotations

from nomad.view.authprompt import AuthorizationPrompter
from nomad.view.renderer import TurnRenderer
from nomad.view.screen import ScreenOwner, ScreenView
from nomad.view.server import ScreenServer

__all__ = [
    "AuthorizationPrompter",
    "ScreenOwner",
    "ScreenServer",
    "ScreenView",
    "TurnRenderer",
]
