"""Framing + Codec + Transport composed into "send a Message, receive Messages".

`Link` is the only object that holds all three, and it holds them without
letting any of them learn about the others: it hands bytes to `Framing`, frame
bodies to `Codec`, and encoded frames to `Transport`. Everything above this
module deals in `Message` and never in bytes.

## Reboot detection is the reason this class exists

A microcontroller reboots and USB re-enumerates, and the byte stream simply
continues — nothing at the transport layer marks the discontinuity. `seq`
resetting to a low value *is* the signal (D3). When `Link` sees it, three
things must happen before any further message is believed:

1. **Pending requests are discarded**, failed with `ProtocolError` rather than
   left to time out. A response the peer can no longer produce must not be
   waited on, and a *later* response reusing an `id` from before the reboot
   must not be matched to a request from the previous epoch. This is the
   correctness reason D3 carries `seq` in addition to `id`.
2. **The outgoing counter resets**, so the peer sees a fresh sequence rather
   than one continuing from a session it has no memory of.
3. **State is re-established** via `system.hello` instead of assumed. Continuity
   after a reboot is exactly the assumption that makes retry unsafe.

A `seq` wrap at 2^32 is deliberately indistinguishable from a reboot here and
is resolved *as* a reboot. The asymmetry decides it: a spurious re-handshake
costs one round trip roughly every four billion frames, while missing a real
reboot means acting on state the peer no longer has.

## An unknown `type` is ignored, never fatal

New message types are additive (extension rule 1). Seq accounting happens
*before* the type is looked at, so ignoring an unrecognised message cannot
desynchronise the stream — the frame is counted, optionally reported back as
`system.error`, and dropped. The same is true of a body that fails to decode:
one lost frame, never a torn-down link.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace

from nomad.core.errors import ProtocolError, TransportError
from nomad.core.logging import get_logger
from nomad.protocol.codec import Codec, JsonCodec
from nomad.protocol.framing import FrameLoss, FrameLossReason, Framing
from nomad.protocol.messages import (
    SEQ_MODULUS,
    LinkKind,
    Message,
    MessageType,
    PayloadModel,
    SystemError,
    SystemHello,
    known_types,
)
from nomad.protocol.transport import Transport

logger = get_logger(__name__)

#: A `seq` at or below this after a higher one means the peer restarted its
#: counter. Firmware sends `system.hello` first, so the first frames of a new
#: epoch sit in single digits; the margin absorbs a couple of lost frames.
DEFAULT_REBOOT_SEQ_THRESHOLD = 8

DEFAULT_INBOX_SIZE = 256
DEFAULT_REQUEST_TIMEOUT = 5.0

RebootHandler = Callable[["RebootEvent"], Awaitable[None]]


@dataclass(frozen=True)
class RebootEvent:
    """The peer's `seq` went backwards to a low value: it restarted."""

    link: str
    previous_seq: int
    observed_seq: int
    discarded_request_ids: tuple[str, ...]


@dataclass(frozen=True)
class LinkStats:
    """Counters worth watching on a cable that is physically unreliable."""

    bytes_sent: int = 0
    bytes_received: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    checksum_failures: int = 0
    oversized_frames: int = 0
    junk_bytes: int = 0
    decode_failures: int = 0
    unknown_types: int = 0
    seq_gaps: int = 0
    seq_regressions: int = 0
    reboots: int = 0
    discarded_requests: int = 0
    inbox_drops: int = 0


@dataclass
class _Counters:
    stats: LinkStats = field(default_factory=LinkStats)

    def bump(self, **deltas: int) -> None:
        current = self.stats
        self.stats = replace(
            current, **{name: getattr(current, name) + delta for name, delta in deltas.items()}
        )


