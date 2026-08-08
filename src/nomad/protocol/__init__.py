"""Wire protocol for the two USB CDC hardware links (D2, D3).

Three concerns, three objects, and the separation is the point:

* `Framing` delimits a byte stream into frames and knows nothing of meaning.
* `Codec` turns a `Message` into bytes and back; `JsonCodec` is the starting
  implementation and is explicitly replaceable.
* `Transport` moves bytes and knows nothing of framing or meaning.

`Link` composes all three into "send a `Message`, receive `Messages`", and owns
the two things that only exist once they are composed: the `seq` counter, and
detecting that the peer rebooted.

This package depends on `core` and nothing else. It has no hardware
dependency: both shipped transports are mocks, so the whole protocol is
exercised on a laptop with no serial port (D9).
"""

from __future__ import annotations

from nomad.protocol.codec import Codec, JsonCodec
from nomad.protocol.framing import (
    DEFAULT_MAX_FRAME_BYTES,
    OVERHEAD_BYTES,
    SYNC,
    FeedResult,
    FrameLoss,
    FrameLossReason,
    Framing,
    checksum,
)
from nomad.protocol.link import (
    DEFAULT_REBOOT_SEQ_THRESHOLD,
    Link,
    LinkStats,
    RebootEvent,
    RebootHandler,
)
from nomad.protocol.messages import (
    CATALOGUE,
    SEQ_MODULUS,
    Binary,
    ButtonId,
    DisplayBacklight,
    DisplayBlit,
    DisplayDraw,
    DisplayLinkStatus,
    HidKey,
    HidLinkStatus,
    HidPointer,
    InputButton,
    InputJoystick,
    InputTouch,
    KeyPhase,
    LinkKind,
    Message,
    MessageType,
    PayloadModel,
    SystemError,
    SystemHello,
    TouchPhase,
    catalogue_for,
    known_types,
    payload_model_for,
)
from nomad.protocol.transport import LoopbackTransport, MockTransport, Transport

__all__ = [
    "CATALOGUE",
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_REBOOT_SEQ_THRESHOLD",
    "OVERHEAD_BYTES",
    "SEQ_MODULUS",
    "SYNC",
    "Binary",
    "ButtonId",
    "Codec",
    "DisplayBacklight",
    "DisplayBlit",
    "DisplayDraw",
    "DisplayLinkStatus",
    "FeedResult",
    "FrameLoss",
    "FrameLossReason",
    "Framing",
    "HidKey",
    "HidLinkStatus",
    "HidPointer",
    "InputButton",
    "InputJoystick",
    "InputTouch",
    "JsonCodec",
    "KeyPhase",
    "Link",
    "LinkKind",
    "LinkStats",
    "LoopbackTransport",
    "Message",
    "MessageType",
    "MockTransport",
    "PayloadModel",
    "RebootEvent",
    "RebootHandler",
    "SystemError",
    "SystemHello",
    "TouchPhase",
    "Transport",
    "catalogue_for",
    "checksum",
    "known_types",
    "payload_model_for",
]
