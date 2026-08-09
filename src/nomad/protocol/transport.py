"""Moving `bytes`, and nothing else (D2).

A `Transport` does not know what a frame is and does not know what a message
means. It hands over whatever bytes arrived, in whatever sizes they arrived in
— a transport that inspected a payload to make a decision would be the exact
violation D2 exists to prevent.

Three implementations ship. Two are mocks and **mock is still the default**
(D9): the suite must pass on a laptop with no serial port and no hardware
attached, and `SerialTransport` is written so that merely importing this module
costs nothing — `pyserial-asyncio` is an optional extra, imported inside
`start()` and nowhere else.

`SerialTransport` arrived with real hardware to talk to (a Hosyond ESP32-S3
module on `/dev/ttyACM0`), which is the bar this module's earlier docstring set
for writing it. It is not special-cased anywhere above: a `Link` cannot tell it
from `MockTransport`, which is the whole point of D2.
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


class SerialTransport:
    """Bytes over a real serial port, via `pyserial-asyncio` (optional extra).

    **The import lives inside `start()` on purpose.** `nomad.protocol` is
    imported by everything, and the suite runs on a laptop with no serial port
    and no `pyserial-asyncio` installed (D9). A module-level import would make
    an optional extra mandatory for anyone who so much as touches a `Link`.

    A missing dependency and an unopenable port both raise `TransportError`,
    because from a caller's position they are the same event: this transport
    cannot carry bytes. Nothing above here should have to know which it was in
    order to fall back.
    """

    def __init__(self, port: str, *, baudrate: int = 921600, name: str | None = None) -> None:
        if not port:
            raise TransportError("a serial transport needs a port; none was configured")
        self.name = name or f"serial:{port}"
        self._port = port
        self._baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def port(self) -> str:
        return self._port

    @property
    def baudrate(self) -> int:
        return self._baudrate

    async def start(self) -> None:
        if not self._closed:
            return
        try:
            import serial_asyncio
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise TransportError(
                "the serial transport needs the 'serial' extra: pip install -e '.[serial]'",
                {"port": self._port},
            ) from exc

        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port, baudrate=self._baudrate
            )
        except Exception as exc:  # pragma: no cover - depends on real hardware
            raise TransportError(
                f"could not open serial port '{self._port}': {exc}",
                {"port": self._port, "baudrate": self._baudrate},
            ) from exc

        self._closed = False
        logger.info("serial transport open", extra={"port": self._port, "baudrate": self._baudrate})

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception as exc:  # pragma: no cover - close paths vary by platform
            # A port that failed to close cleanly is still closed as far as
            # this process is concerned; raising here would strand callers in
            # shutdown for something they cannot act on.
            logger.warning("serial transport close failed", extra={"error": str(exc)})

    async def send(self, data: bytes) -> None:
        if self._closed or self._writer is None:
            raise TransportError(f"transport '{self.name}' is closed")
        if not data:
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:  # pragma: no cover - depends on real hardware
            self._closed = True
            raise TransportError(f"serial write failed on '{self._port}': {exc}") from exc

    async def receive(self) -> bytes:
        """Up to one buffer of whatever arrived. `b""` means end-of-stream.

        A serial port hands back arbitrary chunk boundaries — a frame may
        arrive in seven pieces, or seven frames in one. That is the framer's
        problem by construction (D2), and `Framing` is already tested against
        the byte-at-a-time worst case.
        """
        if self._closed or self._reader is None:
            return b""
        try:
            data = await self._reader.read(4096)
        except Exception as exc:  # pragma: no cover - depends on real hardware
            self._closed = True
            logger.warning("serial read failed", extra={"port": self._port, "error": str(exc)})
            return b""
        if not data:
            # EOF: the peer unplugged or reset. Latch closed so a reader loop
            # sees a steady stream of b"" and stops, rather than spinning.
            self._closed = True
            return b""
        return data
