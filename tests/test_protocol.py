from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from nomad.core.config import TransportConfig
from nomad.core.errors import ProtocolError, TransportError
from nomad.protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    OVERHEAD_BYTES,
    SYNC,
    DisplayBacklight,
    DisplayDraw,
    DisplayLinkStatus,
    FrameLossReason,
    Framing,
    HidLinkStatus,
    InputButton,
    JsonCodec,
    Link,
    LinkKind,
    LoopbackTransport,
    Message,
    MessageType,
    MockTransport,
    SerialTransport,
    SystemError,
    SystemHello,
    Transport,
    catalogue_for,
    create_transport,
    payload_model_for,
)

CODEC = JsonCodec()


def frame(message: Message, *, framing: Framing | None = None) -> bytes:
    return (framing or Framing()).encode(CODEC.encode(message))


async def collect(link: Link, count: int, *, timeout: float = 2.0) -> list[Message]:
    """Read `count` messages off a link, failing the test rather than hanging."""
    received: list[Message] = []

    async def _pump() -> None:
        async for message in link.messages():
            received.append(message)
            if len(received) >= count:
                return

    await asyncio.wait_for(_pump(), timeout)
    return received


# --- codec ---------------------------------------------------------------


def test_message_round_trips_through_the_json_codec() -> None:
    original = Message.build(InputButton(button="a", phase="press"), seq=7)
    decoded = CODEC.decode(CODEC.encode(original))
    assert decoded == original


def test_binary_payload_survives_a_json_round_trip() -> None:
    """JSON cannot hold raw bytes, so `Binary` fields travel as base64. If this
    ever regresses, `display.draw` silently loses pixels rather than failing."""
    pixels = bytes(range(256))
    original = Message.build(DisplayDraw(x=0, y=0, w=16, h=16, pixels=pixels))
    decoded = CODEC.decode(CODEC.encode(original))
    assert decoded.parse_payload(DisplayDraw).pixels == pixels


def test_decoding_a_non_message_body_raises_protocol_error() -> None:
    with pytest.raises(ProtocolError):
        CODEC.decode(b"{not json")
    with pytest.raises(ProtocolError):
        CODEC.decode(b'{"no_type_field": 1}')


def test_unknown_envelope_fields_are_ignored_not_rejected() -> None:
    """A firmware that adds an envelope field must not kill every frame."""
    decoded = CODEC.decode(b'{"type":"system.error","id":"x","seq":1,"payload":{},"ts":9}')
    assert decoded.type == "system.error"


def test_payload_validation_errors_surface_as_protocol_error() -> None:
    message = Message(type=MessageType.DISPLAY_BACKLIGHT, payload={"level": 999})
    with pytest.raises(ProtocolError):
        message.parse_payload(DisplayBacklight)


# --- catalogue -----------------------------------------------------------


def test_system_status_resolves_to_a_different_model_per_link() -> None:
    """The draft catalogue gives `system.status` two shapes. Keying on the link
    is what keeps that from being a type collision."""
    assert payload_model_for(LinkKind.DISPLAY, "system.status") is DisplayLinkStatus
    assert payload_model_for(LinkKind.HID, "system.status") is HidLinkStatus


def test_hid_link_carries_no_input_types() -> None:
    """The RP2040 has no sensors; `input.*` on that link is not a message."""
    assert not [t for t in catalogue_for(LinkKind.HID) if t.startswith("input.")]


def test_audio_types_are_absent_from_both_links() -> None:
    """Audio hangs off the Pi, not this transport. An earlier draft had it here."""
    for link in LinkKind:
        assert not [t for t in catalogue_for(link) if t.startswith("audio.")]


# --- framing -------------------------------------------------------------


def test_framing_round_trips_a_body() -> None:
    framing = Framing()
    result = framing.feed(framing.encode(b"hello"))
    assert result.frames == [b"hello"]
    assert result.losses == []


def test_stream_delivered_one_byte_at_a_time_still_yields_the_frame() -> None:
    framing = Framing()
    wire = framing.encode(b'{"type":"system.hello"}')
    frames: list[bytes] = []
    for index in range(len(wire)):
        frames.extend(framing.feed(wire[index : index + 1]).frames)
    assert frames == [b'{"type":"system.hello"}']


def test_several_frames_in_one_chunk_are_all_parsed() -> None:
    framing = Framing()
    bodies = [b"one", b"two", b"three", b""]
    chunk = b"".join(framing.encode(body) for body in bodies)
    assert framing.feed(chunk).frames == bodies


