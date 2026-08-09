"""The logical action set (D13, D26).

Core actions exist unconditionally and can never be removed — a menu built
against `CONFIRM`/`BACK` must work on every device this ships on. Everything
else is opt-in: the system or an app registers a name (`ASSISTANT` being the
motivating example from D26) before anything may be mapped to it. That split
is what makes "register a new action" and "map a button to an unregistered
name" two different operations with two different outcomes — the first is
how the action set grows, the second is a config bug that must fail loudly
rather than silently doing nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from nomad.core.errors import NomadError

# Named constants rather than bare strings at the call sites: a consumer that
# compares against a typo silently never matches, and a menu that never
# responds to CONFIRM is a bug with no error message.
ACTION_NAV_UP = "NAV_UP"
ACTION_NAV_DOWN = "NAV_DOWN"
ACTION_NAV_LEFT = "NAV_LEFT"
ACTION_NAV_RIGHT = "NAV_RIGHT"
ACTION_CONFIRM = "CONFIRM"
ACTION_BACK = "BACK"
ACTION_1 = "ACTION_1"
ACTION_2 = "ACTION_2"
#: Held to capture, released to stop (D37). Core rather than app-registered:
#: it is the *only* path that may ever start a microphone recording, and the
#: joystick/buttons are physically disconnected today, so this is driven from
#: the touch/action layer directly rather than a physical mapping.
ACTION_PUSH_TO_TALK = "PUSH_TO_TALK"

CORE_ACTIONS: frozenset[str] = frozenset(
    {
        ACTION_NAV_UP,
        ACTION_NAV_DOWN,
        ACTION_NAV_LEFT,
        ACTION_NAV_RIGHT,
        ACTION_CONFIRM,
        ACTION_BACK,
        ACTION_1,
        ACTION_2,
        ACTION_PUSH_TO_TALK,
    }
)


class UnknownActionError(NomadError):
    """A physical control was mapped to an action nobody registered."""


class ActionRegistry:
    """The runtime-extensible action set.

    Core actions are seeded in and cannot be removed. `register` is how the
    system or a self-authored app adds a new one; `require` is what a mapper
    calls before it will emit an event for a name, so an unregistered target
    fails at mapping time instead of producing an action nothing expects.
    """

    def __init__(self, extra_actions: Iterable[str] = ()) -> None:
        self._actions: set[str] = set(CORE_ACTIONS)
        for name in extra_actions:
            self.register(name)

    def register(self, name: str) -> None:
        self._actions.add(name)

    def is_registered(self, name: str) -> bool:
        return name in self._actions

    def require(self, name: str) -> str:
        if name not in self._actions:
            raise UnknownActionError(f"action {name!r} is not registered", details={"action": name})
        return name

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(self._actions)
