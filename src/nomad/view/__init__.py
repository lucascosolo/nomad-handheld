"""Views onto the session: the screen, and a way to watch it (D11).

A view owns no conversation state. It subscribes, it draws, and it may be
dropped by the bus mid-turn without the session noticing or the answer being
wrong — `agent.turn_finished` is what makes that trade safe.

Depends on `agent` (for the event vocabulary it renders) and on the
`DisplayDriver` *protocol*, never on a concrete driver. Nothing depends on
this package except the composition root.
"""

from __future__ import annotations

from nomad.view.renderer import TurnRenderer
from nomad.view.server import ScreenServer

__all__ = ["ScreenServer", "TurnRenderer"]