class Link:
    """A bidirectional message link over one byte transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        kind: LinkKind,
        name: str | None = None,
        codec: Codec | None = None,
        framing: Framing | None = None,
        accepted_types: Collection[str] | None = None,
        hello: SystemHello | None = None,
        reboot_seq_threshold: int = DEFAULT_REBOOT_SEQ_THRESHOLD,
        hello_on_reboot: bool = True,
        report_unknown_types: bool = True,
        inbox_size: int = DEFAULT_INBOX_SIZE,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._transport = transport
        self._kind = kind
        self._name = name or f"{kind}-link"
        self._codec: Codec = codec or JsonCodec()
        self._framing = framing or Framing()
        self._accepted: frozenset[str] = (
            frozenset(accepted_types) if accepted_types is not None else known_types(kind)
        )
        self._hello = hello or SystemHello()
        self._reboot_seq_threshold = reboot_seq_threshold
        self._hello_on_reboot = hello_on_reboot
        self._report_unknown_types = report_unknown_types
        self._request_timeout = request_timeout

        self._out_seq = 0
        self._last_in_seq: int | None = None
        self._pending: dict[str, asyncio.Future[Message]] = {}
        self._inbox: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=inbox_size)
        self._reader: asyncio.Task[None] | None = None
        self._reboot_handlers: list[RebootHandler] = []
        self._counters = _Counters()

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> LinkKind:
        return self._kind

    @property
    def stats(self) -> LinkStats:
        return self._counters.stats

    @property
    def pending_ids(self) -> frozenset[str]:
        """Request ids awaiting a response. Emptied by a reboot."""
        return frozenset(self._pending)

    @property
    def last_received_seq(self) -> int | None:
        return self._last_in_seq

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self._transport.start()
        self._reader = asyncio.ensure_future(self._read_loop())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        self._fail_pending("link stopped")
        await self._transport.stop()
        self._close_inbox()

    # -- sending -----------------------------------------------------------

    async def send(self, message: Message) -> Message:
        """Stamp `seq`, encode, frame, and write. Returns the stamped message."""
        stamped = message.model_copy(update={"seq": self._next_seq()})
        await self._write(stamped)
        return stamped

    async def send_payload(self, payload: PayloadModel, *, id: str | None = None) -> Message:
        return await self.send(Message.build(payload, id=id))

    async def request(
        self,
        payload: PayloadModel,
        *,
        id: str | None = None,
        timeout: float | None = None,
    ) -> Message:
        """Send and await the response correlated on `id`.

        The pending entry is registered *before* the write, so a peer that
        answers faster than this coroutine resumes cannot have its response
        dropped as unsolicited.
        """
        message = Message.build(payload, id=id, seq=self._next_seq())
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[message.id] = future
        try:
            await self._write(message)
            return await asyncio.wait_for(future, timeout or self._request_timeout)
        except TimeoutError as exc:
            raise ProtocolError(
                "request timed out",
                {"link": self._name, "type": message.type, "id": message.id},
            ) from exc
        finally:
            self._pending.pop(message.id, None)

    async def send_hello(self) -> Message:
        """Announce this side and invite the peer's `system.hello` back."""
        return await self.send_payload(self._hello)

    def on_reboot(self, handler: RebootHandler) -> None:
        """Register a coroutine called after a detected peer reboot. Handler
        failures are logged and isolated — a bad handler must not stop the
        reader or mask the reboot from the others."""
        self._reboot_handlers.append(handler)

    # -- receiving ---------------------------------------------------------

    async def messages(self) -> AsyncIterator[Message]:
        """Inbound messages that were not consumed as a request's response."""
        while True:
            message = await self._inbox.get()
            if message is None:
                return
            yield message

    async def _read_loop(self) -> None:
        while True:
            try:
                chunk = await self._transport.receive()
            except TransportError as exc:
                logger.error(
                    "Transport failed; stopping reader",
                    extra={"link": self._name, "error": str(exc)},
                )
                self._fail_pending("transport failed")
                self._close_inbox()
                return
            if not chunk:
                self._close_inbox()
                return

            self._counters.bump(bytes_received=len(chunk))
            result = self._framing.feed(chunk)
            for loss in result.losses:
                self._record_loss(loss)
            for body in result.frames:
                try:
                    await self._handle_frame(body)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one frame must never kill the link
                    logger.error(
                        "Failed handling frame",
                        extra={"link": self._name, "error": str(exc)},
                    )

    async def _handle_frame(self, body: bytes) -> None:
        try:
            message = self._codec.decode(body)
        except ProtocolError as exc:
            self._counters.bump(decode_failures=1)
            logger.warning(
                "Undecodable frame body; dropping",
                extra={"link": self._name, "bytes": len(body), "error": str(exc)},
            )
            return

        self._counters.bump(messages_received=1)
        # Seq accounting runs before anything looks at `type`, so an
        # unrecognised message cannot desynchronise the stream.
        await self._account_seq(message)

        if message.type not in self._accepted:
            self._counters.bump(unknown_types=1)
            logger.info(
                "Ignoring unrecognised message type",
                extra={"link": self._name, "type": message.type, "id": message.id},
            )
            await self._report_unknown(message)
            return

        pending = self._pending.pop(message.id, None)
        if pending is not None and not pending.done():
            pending.set_result(message)
            return

        self._enqueue(message)

    def _enqueue(self, message: Message) -> None:
        try:
            self._inbox.put_nowait(message)
        except asyncio.QueueFull:
            # Drop the oldest rather than let a slow consumer apply
            # backpressure into the serial buffer, mirroring the event bus.
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - race guard
                self._inbox.get_nowait()
            self._counters.bump(inbox_drops=1)
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - race guard
                self._inbox.put_nowait(message)

    def _close_inbox(self) -> None:
        """Wake `messages()` with the end-of-stream sentinel. A full inbox must
        not be able to swallow it, or the consumer never learns the link died."""
        with contextlib.suppress(asyncio.QueueEmpty):
            while self._inbox.full():
                self._inbox.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - race guard
            self._inbox.put_nowait(None)

    async def _report_unknown(self, message: Message) -> None:
        # Never answer a `system.error` with a `system.error`: two peers that
        # each dislike the other's error message would ping-pong forever.
        if not self._report_unknown_types or message.type == MessageType.SYSTEM_ERROR:
            return
        with contextlib.suppress(TransportError):
            await self.send_payload(
                SystemError(code="unknown_type", message=f"unsupported type '{message.type}'"),
                id=message.id,
            )

    # -- seq accounting ----------------------------------------------------

    async def _account_seq(self, message: Message) -> None:
        last = self._last_in_seq
        seq = message.seq

        if last is None:
            self._last_in_seq = seq
            return

        if seq > last:
            gap = seq - last - 1
            if gap:
                self._counters.bump(seq_gaps=gap)
            self._last_in_seq = seq
            return

        if seq < last and seq <= self._reboot_seq_threshold:
            # Backwards *into the bottom of the range* is the reboot signal. It
            # is deliberately not conditioned on how far the peer had counted:
            # an MCU that browns out after six frames rebooted just as truly as
            # one that ran for a week.
            await self._handle_reboot(previous=last, observed=seq)
            return

        # Backwards but not to the bottom: a duplicate or a reordering, neither
        # of which a reliable byte stream should produce. Count it and deliver
        # anyway — the frame passed its checksum, and dropping a valid message
        # over a counter oddity is the worse failure. `_last_in_seq` keeps the
        # high-water mark so a genuine reboot is still recognised later.
        self._counters.bump(seq_regressions=1)
        logger.warning(
            "Inbound seq went backwards",
            extra={"link": self._name, "last_seq": last, "seq": seq},
        )

    async def _handle_reboot(self, *, previous: int, observed: int) -> None:
        discarded = tuple(self._pending)
        self._fail_pending("peer rebooted; request cannot be answered")
        self._out_seq = 0
        self._last_in_seq = observed
        self._counters.bump(reboots=1, discarded_requests=len(discarded))
        logger.warning(
            "Peer reboot detected via seq reset",
            extra={
                "link": self._name,
                "previous_seq": previous,
                "seq": observed,
                "discarded_requests": len(discarded),
            },
        )

        event = RebootEvent(
            link=self._name,
            previous_seq=previous,
            observed_seq=observed,
            discarded_request_ids=discarded,
        )
        for handler in list(self._reboot_handlers):
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate handler failures
                logger.error(
                    "Reboot handler failed",
                    extra={"link": self._name, "error": str(exc)},
                )

        if self._hello_on_reboot:
            with contextlib.suppress(TransportError):
                await self.send_hello()

    # -- internals ---------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._out_seq
        self._out_seq = (self._out_seq + 1) % SEQ_MODULUS
        return seq

    async def _write(self, message: Message) -> None:
        frame = self._framing.encode(self._codec.encode(message))
        await self._transport.send(frame)
        self._counters.bump(messages_sent=1, bytes_sent=len(frame))

    def _fail_pending(self, reason: str) -> None:
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(ProtocolError(reason, {"link": self._name, "id": request_id}))
        self._pending.clear()

    def _record_loss(self, loss: FrameLoss) -> None:
        if loss.reason is FrameLossReason.CHECKSUM:
            self._counters.bump(checksum_failures=1)
        elif loss.reason is FrameLossReason.OVERSIZED:
            self._counters.bump(oversized_frames=1)
        else:
            self._counters.bump(junk_bytes=loss.discarded_bytes)
