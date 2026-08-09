"""The firmware's framing and Python's must agree byte for byte.

ARCHITECTURE.md: "Envelope or framing changes require both sides in lockstep.
There is no negotiation." That is a rule with no enforcement — `framing.py` and
`firmware/nomad_face/framing.h` are two independent implementations of
`SYNC | length | body | crc32`, and the CRC covers the *length field* as well as
the body, which is exactly the sort of detail a reimplementation gets wrong.

The failure mode is nasty and worth the cost of these tests: a mismatch does not
break the link at boot. Frames flow, the screen draws, and then the first bit
error desynchronises the stream permanently because the two sides disagree about
where a frame ends. Better to find it here, in a second, on a laptop.

`framing.h` is deliberately free of Arduino headers so it compiles natively;
that is what makes this possible. These tests skip where no C++ compiler exists
rather than failing, because a working compiler is not a requirement for running
Nomad (D9) — but on any machine that has one, the two implementations are
checked against each other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nomad.protocol.framing import Framing

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "nomad_face"

# A `Stream` stand-in, so `writeFrame`'s template instantiates without Arduino.
HARNESS = """
#include "framing.h"
#include <cstdio>
#include <cstring>
#include <vector>

struct FakeStream {
  std::vector<uint8_t> out;
  void write(const uint8_t *data, size_t n) { out.insert(out.end(), data, data + n); }
};

int main(int argc, char **argv) {
  std::vector<uint8_t> body;
  int c;
  while ((c = getchar()) != EOF) body.push_back((uint8_t)c);

  if (std::strcmp(argv[1], "encode") == 0) {
    FakeStream s;
    if (!nomad::writeFrame(s, body.data(), body.size())) { printf("REFUSED\\n"); return 0; }
    for (uint8_t b : s.out) printf("%02x", b);
    printf("\\n");
  } else if (std::strcmp(argv[1], "parse") == 0) {
    static nomad::Framing f;
    static uint8_t out[nomad::kMaxFrameBytes];
    for (uint8_t b : body) {
      if (!f.push(b)) { printf("OVERFLOW\\n"); return 0; }
    }
    for (;;) {
      nomad::Framing::Loss loss;
      size_t n = f.next(out, sizeof(out), &loss);
      if (n == 0) break;
      for (size_t i = 0; i < n; i++) printf("%02x", out[i]);
      printf("\\n");
    }
  }
  return 0;
}
"""


def _compiler() -> str | None:
    for name in ("g++", "clang++", "c++"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = _compiler()
    if compiler is None:
        pytest.skip("no C++ compiler; the firmware framing cross-check needs one")
    if not (FIRMWARE / "framing.h").exists():
        pytest.skip("firmware/nomad_face/framing.h is not present")

    build = tmp_path_factory.mktemp("firmware-framing")
    source = build / "harness.cpp"
    source.write_text(HARNESS)
    binary = build / "harness"

    result = subprocess.run(
        [compiler, "-O1", "-std=c++17", f"-I{FIRMWARE}", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
        # `nice` is not portable through subprocess, but -O1 on one small
        # translation unit is cheap enough not to need it.
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"firmware framing.h does not compile:\n{result.stderr}")
    return binary


def _run(harness: Path, mode: str, data: bytes) -> list[str]:
    result = subprocess.run(
        [str(harness), mode], input=data, capture_output=True, check=True
    )
    return result.stdout.decode().split()


BODIES = [
    pytest.param(b"", id="empty"),
    pytest.param(b"{}", id="smallest-json"),
    pytest.param(
        b'{"type":"display.state","id":"a","seq":7,"payload":{"kind":"card"}}',
        id="realistic-envelope",
    ),
    pytest.param(bytes(range(256)), id="every-byte-value"),
    # A body made entirely of preambles: if either side's resynchronisation
    # looked inside a body it would find dozens of false frame starts.
    pytest.param(b"\xa7\x5e" * 40, id="body-of-syncs"),
    pytest.param(bytes(range(256)) * 11 + b"tail", id="multi-kilobyte"),
]


@pytest.mark.parametrize("body", BODIES)
def test_the_firmware_encodes_exactly_what_python_encodes(harness: Path, body: bytes) -> None:
    expected = Framing().encode(body).hex()
    assert _run(harness, "encode", body) == [expected]


@pytest.mark.parametrize("body", BODIES)
def test_the_firmware_parses_what_python_encoded(harness: Path, body: bytes) -> None:
    frame = Framing().encode(body)
    assert _run(harness, "parse", frame) == ([body.hex()] if body else [])


def test_python_parses_what_the_firmware_encoded(harness: Path) -> None:
    """The other direction, which is the one carrying `input.touch`."""
    body = b'{"type":"input.touch","payload":{"x":10,"y":20,"phase":"down"}}'
    wire = bytes.fromhex(_run(harness, "encode", body)[0])
    assert Framing().feed(wire).frames == [body]


def test_both_sides_recover_the_same_frames_from_a_damaged_stream(harness: Path) -> None:
    """The point of the preamble: junk ahead of a frame and one corrupt frame in
    the middle must cost exactly the frames they damaged, and no more."""
    encoder = Framing()
    stream = (
        b"garbage"
        + encoder.encode(b'{"a":1}')
        + encoder.encode(b"second")
        # A well-formed header whose CRC is wrong. Both sides must drop this one
        # frame and resynchronise, not lose the rest of the stream.
        + b"\xa7\x5e\x05\x00\x00\x00junkkBAD!"
        + encoder.encode(b"third")
    )

    from_c = _run(harness, "parse", stream)
    from_python = [frame.hex() for frame in Framing().feed(stream).frames]

    assert from_python == [b'{"a":1}'.hex(), b"second".hex(), b"third".hex()]
    assert from_c == from_python


def test_the_firmware_refuses_a_body_larger_than_it_can_frame(harness: Path) -> None:
    """The firmware's cap (8 KiB) is deliberately *below* Python's (64 KiB), so
    the Pi can send something this build rejects. It must refuse rather than
    emit a truncated frame — a truncated frame desynchronises the link, and a
    refusal is a missing screen update."""
    assert _run(harness, "encode", b"x" * 9000) == ["REFUSED"]


def test_a_body_at_pythons_limit_is_rejected_not_truncated(harness: Path) -> None:
    """And the parser side of the same asymmetry: an oversized length prefix is
    rejected on sight without allocating, so nothing is returned."""
    oversized = b"\xa7\x5e" + (20000).to_bytes(4, "little") + b"\x00" * 32
    assert _run(harness, "parse", oversized) == []


@pytest.mark.skipif(os.name != "posix", reason="urandom-driven fuzz; posix only")
def test_the_two_implementations_agree_on_random_bodies(harness: Path) -> None:
    """Fixed seed, so a failure is reproducible rather than a rumour."""
    import random

    rng = random.Random(20260808)
    for _ in range(12):
        body = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 900)))
        assert _run(harness, "encode", body) == [Framing().encode(body).hex()]
