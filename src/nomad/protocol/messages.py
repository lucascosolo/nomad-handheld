"""The wire envelope and the message catalogue (D3).

Every frame carries `type`, `id`, `seq`, `payload`. `id` correlates a response
with its request; `seq` is monotonic per direction and is what tells the Pi a
microcontroller rebooted (see `link.py`). Both exist because `id` alone cannot
distinguish a late response from a wrong one once a peer's counters have reset.

## `payload` is a plain dict, on purpose

`Message.payload` is `dict[str, Any]`, not a discriminated union of the models
below. That is what makes rule 1 of the extension policy work: an unrecognised
`type` still *decodes*, so it can be ignored deliberately at the link layer
instead of failing validation and being mistaken for a corrupt frame. The typed
models are opt-in — `Message.build` on the way out, `parse_payload` on the way
in — and they carry the field-level validation the envelope cannot.

For the same reason the envelope ignores unknown fields rather than forbidding
them. Forbidding would mean a firmware that adds one envelope field kills the
link on every frame; nothing above this layer makes a decision on the envelope,
so there is nothing to protect by being strict.

## `system.status` means two different shapes

The draft catalogue gives `system.status` `{uptime_ms, free_heap,
last_seq_seen}` on the ESP32 link and `{uptime_ms, host_connected}` on the
RP2040 link — which is the thing its own extension rule 3 forbids. The two
links are physically separate and never see each other's traffic, so the
catalogue is keyed by `LinkKind` and both shapes coexist safely. It is still
worth minting distinct type strings if a third link ever appears.

## Binary payload fields

`display.draw` carries raw pixels, and JSON cannot. `Binary` fields hold
`bytes` in Python and travel as base64 in the payload dict, so a `Message`
round-trips through `JsonCodec` unchanged. The cost is 33% on the one message
type that is already the largest, over a 921600-baud link shared with input
events — which is the concrete reason a binary codec swap exists (D2), not a
stylistic one.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from nomad.core.errors import ProtocolError

#: `seq` is a u32 on the wire. At one frame per millisecond this wraps after
#: roughly seven weeks of continuous traffic, so wrap is rare but not
#: unreachable and `link.py` distinguishes it from a reboot explicitly.
SEQ_MODULUS = 2**32


def _to_bytes(value: Any) -> Any:
    if isinstance(value, str):
        return base64.b64decode(value, validate=True)
    return value


def _to_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


#: Raw bytes in Python, base64 in the payload dict. A `str` arriving here is
#: always read as base64 — a binary field has no other sensible reading.
Binary = Annotated[
    bytes,
    BeforeValidator(_to_bytes),
    PlainSerializer(_to_base64, return_type=str, when_used="always"),
]


class LinkKind(StrEnum):
    """Which of the two USB CDC links a message belongs to."""

    #: Pi <-> ESP32-S3. Display out, input in.
    DISPLAY = "display"
    #: Pi <-> RP2040-Zero. HID out, status in. No sensors on this link.
    HID = "hid"


class MessageType(StrEnum):
    """The catalogue's type strings. `Message.type` stays a plain `str` so an
    unrecognised type from newer firmware decodes rather than raising."""

    DISPLAY_DRAW = "display.draw"
    DISPLAY_BLIT = "display.blit"
    DISPLAY_BACKLIGHT = "display.backlight"
    INPUT_TOUCH = "input.touch"
    INPUT_JOYSTICK = "input.joystick"
    INPUT_BUTTON = "input.button"
    HID_KEY = "hid.key"
    HID_POINTER = "hid.pointer"
    SYSTEM_HELLO = "system.hello"
    SYSTEM_STATUS = "system.status"
    SYSTEM_ERROR = "system.error"


class TouchPhase(StrEnum):
    DOWN = "down"
    MOVE = "move"
    UP = "up"


class KeyPhase(StrEnum):
    PRESS = "press"
    RELEASE = "release"


class ButtonId(StrEnum):
    """Physical button identity. Device-local and never referenced above
    `nomad.input`, which normalises these into logical actions (D13)."""

    A = "a"
    B = "b"
    X = "x"
    Y = "y"


class PayloadModel(BaseModel):
    """Base for catalogue payloads.

    Unknown fields are ignored rather than rejected: new payload fields are
    additive and optional, so an older Pi must keep parsing a newer firmware's
    messages.
    """

    model_config = ConfigDict(extra="ignore")

    message_type: ClassVar[MessageType]


class DisplayDraw(PayloadModel):
    """A pixel region. `pixels` is raw or RLE — the codec does not interpret it."""

    message_type: ClassVar[MessageType] = MessageType.DISPLAY_DRAW

    x: int
    y: int
    w: int = Field(gt=0)
    h: int = Field(gt=0)
    pixels: Binary


class DisplayBlit(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.DISPLAY_BLIT

    x: int
    y: int
    region_id: str


class DisplayBacklight(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.DISPLAY_BACKLIGHT

    level: int = Field(ge=0, le=255)


class InputTouch(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.INPUT_TOUCH

    x: int
    y: int
    phase: TouchPhase


class InputJoystick(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.INPUT_JOYSTICK

    x: float = Field(ge=-1.0, le=1.0)
    y: float = Field(ge=-1.0, le=1.0)


class InputButton(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.INPUT_BUTTON

    button: ButtonId
    phase: KeyPhase


class HidKey(PayloadModel):
    """A keystroke into another computer. `EXTERNAL_DEVICE` risk and
    `never_auto` in every permission mode (D14) — enforced in `tools`, stated
    here so nobody reads this model as ordinary output."""

    message_type: ClassVar[MessageType] = MessageType.HID_KEY

    keycode: int = Field(ge=0)
    phase: KeyPhase


class HidPointer(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.HID_POINTER

    dx: int
    dy: int
    buttons: int = Field(ge=0, default=0)


class SystemHello(PayloadModel):
    """Sent on connect and after a detected reboot. `capabilities` is how the
    Pi learns which message versions this firmware speaks."""

    message_type: ClassVar[MessageType] = MessageType.SYSTEM_HELLO

    firmware_version: str = ""
    capabilities: list[str] = Field(default_factory=list)


class DisplayLinkStatus(PayloadModel):
    """`system.status` on the ESP32 link."""

    message_type: ClassVar[MessageType] = MessageType.SYSTEM_STATUS

    uptime_ms: int = Field(ge=0)
    free_heap: int = Field(ge=0)
    last_seq_seen: int = Field(ge=0)


class HidLinkStatus(PayloadModel):
    """`system.status` on the RP2040 link. Different shape, same type string —
    see the module docstring."""

    message_type: ClassVar[MessageType] = MessageType.SYSTEM_STATUS

    uptime_ms: int = Field(ge=0)
    host_connected: bool = False


class SystemError(PayloadModel):
    message_type: ClassVar[MessageType] = MessageType.SYSTEM_ERROR

    code: str
    message: str = ""


_DISPLAY_CATALOGUE: dict[str, type[PayloadModel]] = {
    MessageType.DISPLAY_DRAW: DisplayDraw,
    MessageType.DISPLAY_BLIT: DisplayBlit,
    MessageType.DISPLAY_BACKLIGHT: DisplayBacklight,
    MessageType.INPUT_TOUCH: InputTouch,
    MessageType.INPUT_JOYSTICK: InputJoystick,
    MessageType.INPUT_BUTTON: InputButton,
    MessageType.SYSTEM_HELLO: SystemHello,
    MessageType.SYSTEM_STATUS: DisplayLinkStatus,
    MessageType.SYSTEM_ERROR: SystemError,
}

_HID_CATALOGUE: dict[str, type[PayloadModel]] = {
    MessageType.HID_KEY: HidKey,
    MessageType.HID_POINTER: HidPointer,
    MessageType.SYSTEM_HELLO: SystemHello,
    MessageType.SYSTEM_STATUS: HidLinkStatus,
    MessageType.SYSTEM_ERROR: SystemError,
}

CATALOGUE: Mapping[LinkKind, Mapping[str, type[PayloadModel]]] = {
    LinkKind.DISPLAY: _DISPLAY_CATALOGUE,
    LinkKind.HID: _HID_CATALOGUE,
}


def catalogue_for(link: LinkKind) -> Mapping[str, type[PayloadModel]]:
    """The payload models a given link may legitimately carry."""
    return CATALOGUE[link]


def payload_model_for(link: LinkKind, message_type: str) -> type[PayloadModel] | None:
    """The model for a type on a link, or `None` if the link does not know it."""
    return CATALOGUE[link].get(message_type)


def known_types(link: LinkKind) -> frozenset[str]:
    return frozenset(CATALOGUE[link])


def _new_id() -> str:
    return uuid.uuid4().hex


P = TypeVar("P", bound=PayloadModel)


class Message(BaseModel):
    """The D3 envelope. The only thing a `Codec` ever encodes or decodes."""

    model_config = ConfigDict(extra="ignore")

    type: str
    id: str = Field(default_factory=_new_id)
    seq: int = Field(default=0, ge=0, lt=SEQ_MODULUS)
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        payload: PayloadModel,
        *,
        id: str | None = None,
        seq: int = 0,
    ) -> Message:
        """Wrap a typed payload in an envelope, taking `type` from the model."""
        return cls(
            type=str(payload.message_type),
            id=id or _new_id(),
            seq=seq,
            payload=payload.model_dump(),
        )

    def parse_payload(self, model: type[P]) -> P:
        """Validate this message's payload as `model`.

        Raises `ProtocolError` rather than pydantic's `ValidationError` so a
        caller only ever has to catch `NomadError`.
        """
        try:
            return model.model_validate(self.payload)
        except Exception as exc:
            raise ProtocolError(
                f"payload does not match {model.__name__}",
                {"type": self.type, "id": self.id, "error": str(exc)},
            ) from exc