def test_partial_trailing_frame_is_buffered_until_completed() -> None:
    framing = Framing()
    wire = framing.encode(b"first") + framing.encode(b"second")
    split = len(wire) - 3
    assert framing.feed(wire[:split]).frames == [b"first"]
    assert framing.feed(wire[split:]).frames == [b"second"]


def test_corrupt_body_is_reported_and_skipped_without_desyncing() -> None:
    framing = Framing()
    wire = bytearray(framing.encode(b"corrupt-me") + framing.encode(b"intact"))
    wire[OVERHEAD_BYTES + 2] ^= 0xFF  # flip a bit inside the first body

    result = framing.feed(bytes(wire))

    assert result.frames == [b"intact"]
    assert [loss.reason for loss in result.losses] == [FrameLossReason.CHECKSUM]


def test_corrupt_length_prefix_resynchronises_on_the_next_frame() -> None:
    """The failure the draft layout could not survive: with the CRC over the
    body alone and no preamble, a damaged length prefix desynchronises the
    parser permanently. Recovery must not depend on the length being intact."""
    framing = Framing()
    good = framing.encode(b"survivor")
    wire = bytearray(framing.encode(b"a-reasonably-long-body") + good)
    wire[len(SYNC)] = 3  # shrink the first frame's declared length

    result = framing.feed(bytes(wire))

    assert result.frames == [b"survivor"]
    assert FrameLossReason.CHECKSUM in {loss.reason for loss in result.losses}


def test_oversized_length_prefix_is_rejected_without_buffering() -> None:
    """A corrupt length must never make a Pi try to hold hundreds of megabytes
    off a rattling USB cable."""
    framing = Framing(max_frame_bytes=1024)
    hostile = SYNC + (0xFFFFFFFF).to_bytes(4, "little") + b"\x00" * 16

    result = framing.feed(hostile)

    assert result.frames == []
    assert result.losses[0].reason is FrameLossReason.OVERSIZED
    assert framing.buffered_bytes < 64

    assert framing.feed(framing.encode(b"after")).frames == [b"after"]


def test_encoding_a_body_over_the_cap_is_refused() -> None:
    framing = Framing(max_frame_bytes=16)
    with pytest.raises(ValueError, match="exceeds max_frame_bytes"):
        framing.encode(b"x" * 17)


def test_leading_junk_is_reported_and_stepped_over() -> None:
    framing = Framing()
    result = framing.feed(b"\x00\x01\x02noise" + framing.encode(b"payload"))
    assert result.frames == [b"payload"]
    assert result.losses[0].reason is FrameLossReason.JUNK
    assert result.losses[0].discarded_bytes == 8


def test_a_preamble_split_across_chunks_is_still_found() -> None:
    framing = Framing()
    wire = framing.encode(b"split")
    assert framing.feed(wire[:1]).frames == []
    assert framing.feed(wire[1:]).frames == [b"split"]


def test_a_body_containing_the_preamble_parses_normally() -> None:
    """A valid frame is consumed whole, so `SYNC` inside a body is never scanned."""
    framing = Framing()
    body = b"before" + SYNC + b"after"
    assert framing.feed(framing.encode(body)).frames == [body]


def test_defaults_cap_a_frame_well_under_a_megabyte() -> None:
    assert DEFAULT_MAX_FRAME_BYTES <= 1_000_000


# --- transports ----------------------------------------------------------


async def test_loopback_transport_returns_what_was_written() -> None:
    transport = LoopbackTransport()
    await transport.start()
    await transport.send(b"echo")
    assert await transport.receive() == b"echo"


async def test_mock_transport_preserves_scripted_chunk_boundaries() -> None:
    transport = MockTransport(script=[b"ab", b"c"])
    await transport.start()
    assert await transport.receive() == b"ab"
    assert await transport.receive() == b"c"


async def test_closed_transport_reads_empty_and_refuses_writes() -> None:
    transport = MockTransport()
    await transport.start()
    await transport.stop()
    assert await transport.receive() == b""
    with pytest.raises(TransportError):
        await transport.send(b"x")


# --- serial transport -----------------------------------------------------
#
# All of this runs with no serial port and no `pyserial-asyncio` installed
# (D9). The happy path injects a fake module so the transport's own code runs
# for real — a test that only asserted the import error would leave send,
# receive and EOF handling entirely uncovered.


class _FakeSerialReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""  # EOF, as a real StreamReader reports it
        return self._chunks.pop(0)


