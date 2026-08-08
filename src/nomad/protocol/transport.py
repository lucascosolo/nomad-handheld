"""Moving `bytes`, and nothing else (D2).

A `Transport` does not know what a frame is and does not know what a message
means. It hands over whatever bytes arrived, in whatever sizes they arrived in
— a transport that inspected a payload to make a decision would be the exact
violation D2 exists to prevent.

Two implementations ship, both mock (D9): the suite must pass on a laptop with
no serial port and no hardware attached.

**There is deliberately no serial transport here.** `pyserial-asyncio` is an
optional extra, no firmware exists yet to talk to, and a serial transport
written against imagined hardware is a liability rather than a head start. It
arrives with the drivers that need it, implementing this same Protocol.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from nomad.core.errors import TransportError
from nomad.core.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class Transport(Protocol):
    """A bidirectional byte pipe."""

    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, data: bytes) -> None: ...

    async def receive(self) -> bytes:
        """Block until at least one byte is available and return it.

        Returns `b""` — and keeps returning it — once the transport is closed.
        A reader loop must treat empty as end-of-stream and stop, not spin.
        """
        ...


class _QueueTransport:
    """Shared machinery: an inbound queue plus a closed flag.

    Both mocks differ only in what `send` does, so the read side lives once
    here rather than being written twice slightly differently.
    """

    name = "queue"

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self._closed = False

    async def stop(self) -> None:
        self.close()

    def close(self) -> None:
        """Close the pipe and wake any pending `receive`."""
        if self._closed:
            return
        self._closed = True
        self._inbound.put_nowait(b"")

    def deliver(self, data: bytes) -> None:
        """Push bytes to be read by `receive`. Chunk boundaries are preserved,
        which is what lets a test drive a stream one byte at a time."""
        if self._closed:
            raise TransportError(f"transport '{self.name}' is closed")
        if data:
            self._inbound.put_nowait(data)

    async def receive(self) -> bytes:
        if self._closed and self._inbound.empty():
            return b""
        return await self._inbound.get()


class LoopbackTransport(_QueueTransport):
    """Everything written comes back as a read.

    For exercising a whole `Link` — framing, codec, seq accounting — with no
    peer and no hardware. Note that a `Link` on a loopback therefore receives
    its own `seq` values, which is exactly what makes seq handling testable.
    """

    name = "loopback"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise TransportError("loopback transport is closed")
        self.sent.append(data)
        self._inbound.put_nowait(data)


class MockTransport(_QueueTransport):
    """Scriptable both ways, for driver tests.

    Inbound bytes are supplied by `script` (up front) or `deliver` (mid-test);
    outbound bytes accumulate in `sent` for assertions. Nothing echoes.
    """

    name = "mock"

    def __init__(self, *, script: list[bytes] | None = None, fail_on_send: bool = False) -> None:
        super().__init__()
        self.sent: list[bytes] = []
        self.fail_on_send = fail_on_send
        for chunk in script or []:
            self.deliver(chunk)

    def script(self, *chunks: bytes) -> None:
        """Queue inbound chunks. Each chunk is delivered to `receive` intact."""
        for chunk in chunks:
            self.deliver(chunk)

    def script_bytewise(self, data: bytes) -> None:
        """Queue `data` one byte per chunk — the worst case a framer must survive."""
        for index in range(len(data)):
            self.deliver(data[index : index + 1])

    async def send(self, data: bytes) -> None:
        if self.fail_on_send:
            raise TransportError("mock transport configured to fail on send")
        if self._closed:
            raise TransportError("mock transport is closed")
        self.sent.append(data)

    @property
    def sent_bytes(self) -> bytes:
        return b"".join(self.sent)
