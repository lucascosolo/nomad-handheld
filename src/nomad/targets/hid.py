"""HID target — keyboard/pointer output only (D12).

The RP2040-Zero is a keyboard and mouse to whatever it is plugged into. It is
modelled as a target with `HID_OUTPUT` and *nothing else* on purpose: with no
`FILESYSTEM` capability, a file tool aimed at it fails the capability check
before any permission logic runs. That is a structural guarantee, not a
convention.

`never_auto` (D14) covers any HID output in every mode. Implementation is
deferred to the chunk that owns the RP2040 transport.
"""

from __future__ import annotations

from collections.abc import Sequence

from nomad.targets.base import Capability, TargetKind

_UNIMPLEMENTED = "HID target not implemented; see docs/BUILD_LEDGER.md"


class HidTarget:
    """A USB HID output device (RP2040-Zero). Output weapon, never auto (D14)."""

    kind = TargetKind.HID
    capabilities = frozenset({Capability.HID_OUTPUT})

    def __init__(self, target_id: str = "hid", *, description: str = "USB HID output") -> None:
        self.id = target_id
        self.description = description

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"HidTarget(id={self.id!r})"

    async def send_keys(self, keys: Sequence[str]) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def send_text(self, text: str) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def move_pointer(self, dx: int, dy: int) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def click(self, button: str = "left", *, count: int = 1) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)