class _FakeSerialWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _install_fake_serial(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]
) -> tuple[_FakeSerialWriter, dict[str, object]]:
    """Put a stand-in `serial_asyncio` on `sys.modules` and report the args."""
    import sys
    import types

    writer = _FakeSerialWriter()
    seen: dict[str, object] = {}

    async def open_serial_connection(**kwargs: object) -> tuple[object, object]:
        seen.update(kwargs)
        return _FakeSerialReader(chunks), writer

    module = types.ModuleType("serial_asyncio")
    module.open_serial_connection = open_serial_connection  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial_asyncio", module)
    return writer, seen


def test_serial_transport_refuses_an_empty_port() -> None:
    with pytest.raises(TransportError):
        SerialTransport("")


def test_serial_transport_names_itself_after_its_port() -> None:
    assert SerialTransport("/dev/ttyACM0").name == "serial:/dev/ttyACM0"
    assert SerialTransport("/dev/ttyACM0", name="esp32").name == "esp32"


async def test_serial_transport_is_closed_until_started() -> None:
    transport = SerialTransport("/dev/ttyACM0")
    assert transport.closed
    # Closed reads are end-of-stream, not an error — a reader loop must stop.
    assert await transport.receive() == b""
    with pytest.raises(TransportError):
        await transport.send(b"x")


async def test_serial_transport_without_the_extra_raises_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing optional dependency is a transport failure, not an ImportError.

    Callers above `Transport` cannot act on the difference, and D2 says they
    should not have to know it.
    """
    import sys

    monkeypatch.setitem(sys.modules, "serial_asyncio", None)
    transport = SerialTransport("/dev/ttyACM0")
    with pytest.raises(TransportError):
        await transport.start()


async def test_serial_transport_round_trips_over_a_fake_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, seen = _install_fake_serial(monkeypatch, [b"he", b"llo"])
    transport = SerialTransport("/dev/ttyACM0", baudrate=921600)

    await transport.start()
    assert seen == {"url": "/dev/ttyACM0", "baudrate": 921600}
    assert not transport.closed

    await transport.send(b"ping")
    assert writer.written == [b"ping"]

    # Chunk boundaries arrive as the port gives them — the framer's problem.
    assert await transport.receive() == b"he"
    assert await transport.receive() == b"llo"

    await transport.stop()
    assert transport.closed
    assert writer.closed


async def test_serial_transport_latches_closed_on_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unplugged peer must end the stream, not spin the reader loop."""
    _install_fake_serial(monkeypatch, [b"data"])
    transport = SerialTransport("/dev/ttyACM0")
    await transport.start()

    assert await transport.receive() == b"data"
    assert await transport.receive() == b""
    assert transport.closed
    # And it keeps saying b"" rather than reopening.
    assert await transport.receive() == b""


async def test_serial_transport_send_is_a_noop_for_empty_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, _ = _install_fake_serial(monkeypatch, [])
    transport = SerialTransport("/dev/ttyACM0")
    await transport.start()
    await transport.send(b"")
    assert writer.written == []


async def test_a_link_cannot_tell_serial_from_a_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2's actual claim: nothing above `Transport` special-cases the real one."""
    _install_fake_serial(monkeypatch, [])
    transport = SerialTransport("/dev/ttyACM0")
    assert isinstance(transport, Transport)


# --- transport selection --------------------------------------------------


def test_create_transport_defaults_to_a_mock() -> None:
    assert isinstance(create_transport(TransportConfig()), MockTransport)


def test_create_transport_builds_a_serial_transport_without_importing_the_extra() -> None:
    """Selecting `serial` must not require `pyserial-asyncio` to be installed —
    the import is deferred to `start()`, so construction alone stays cheap."""
    config = TransportConfig(kind="serial", port="/dev/ttyACM0", baudrate=921600)
    transport = create_transport(config, name="esp32")
    assert isinstance(transport, SerialTransport)
    assert transport.port == "/dev/ttyACM0"
    assert transport.baudrate == 921600
    assert transport.name == "esp32"


def test_create_transport_rejects_an_unknown_kind() -> None:
    """A typo must not silently become a mock — 'connected but silent' is the
    most expensive failure to diagnose on this device."""
    config = TransportConfig.model_construct(kind="srial", port="/dev/ttyACM0", baudrate=115200)
    with pytest.raises(TransportError):
        create_transport(config)


def test_transport_config_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TransportConfig(kind="uart")


def test_transport_config_requires_a_port_for_serial() -> None:
    with pytest.raises(ValidationError):
        TransportConfig(kind="serial", port="")


# --- link ----------------------------------------------------------------


