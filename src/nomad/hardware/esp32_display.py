"""Speaks `display.*` messages to the ESP32-S3 over a `protocol.Link` (D2, D3).

**A driver composes a `Link`; it does not touch bytes, framing, or a serial
port directly (D2).** Everything below `Link` — `Codec`, `Framing`,
`Transport` — is invisible from here on purpose, which is what makes this
driver constructible and testable against `MockTransport` with no serial port
and no firmware present.

Nomad's display vocabulary (text, card, list, choice) travels as a structured
`display.state` message. It does **not** get rasterised here and it does not
get smuggled through `display.draw`'s `pixels` field: the ESP32 owns the panel
and its fonts, so it owns layout, and a card is a few dozen bytes as structure
against 8-11 KB as a pixel region on a link it shares with input events.
`display.draw` remains for the cases that genuinely are pixels.
"""

from __future__ import annotations

from nomad.core.logging import get_logger
from nomad.protocol import DisplayBacklight, DisplayState, Link, ScreenKind

logger = get_logger(__name__)


class Esp32Display:
    """The screen, driven over a `protocol.Link` to the ESP32-S3 (D9)."""

    def __init__(self, link: Link, *, width: int = 320, height: int = 240) -> None:
        self._link = link
        self._width = width
        self._height = height

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        await self._send(DisplayState(kind=ScreenKind.TEXT, title=title or "", body=text))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        await self._send(
            DisplayState(kind=ScreenKind.CARD, title=title, body=body, rows=list(rows))
        )

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None:
        await self._send(
            DisplayState(
                kind=ScreenKind.LIST, title=title, items=list(items), selectable=selectable
            )
        )

    async def show_choice(self, question: str, options: list[str]) -> None:
        await self._send(
            DisplayState(kind=ScreenKind.CHOICE, question=question, options=list(options))
        )

    async def set_backlight(self, level: int) -> None:
        """Not part of `DisplayDriver` — extra ground this driver covers
        because the wire message already exists (`display.backlight`)."""
        await self._link.send_payload(DisplayBacklight(level=level))

    async def _send(self, state: DisplayState) -> None:
        await self._link.send_payload(state)
        logger.debug("Sent display.state", extra={"kind": str(state.kind)})
