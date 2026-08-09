"""Physical events in, logical `InputAction`/`TouchEvent` out (D13).

`InputMapper` is the only object in this codebase allowed to know a
`ButtonId` or a joystick axis exists. Everything above it — UI, apps, the
edge-vs-repeat decision — sees `action` strings and `ActionPhase` only.

## Edge and repeat on one stream

A menu wants one `PRESS` per press; a game wants `PRESS` then, if the control
is still held, a `REPEAT` after an initial delay and then at a faster
interval. Both are served by the *same* method calls rather than two
pipelines: `on_button`/`on_joystick` emit `PRESS`/`RELEASE` on the edges, and
`tick()` — called on a cadence by whatever owns the clock, real or fake —
emits `REPEAT` for whatever is still held. A menu consumer just never calls
`tick()`, or calls it and ignores `REPEAT` phases; a game consumer uses both.
Nothing about the producer has to guess which one it's talking to.

## Joystick deadzone and hysteresis

A stick at rest jitters within a few percent of centre, and a stick held
near the mapping threshold would otherwise chatter between "centred" and a
`NAV_*` direction every frame. Two thresholds fix both: `deadzone` (from
config) must be crossed to *enter* a direction, but only the smaller
`hysteresis * deadzone` needs to be re-crossed going the other way to *leave*
it. Screen/stick convention: +x is right, +y is down.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from nomad.core.config import InputConfig
from nomad.input.actions import ActionRegistry
from nomad.input.events import ActionPhase, InputAction, InputSource, TouchEvent
from nomad.protocol.messages import ButtonId, InputButton, InputJoystick, InputTouch, KeyPhase

_TIME_EPSILON_S = 1e-6

_NAV_LEFT = "NAV_LEFT"
_NAV_RIGHT = "NAV_RIGHT"
_NAV_UP = "NAV_UP"
_NAV_DOWN = "NAV_DOWN"


@dataclass
class _Held:
    action: str
    source: InputSource
    pressed_at: float
    last_emit: float
    repeating: bool = False


class InputMapper:
    """Maps `protocol` `input.*` payloads to the logical action stream."""

    def __init__(
        self,
        config: InputConfig,
        *,
        registry: ActionRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self.registry = registry if registry is not None else ActionRegistry(config.extra_actions)
        self._clock = clock
        self._buttons_held: dict[ButtonId, _Held] = {}
        self._nav_held: _Held | None = None
        self._nav_direction: str | None = None

    # -- buttons -------------------------------------------------------

    def on_button(self, payload: InputButton, *, now: float | None = None) -> list[InputAction]:
        now = now if now is not None else self._clock()
        action_name = self.registry.require(self._button_action(payload.button))

        if payload.phase == KeyPhase.PRESS:
            if payload.button in self._buttons_held:
                return []  # already down; a duplicate press is not an edge
            self._buttons_held[payload.button] = _Held(
                action=action_name, source=InputSource.BUTTON, pressed_at=now, last_emit=now
            )
            return [
                InputAction(
                    action=action_name, phase=ActionPhase.PRESS, ts=now, source=InputSource.BUTTON
                )
            ]

        held = self._buttons_held.pop(payload.button, None)
        if held is None:
            return []  # spurious release with no matching press
        return [
            InputAction(
                action=held.action, phase=ActionPhase.RELEASE, ts=now, source=InputSource.BUTTON
            )
        ]

    def _button_action(self, button: ButtonId) -> str:
        return getattr(self._config.buttons, button.value)

    # -- joystick --------------------------------------------------------

    def on_joystick(self, payload: InputJoystick, *, now: float | None = None) -> list[InputAction]:
        direction = self._resolve_direction(payload.x, payload.y)
        if direction == self._nav_direction:
            return []  # no edge; tick() handles repeat for whatever is held

        now = now if now is not None else self._clock()
        events: list[InputAction] = []
        if self._nav_held is not None:
            events.append(
                InputAction(
                    action=self._nav_held.action,
                    phase=ActionPhase.RELEASE,
                    ts=now,
                    source=InputSource.JOYSTICK,
                )
            )
            self._nav_held = None
        if direction is not None:
            action_name = self.registry.require(direction)
            self._nav_held = _Held(
                action=action_name, source=InputSource.JOYSTICK, pressed_at=now, last_emit=now
            )
            events.append(
                InputAction(
                    action=action_name, phase=ActionPhase.PRESS, ts=now, source=InputSource.JOYSTICK
                )
            )
        self._nav_direction = direction
        return events

    def _resolve_direction(self, x: float, y: float) -> str | None:
        deadzone = self._config.joystick.deadzone
        threshold = (
            deadzone if self._nav_direction is None else deadzone * self._config.joystick.hysteresis
        )
        ax, ay = abs(x), abs(y)
        if max(ax, ay) < threshold:
            return None
        if ax >= ay:
            return _NAV_RIGHT if x > 0 else _NAV_LEFT
        return _NAV_DOWN if y > 0 else _NAV_UP

    # -- touch -------------------------------------------------------------

    def on_touch(self, payload: InputTouch, *, now: float | None = None) -> TouchEvent:
        now = now if now is not None else self._clock()
        return TouchEvent(x=payload.x, y=payload.y, phase=payload.phase, ts=now)

    # -- repeat --------------------------------------------------------

    def tick(self, *, now: float | None = None) -> list[InputAction]:
        """Emit `REPEAT` for anything still held long enough. Call this on
        whatever cadence the caller wants — real timer or, in tests, a
        directly-advanced fake clock."""
        now = now if now is not None else self._clock()
        events: list[InputAction] = []
        for held in list(self._buttons_held.values()):
            self._maybe_repeat(held, now, events)
        if self._nav_held is not None:
            self._maybe_repeat(self._nav_held, now, events)
        return events

    def _maybe_repeat(self, held: _Held, now: float, events: list[InputAction]) -> None:
        delay_s = self._config.repeat.delay_ms / 1000
        interval_s = self._config.repeat.interval_ms / 1000
        if not held.repeating:
            if now - held.pressed_at >= delay_s - _TIME_EPSILON_S:
                held.repeating = True
                held.last_emit = now
                events.append(
                    InputAction(
                        action=held.action, phase=ActionPhase.REPEAT, ts=now, source=held.source
                    )
                )
        elif now - held.last_emit >= interval_s - _TIME_EPSILON_S:
            held.last_emit = now
            events.append(
                InputAction(
                    action=held.action, phase=ActionPhase.REPEAT, ts=now, source=held.source
                )
            )