async def test_link_round_trips_a_message_over_a_loopback() -> None:
    link = Link(LoopbackTransport(), kind=LinkKind.DISPLAY)
    await link.start()
    try:
        await link.send_payload(DisplayBacklight(level=128))
        received = await collect(link, 1)
    finally:
        await link.stop()

    assert received[0].type == MessageType.DISPLAY_BACKLIGHT
    assert received[0].parse_payload(DisplayBacklight).level == 128


async def test_link_stamps_a_monotonic_outgoing_seq() -> None:
    link = Link(LoopbackTransport(), kind=LinkKind.DISPLAY)
    await link.start()
    try:
        first = await link.send_payload(DisplayBacklight(level=1))
        second = await link.send_payload(DisplayBacklight(level=2))
    finally:
        await link.stop()

    assert (first.seq, second.seq) == (0, 1)


async def test_unknown_message_type_is_ignored_and_does_not_stop_the_link() -> None:
    """Extension rule 1: a type from newer firmware must cost one message, not
    the link. It is also not allowed to desynchronise `seq`."""
    transport = MockTransport()
    framing = Framing()
    transport.script(
        frame(Message(type="display.draw_v2", seq=0, payload={"anything": True}), framing=framing),
        frame(Message.build(DisplayBacklight(level=9), seq=1), framing=framing),
    )
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        received = await collect(link, 1)
    finally:
        await link.stop()

    assert received[0].type == MessageType.DISPLAY_BACKLIGHT
    assert link.stats.unknown_types == 1
    assert link.stats.seq_gaps == 0
    assert link.last_received_seq == 1


async def test_unknown_message_type_is_reported_back_as_system_error() -> None:
    transport = MockTransport(script=[frame(Message(type="display.draw_v2", seq=0))])
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        await asyncio.wait_for(_until(lambda: bool(transport.sent)), timeout=2.0)
    finally:
        await link.stop()

    reported = CODEC.decode(Framing().feed(transport.sent_bytes).frames[0])
    assert reported.parse_payload(SystemError).code == "unknown_type"


async def test_an_unknown_system_error_is_never_answered_with_another() -> None:
    """Two peers that each dislike the other's error would ping-pong forever."""
    transport = MockTransport(script=[frame(Message(type=MessageType.SYSTEM_ERROR, seq=0))])
    link = Link(transport, kind=LinkKind.DISPLAY, accepted_types=frozenset())
    await link.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        await link.stop()

    assert transport.sent == []


async def test_undecodable_frame_body_costs_one_message_not_the_link() -> None:
    transport = MockTransport()
    framing = Framing()
    transport.script(
        framing.encode(b"not-a-message"),
        frame(Message.build(DisplayBacklight(level=3), seq=0), framing=framing),
    )
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        received = await collect(link, 1)
    finally:
        await link.stop()

    assert received[0].parse_payload(DisplayBacklight).level == 3
    assert link.stats.decode_failures == 1


async def test_seq_reset_is_detected_and_discards_pending_requests() -> None:
    """The load-bearing behaviour (D3). A request outstanding when the MCU
    reboots can never be answered, so it must fail rather than wait for a
    response that would, at best, belong to a different epoch."""
    transport = MockTransport()
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    reboots: list[int] = []
    link.on_reboot(lambda event: _record(reboots, event.previous_seq))

    pending = asyncio.ensure_future(link.request(SystemHello(), timeout=5.0))
    await asyncio.wait_for(_until(lambda: bool(link.pending_ids)), timeout=2.0)

    framing = Framing()
    transport.script(
        frame(
            Message.build(
                DisplayLinkStatus(uptime_ms=90_000, free_heap=1, last_seq_seen=40), seq=41
            ),
            framing=framing,
        ),
        frame(Message.build(SystemHello(firmware_version="1.2"), seq=0), framing=framing),
    )

    with pytest.raises(ProtocolError, match="rebooted"):
        await asyncio.wait_for(pending, timeout=2.0)

    assert link.pending_ids == frozenset()
    assert link.stats.reboots == 1
    assert link.stats.discarded_requests == 1
    assert reboots == [41]
    await link.stop()


async def test_reboot_resets_the_outgoing_seq_and_re_greets_the_peer() -> None:
    """State is re-established via `system.hello`, never assumed to continue."""
    transport = MockTransport()
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        await link.send_payload(DisplayBacklight(level=1))
        await link.send_payload(DisplayBacklight(level=2))
        framing = Framing()
        transport.script(
            frame(Message.build(SystemHello(), seq=30), framing=framing),
            frame(Message.build(SystemHello(), seq=0), framing=framing),
        )
        await asyncio.wait_for(_until(lambda: link.stats.reboots == 1), timeout=2.0)
        await asyncio.wait_for(_until(lambda: len(transport.sent) == 3), timeout=2.0)
    finally:
        await link.stop()

    hello = CODEC.decode(Framing().feed(transport.sent[2]).frames[0])
    assert hello.type == MessageType.SYSTEM_HELLO
    assert hello.seq == 0


