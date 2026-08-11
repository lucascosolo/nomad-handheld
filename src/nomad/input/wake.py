"""The tap that starts a turn: Nomad's Wake button (D44).

The idle screen is drawn as a one-option `choice` — the answer, or `idle`, with
a **Wake** row under it — so the panel already knows how to report a tap on it
(`input.choice`). This module is what that tap reaches: the `InputBroker`'s
idle handler, holding a callable that starts a self-directed turn.

It is deliberately not a raw touch handler, even though the panel reports touch
on every screen and that would need no screen change at all. A turn costs
money and this device lives in a pocket; any-tap-wakes is one brush of fabric
away from spending it. A named option is a press the operator aimed, and the
label is checked before it counts.

The callable arrives injected rather than imported, for the reason `triggers`
gets its readiness probe the same way: `input` sits below `agent` in the
dependency graph and must not learn what a turn is to notice a finger.
"""

from __future__ import annotations

from collections.abc import Callable

from nomad.core.logging import get_logger
from nomad.input.events import ChoiceSelection, InputAction, TouchEvent

logger = get_logger(__name__)

#: The label drawn on the idle screen, and the one a tap must carry to count.
#: One string, exported, because the renderer draws it and this module checks
#: it — two spellings of it would be a button that draws and never fires.
WAKE_LABEL = "Wake"

#: What `fire` returns: `True` if a turn was started, `False` if one was
#: already in flight. Not a coroutine — see D44 on why starting a turn must not
#: be awaited by the thing that drew the button.
Fire = Callable[[], bool]


class WakeAffordance:
    """Turns a tap on the idle screen's one option into a self-directed turn."""

    def __init__(self, fire: Fire, *, label: str = WAKE_LABEL) -> None:
        self._fire = fire
        self._label = label
        #: Taps that started a turn, and taps that landed while one was already
        #: running. Counted rather than logged only, because "the button does
        #: nothing" and "the button works and Nomad is already thinking" look
        #: identical from outside and are answered by these two numbers.
        self.woke = 0
        self.ignored = 0

    @property
    def label(self) -> str:
        return self._label

    async def __call__(self, event: InputAction | TouchEvent | ChoiceSelection) -> None:
        """The `IdleHandler` signature. Everything that is not the button is
        dropped in silence — while idle that is most of what arrives."""
        if not isinstance(event, ChoiceSelection):
            return
        if event.option != self._label:
            # A stale answer to a question that has gone, or a screen this
            # module did not draw. The label is the whole check: an index
            # would match option 0 of anything.
            logger.debug("Ignored a choice that is not the wake button")
            return
        if self._fire():
            self.woke += 1
            logger.info("Woken by the operator")
            return
        self.ignored += 1
        logger.info("Wake tap ignored: a turn is already running")
