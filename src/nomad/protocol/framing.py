"""Delimiting a byte stream into frames (D2, D3).

`Framing` is the only object that knows where one frame ends and the next
begins. It never looks inside a frame body — that is the `Codec`'s job — and it
never touches a serial port — that is the `Transport`'s.

## The layout, and why it is not quite the draft in ARCHITECTURE.md

| Bytes | Field | Notes |
|---|---|---|
| 2 | `SYNC` | constant `a7 5e` |
| 4 | `length` (u32 LE) | length of `frame_body` only |
| `length` | `frame_body` | codec-encoded `Message` |
| 4 | `checksum` (CRC32 LE) | over the **`length` field and the body** |

The draft was length-prefix-then-body-then-CRC-over-body, with no preamble. It
does not survive its own stated failure mode. A corrupt frame is one of two
things and the draft cannot tell them apart:

* the **body** was corrupted and `length` is still right — skipping
  `length` bytes lands exactly on the next frame;
* the **`length` prefix itself** was corrupted — skipping `length` bytes lands
  somewhere arbitrary, and since a CRC over the body alone is computed over the
  wrong slice, every subsequent parse is garbage. On a flaky USB cable that is
  a permanently dead link, recoverable only by a reboot.

A constant preamble collapses both cases into one recovery rule: **after a
checksum failure, never trust `length` again — scan forward for the next
`SYNC`.** An accidental `SYNC` inside corrupt data costs one extra CRC check
and the scan continues, so recovery is self-correcting rather than lucky. The
checksum covers the `length` field too, so a corrupt header is detectable at
the header instead of being discovered as mysterious body corruption.

Cost: two bytes per frame. That buys the only property that matters here — a
single flipped bit costs one frame, never the link.

## Bounded memory

`max_frame_bytes` caps both a single frame and, transitively, the parse buffer:
a length prefix larger than the cap is rejected *before* anything is allocated
or waited for. A corrupt `length` of 0xFFFFFFFF must not make a Pi try to
buffer four gigabytes off a rattling cable.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from enum import StrEnum

from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: Frame preamble. Arbitrary, but deliberately not ASCII and not `00`/`ff`, so
#: it is unlikely to appear in idle-line noise or in a run of zero padding.
SYNC: bytes = b"\xa7\x5e"

LENGTH_BYTES = 4
CHECKSUM_BYTES = 4
HEADER_BYTES = len(SYNC) + LENGTH_BYTES
OVERHEAD_BYTES = HEADER_BYTES + CHECKSUM_BYTES

#: Default cap on a single frame body. Generous for a JSON control message and
#: for an RLE display region, small enough that a corrupt length prefix is
#: rejected rather than honoured. Per-link overridable.
DEFAULT_MAX_FRAME_BYTES = 65_536


class FrameLossReason(StrEnum):
    """Why bytes were dropped instead of becoming a frame."""

    #: CRC mismatch. The frame is lost; the parser resynchronises on `SYNC`.
    CHECKSUM = "checksum"
    #: `length` prefix exceeded `max_frame_bytes`. Rejected without allocating.
    OVERSIZED = "oversized"
    #: Bytes preceding a `SYNC` that belonged to no parseable frame.
    JUNK = "junk"


@dataclass(frozen=True)
class FrameLoss:
    """One reported gap in the stream.

    Loss is normal on a serial link and is *not* an error: it is reported,
    counted, and stepped over. Nothing here raises.
    """

    reason: FrameLossReason
    discarded_bytes: int
    detail: str = ""


@dataclass(frozen=True)
class FeedResult:
    """What one chunk of arriving bytes produced."""

    frames: list[bytes] = field(default_factory=list)
    losses: list[FrameLoss] = field(default_factory=list)


def checksum(length_field: bytes, body: bytes) -> int:
    """CRC32 over the `length` field and the body, in wire order."""
    return zlib.crc32(length_field + body)


class Framing:
    """Incremental frame parser and encoder over a byte stream.

    `feed` accepts bytes in whatever sizes they arrive in — one byte, half a
    frame, nine frames and a fragment — and is the only stateful part. It is
    synchronous on purpose: this is pure computation over a `bytes` buffer with
    no I/O to await (D1 asks for async where there is blocking work; there is
    none here).
    """

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    @property
    def max_frame_bytes(self) -> int:
        return self._max_frame_bytes

    @property
    def buffered_bytes(self) -> int:
        """Bytes held pending more input. Bounded by `max_frame_bytes` plus one
        frame's overhead, because an oversized length is rejected on sight."""
        return len(self._buffer)

    def encode(self, body: bytes) -> bytes:
        """Wrap a codec-encoded body in `SYNC | length | body | crc32`."""
        if len(body) > self._max_frame_bytes:
            raise ValueError(
                f"frame body of {len(body)} bytes exceeds max_frame_bytes ({self._max_frame_bytes})"
            )
        length_field = len(body).to_bytes(LENGTH_BYTES, "little")
        crc = checksum(length_field, body)
        return SYNC + length_field + body + crc.to_bytes(CHECKSUM_BYTES, "little")

    def reset(self) -> None:
        """Drop the parse buffer. Called when the peer rebooted — bytes queued
        from before a reboot describe a state that no longer exists."""
        self._buffer.clear()

    def feed(self, chunk: bytes) -> FeedResult:
        """Consume arriving bytes and return whatever completed."""
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        losses: list[FrameLoss] = []

        while True:
            if not self._seek_sync(losses):
                break
            if len(self._buffer) < HEADER_BYTES:
                break

            length_field = bytes(self._buffer[len(SYNC) : HEADER_BYTES])
            length = int.from_bytes(length_field, "little")
            if length > self._max_frame_bytes:
                # Reject before allocating or waiting. This SYNC was noise (or
                # its header is corrupt); step past it and hunt for the next.
                losses.append(
                    FrameLoss(
                        FrameLossReason.OVERSIZED,
                        len(SYNC),
                        f"length prefix {length} exceeds max {self._max_frame_bytes}",
                    )
                )
                logger.warning(
                    "Rejected oversized frame length",
                    extra={"length": length, "max_frame_bytes": self._max_frame_bytes},
                )
                del self._buffer[: len(SYNC)]
                continue

            total = OVERHEAD_BYTES + length
            if len(self._buffer) < total:
                break  # partial frame; wait for more bytes

            body = bytes(self._buffer[HEADER_BYTES : HEADER_BYTES + length])
            expected = int.from_bytes(self._buffer[HEADER_BYTES + length : total], "little")
            if checksum(length_field, body) != expected:
                self._skip_corrupt_frame(total, losses)
                continue

            frames.append(body)
            del self._buffer[:total]

        return FeedResult(frames=frames, losses=losses)

    def _seek_sync(self, losses: list[FrameLoss]) -> bool:
        """Align the buffer to the next `SYNC`. False when none is present yet."""
        index = self._buffer.find(SYNC)
        if index == 0:
            return True
        if index > 0:
            losses.append(FrameLoss(FrameLossReason.JUNK, index, "bytes before SYNC"))
            del self._buffer[:index]
            return True

        # No SYNC anywhere. Keep only enough tail that a preamble split across
        # two chunks is still found next time.
        keep = len(SYNC) - 1
        junk = len(self._buffer) - keep
        if junk > 0:
            losses.append(FrameLoss(FrameLossReason.JUNK, junk, "no SYNC in buffer"))
            del self._buffer[:junk]
        return False

    def _skip_corrupt_frame(self, total: int, losses: list[FrameLoss]) -> None:
        """Step over a frame that failed its checksum.

        If a `SYNC` sits exactly where this frame's `length` says the next frame
        starts, the length was intact and only the body was corrupted: drop the
        whole frame in one go and report one loss. Otherwise the header itself
        is suspect, so drop only the preamble and let `_seek_sync` rescan — the
        conservative move, since honouring a corrupt length is how a stream
        desynchronises for good.
        """
        trustworthy = self._buffer[total : total + len(SYNC)] == SYNC
        discarded = total if trustworthy else len(SYNC)
        losses.append(
            FrameLoss(
                FrameLossReason.CHECKSUM,
                discarded,
                "body corrupt, length intact" if trustworthy else "resynchronising on SYNC",
            )
        )
        logger.warning(
            "Frame failed checksum; skipping",
            extra={"discarded_bytes": discarded, "length_trusted": trustworthy},
        )
        del self._buffer[:discarded]
