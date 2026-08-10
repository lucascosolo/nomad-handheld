"""The missing half of D13: what carries a press from the cable to the stream.

`InputStream.feed_button`, `feed_joystick` and `feed_touch` existed, were
tested, and had **no caller anywhere outside their own package**. The mapper
knew how to turn a pin into an action, the prompter knew how to turn an action
into an answer, and nothing joined the two — so a device with a real panel
attached still could not answer a single question. An adversarial reviewer
found it by booting the app rather than by reading it, which is the only way
this class of gap is ever found.

This is deliberately a *component*, not a helper function. It owns a task that
lives as long as the device does, and `core.lifecycle` is the one place tasks
are allowed to be owned.

**It is the single consumer of `Link.messages()`, and that is a contract.**
The link has one inbox queue: a second `async for` over it steals messages from
the first, and input that arrives every other press is worse than input that
never arrives, because the first looks like flaky hardware. If something else
ever needs the inbound stream, it subscribes here — it does not open a second
iterator.

Unknown and malformed messages are counted and dropped, never raised. A panel
running newer firmware than the Pi is a normal state on a device that flashes
itself (D22), and a reader task that dies on one bad frame takes every future
press with it.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from nomad.core.errors import ProtocolError
from nomad.core.logging import get_logger
from nomad.input.stream import InputStream
from nomad.protocol.link import Link
from nomad.protocol.messages import (
    InputButton,
    InputChoice,
    InputJoystick,
    InputTouch,
    Message,
    MessageType,
)

logger = get_logger(__name__)


@dataclass
class RouterStats:
    """What arrived and what did not. Read by `nomad status`, and the only way
    to tell "nobody is pressing anything" from "presses are being dropped"."""

    buttons: int = 0
    joystick: int = 0
    touches: int = 0
    #: Taps the panel resolved to an option of a `choice` screen.
    choices: int = 0
    #: Messages this build has no handler for. Expected and harmless.
    ignored: int = 0
    #: Messages of a known input type whose payload would not parse. Not
    #: harmless — this is firmware and Pi disagreeing about a shape.
    malformed: int = 0


class InputRouter:
    """Reads the panel's input messages and feeds them to the logical layer."""

    name = "input_router"

    def __init__(self, link: Link, stream: InputStream) -> None:
        self._link = link
        self._stream = stream
        self._task: asyncio.Task[None] | None = None
        self.stats = RouterStats()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        try:
            async for message in self._link.messages():
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead router is silent hardware
            # Logged loudly because the symptom is indistinguishable from a
            # broken panel: the screen still draws, and nothing the operator
            # presses ever does anything.
            logger.error("Input router stopped", extra={"error": f"{type(exc).__name__}: {exc}"})

    async def _dispatch(self, message: Message) -> None:
        handlers = {
            str(MessageType.INPUT_BUTTON): (InputButton, self._stream.feed_button, "buttons"),
            str(MessageType.INPUT_JOYSTICK): (
                InputJoystick,
                self._stream.feed_joystick,
                "joystick",
            ),
            str(MessageType.INPUT_TOUCH): (InputTouch, self._stream.feed_touch, "touches"),
            str(MessageType.INPUT_CHOICE): (InputChoice, self._stream.feed_choice, "choices"),
        }
        entry = handlers.get(message.type)
        if entry is None:
            self.stats.ignored += 1
            return
        model, feed, counter = entry
        try:
            payload = message.parse_payload(model)
        except ProtocolError as exc:
            self.stats.malformed += 1
            logger.warning(
                "Dropped an unparseable input message",
                extra={"type": message.type, "error": str(exc)},
            )
            return
        await feed(payload)  # type: ignore[arg-type]
        setattr(self.stats, counter, getattr(self.stats, counter) + 1)