async def test_a_reboot_within_the_first_few_frames_is_still_detected() -> None:
    """An MCU that browns out after four frames rebooted just as truly as one
    that ran for a week; detection must not require a high previous seq."""
    transport = MockTransport()
    framing = Framing()
    transport.script(
        frame(Message.build(SystemHello(), seq=4), framing=framing),
        frame(Message.build(SystemHello(), seq=0), framing=framing),
    )
    link = Link(transport, kind=LinkKind.DISPLAY, hello_on_reboot=False)
    await link.start()
    try:
        await asyncio.wait_for(_until(lambda: link.stats.reboots == 1), timeout=2.0)
    finally:
        await link.stop()


async def test_a_forward_seq_gap_is_counted_as_loss_not_a_reboot() -> None:
    transport = MockTransport()
    framing = Framing()
    transport.script(
        frame(Message.build(SystemHello(), seq=10), framing=framing),
        frame(Message.build(SystemHello(), seq=14), framing=framing),
    )
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        await collect(link, 2)
    finally:
        await link.stop()

    assert link.stats.seq_gaps == 3
    assert link.stats.reboots == 0


async def test_a_duplicate_seq_is_counted_but_the_message_is_still_delivered() -> None:
    """A frame that passed its checksum is real. Dropping it over a counter
    oddity is the worse failure, so it is delivered and the oddity recorded."""
    transport = MockTransport()
    framing = Framing()
    transport.script(
        frame(Message.build(DisplayBacklight(level=1), seq=20), framing=framing),
        frame(Message.build(DisplayBacklight(level=2), seq=20), framing=framing),
    )
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        received = await collect(link, 2)
    finally:
        await link.stop()

    assert len(received) == 2
    assert link.stats.seq_regressions == 1
    assert link.stats.reboots == 0


async def test_a_response_matching_a_pending_id_resolves_the_request() -> None:
    transport = MockTransport()
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        pending = asyncio.ensure_future(link.request(SystemHello(), timeout=5.0))
        await asyncio.wait_for(_until(lambda: bool(link.pending_ids)), timeout=2.0)
        request_id = next(iter(link.pending_ids))
        transport.script(
            frame(Message.build(SystemHello(firmware_version="2.0"), id=request_id, seq=0))
        )
        response = await asyncio.wait_for(pending, timeout=2.0)
    finally:
        await link.stop()

    assert response.parse_payload(SystemHello).firmware_version == "2.0"


async def test_a_request_with_no_answer_times_out_rather_than_hanging() -> None:
    link = Link(MockTransport(), kind=LinkKind.DISPLAY)
    await link.start()
    try:
        with pytest.raises(ProtocolError, match="timed out"):
            await link.request(SystemHello(), timeout=0.05)
        assert link.pending_ids == frozenset()
    finally:
        await link.stop()


async def test_corruption_on_the_wire_is_counted_and_the_next_message_arrives() -> None:
    """End to end: the framer's resynchronisation is visible as a live link
    that loses one message and keeps working."""
    transport = MockTransport()
    framing = Framing()
    wire = bytearray(
        frame(Message.build(DisplayBacklight(level=1), seq=0), framing=framing)
        + frame(Message.build(DisplayBacklight(level=2), seq=1), framing=framing)
    )
    wire[OVERHEAD_BYTES + 5] ^= 0xFF
    transport.script(bytes(wire))

    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    try:
        received = await collect(link, 1)
    finally:
        await link.stop()

    assert received[0].parse_payload(DisplayBacklight).level == 2
    assert link.stats.checksum_failures == 1


async def test_messages_iterator_ends_when_the_transport_closes() -> None:
    transport = MockTransport()
    link = Link(transport, kind=LinkKind.DISPLAY)
    await link.start()
    transport.close()

    async def _drain() -> list[Message]:
        return [message async for message in link.messages()]

    assert await asyncio.wait_for(_drain(), timeout=2.0) == []
    await link.stop()


# --- helpers -------------------------------------------------------------


async def _until(predicate, *, interval: float = 0.01) -> None:
    while not predicate():
        await asyncio.sleep(interval)


async def _record(sink: list[int], value: int) -> None:
    sink.append(value)
