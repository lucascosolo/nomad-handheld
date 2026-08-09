"""Building the transport named by `[transports.*].kind`.

This module exists because the config knob had no consumer. `[transports.esp32]`
sat in `nomad.toml` describing a port and a baud rate that nothing read, so
setting `kind = "serial"` changed precisely nothing — the sort of dead
configuration that reads as a feature and behaves as a lie.

`mock` stays the default (D9), and choosing it must not require the serial
extra to be installed, which is why `SerialTransport` defers its own import.
"""

from __future__ import annotations

from nomad.core.config import TransportConfig
from nomad.core.errors import TransportError
from nomad.core.logging import get_logger
from nomad.protocol.transport import MockTransport, SerialTransport, Transport

logger = get_logger(__name__)

#: Kinds that need no hardware and no optional dependency.
MOCK_KINDS = ("mock", "loopback")


def create_transport(config: TransportConfig, *, name: str | None = None) -> Transport:
    """Build the transport named by `config.kind`.

    Raises `TransportError` for an unknown kind rather than silently falling
    back to a mock: a typo in `kind` that quietly yields a mock would present
    as "the hardware is connected but says nothing", which is the single most
    expensive failure to diagnose on this device.
    """
    kind = config.kind

    if kind == "mock":
        return MockTransport()

    if kind == "loopback":
        from nomad.protocol.transport import LoopbackTransport

        return LoopbackTransport()

    if kind == "serial":
        return SerialTransport(config.port, baudrate=config.baudrate, name=name)

    raise TransportError(
        f"unknown transport kind '{kind}'",
        {"kind": kind, "known": ["mock", "loopback", "serial"]},
    )
