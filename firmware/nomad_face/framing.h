// D30's framing, in C, byte-for-byte what `src/nomad/protocol/framing.py` does.
//
//   SYNC (0xA7 0x5E) | length (u32 LE) | body | crc32 (u32 LE)
//
// The CRC covers the **length field and the body**, in wire order — not the
// body alone. That is deliberate on the Python side and the reason a corrupt
// length prefix cannot be honoured, so getting it wrong here would produce a
// link that works until the first bit error and then desynchronises for good.
//
// Recovery rule, also from D30: after a bad frame, scan forward for the next
// SYNC. An accidental SYNC inside corrupt data costs one wasted CRC check, not
// a lost stream.
//
// ARCHITECTURE.md: "Envelope or framing changes require both sides in
// lockstep." If you edit this file, edit `framing.py` in the same commit.

#pragma once

#include <stddef.h>
#include <stdint.h>

namespace nomad {

static const uint8_t kSync0 = 0xA7;
static const uint8_t kSync1 = 0x5E;
static const size_t kLengthBytes = 4;
static const size_t kChecksumBytes = 4;
static const size_t kHeaderBytes = 2 + kLengthBytes;
static const size_t kOverheadBytes = kHeaderBytes + kChecksumBytes;

// The Python side's cap is 65536. Matching it exactly matters less than being
// no *larger*: a length this side would accept and that side rejects is a frame
// that vanishes with no error either end can explain. Ours is smaller because
// the ESP32's heap is the tighter constraint and a structural `display.state`
// is a few hundred bytes.
static const size_t kMaxFrameBytes = 8192;

// CRC-32 as zlib/`zlib.crc32` computes it: reflected, polynomial 0xEDB88320,
// initial value all-ones, final complement. Computed bytewise rather than from
// a 1 KiB table — a `display.state` frame is small and this runs far faster
// than the 921600-baud link it feeds.
inline uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t length) {
  crc = ~crc;
  for (size_t i = 0; i < length; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) {
      crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1)));
    }
  }
  return ~crc;
}

inline uint32_t crc32(const uint8_t *data, size_t length) {
  return crc32Update(0, data, length);
}

// Incremental parser. Fixed buffer, no allocation: this runs on the display
// task and a heap fragmented by frame-sized churn is how a long-lived embedded
// link dies after a day.
class Framing {
 public:
  // Reasons a frame was lost. Reported, counted, stepped over — never fatal.
  // Loss is normal on a cable that can be pulled.
  enum class Loss : uint8_t { kNone, kChecksum, kOversized, kJunk };

  void reset() { used_ = 0; }

  size_t buffered() const { return used_; }
  uint32_t checksumLosses() const { return checksum_losses_; }
  uint32_t oversizedLosses() const { return oversized_losses_; }
  uint32_t junkBytes() const { return junk_bytes_; }

  // Appends one byte. Returns false if the buffer is full, which can only
  // happen if a length prefix passed the size check and its body never
  // arrived; the caller resets in that case.
  bool push(uint8_t byte) {
    if (used_ >= sizeof(buffer_)) {
      return false;
    }
    buffer_[used_++] = byte;
    return true;
  }

  // Extracts the next complete frame body into `out`. Returns the body length,
  // or 0 if no frame is ready yet, setting `loss` when bytes were discarded.
  size_t next(uint8_t *out, size_t capacity, Loss *loss) {
    *loss = Loss::kNone;

    for (;;) {
      const size_t start = seekSync();
      if (start > 0) {
        junk_bytes_ += start;
        *loss = Loss::kJunk;
        consume(start);
      }
      if (used_ < kHeaderBytes) {
        return 0;  // not even a header yet
      }

      const uint32_t length = (uint32_t)buffer_[2] | ((uint32_t)buffer_[3] << 8) |
                              ((uint32_t)buffer_[4] << 16) | ((uint32_t)buffer_[5] << 24);

      if (length > kMaxFrameBytes || length > capacity) {
        // Rejected without waiting for a body that may never come. Drop only
        // the preamble, so a real frame that began inside this garbage is still
        // found by the rescan.
        oversized_losses_++;
        *loss = Loss::kOversized;
        consume(2);
        continue;
      }

      const size_t total = kHeaderBytes + length + kChecksumBytes;
      if (used_ < total) {
        return 0;  // body still arriving
      }

      const uint32_t expected = crc32(&buffer_[2], kLengthBytes + length);
      const uint8_t *tail = &buffer_[kHeaderBytes + length];
      const uint32_t actual = (uint32_t)tail[0] | ((uint32_t)tail[1] << 8) |
                              ((uint32_t)tail[2] << 16) | ((uint32_t)tail[3] << 24);

      if (expected != actual) {
        checksum_losses_++;
        *loss = Loss::kChecksum;
        consume(2);
        continue;
      }

      for (size_t i = 0; i < length; i++) {
        out[i] = buffer_[kHeaderBytes + i];
      }
      consume(total);
      return length;
    }
  }

 private:
  // Offset of the first SYNC, or `used_` if there is none. A trailing lone
  // 0xA7 is kept: it may be a preamble split across two USB reads.
  size_t seekSync() const {
    for (size_t i = 0; i + 1 < used_; i++) {
      if (buffer_[i] == kSync0 && buffer_[i + 1] == kSync1) {
        return i;
      }
    }
    return (used_ > 0 && buffer_[used_ - 1] == kSync0) ? used_ - 1 : used_;
  }

  void consume(size_t count) {
    if (count >= used_) {
      used_ = 0;
      return;
    }
    for (size_t i = 0; i + count < used_; i++) {
      buffer_[i] = buffer_[i + count];
    }
    used_ -= count;
  }

  uint8_t buffer_[kMaxFrameBytes + kOverheadBytes];
  size_t used_ = 0;
  uint32_t checksum_losses_ = 0;
  uint32_t oversized_losses_ = 0;
  uint32_t junk_bytes_ = 0;
};

// Writes `SYNC | length | body | crc32` to a Stream. Returns false if the body
// is too large to frame.
template <typename StreamT>
inline bool writeFrame(StreamT &stream, const uint8_t *body, size_t length) {
  if (length > kMaxFrameBytes) {
    return false;
  }
  uint8_t header[kHeaderBytes];
  header[0] = kSync0;
  header[1] = kSync1;
  header[2] = (uint8_t)(length & 0xFF);
  header[3] = (uint8_t)((length >> 8) & 0xFF);
  header[4] = (uint8_t)((length >> 16) & 0xFF);
  header[5] = (uint8_t)((length >> 24) & 0xFF);

  // Same coverage as the parser: length field first, then body.
  uint32_t crc = crc32Update(0, &header[2], kLengthBytes);
  crc = crc32Update(crc, body, length);

  const uint8_t trailer[kChecksumBytes] = {
      (uint8_t)(crc & 0xFF), (uint8_t)((crc >> 8) & 0xFF),
      (uint8_t)((crc >> 16) & 0xFF), (uint8_t)((crc >> 24) & 0xFF)};

  stream.write(header, sizeof(header));
  stream.write(body, length);
  stream.write(trailer, sizeof(trailer));
  return true;
}

}  // namespace nomad
