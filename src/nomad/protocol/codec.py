"""`Message` <-> `bytes` (D2).

The codec is the *only* object that knows what a frame body means. It knows
nothing about how bytes are delimited (`Framing`) or moved (`Transport`).

**JSON is not load-bearing.** It is here because it is trivial to read on a
serial console and cheap on both MCUs, not because anything depends on it. When
the display link needs binary framebuffer deltas, a `CborCodec` — or a
hand-rolled packed one — implements this same two-method Protocol and *no*
`Transport`, `Framing`, `Link` or driver code changes, because they only ever
dealt in `bytes` and `Message`. That swap being free is the entire reason D2
separates these three objects rather than shipping one `SerialProtocol` class.

The one thing a replacement must preserve is additive payload fields: JSON
gives that away, a binary codec has to buy it deliberately with explicit field
tags rather than positional encoding.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from nomad.core.errors import ProtocolError
from nomad.protocol.messages import Message


@runtime_checkable
class Codec(Protocol):
    """Turns a `Message` into a frame body and back."""

    name: str

    def encode(self, message: Message) -> bytes: ...

    def decode(self, data: bytes) -> Message: ...


class JsonCodec:
    """UTF-8 JSON. The starting codec, not the ending one."""

    name = "json"

    def encode(self, message: Message) -> bytes:
        return message.model_dump_json().encode("utf-8")

    def decode(self, data: bytes) -> Message:
        """Parse a frame body.

        Raises `ProtocolError` for anything malformed. A caller must treat that
        as frame loss — a body that survived its CRC but will not parse means
        the peer sent something this build does not understand, which is a
        reason to drop one frame and carry on, never to tear down the link.
        """
        try:
            return Message.model_validate_json(data)
        except ValidationError as exc:
            raise ProtocolError(
                "frame body is not a valid Message",
                {"bytes": len(data), "error": str(exc)},
            ) from exc
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "frame body is not valid UTF-8",
                {"bytes": len(data), "error": str(exc)},
            ) from exc
