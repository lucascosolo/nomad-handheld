// Nomad's face: chunk W.
//
// This is the firmware the ESP32-S3 module runs in place of its factory LVGL
// demo. It does three things and deliberately not a fourth:
//
//   1. Speaks D30's framing over native USB CDC, with D3's JSON envelope.
//   2. Renders `display.state` — text, card, list, choice — with its own fonts
//      and layout, because E2 sends *structure* rather than pixels.
//   3. Reports touch as `input.touch`.
//
// It holds no application state and makes no decisions. If the Pi says nothing,
// the screen says the link is idle; it does not invent a status screen of its
// own. ARCHITECTURE.md: the ESP32-S3 is a display and input peripheral, not a
// co-processor.
//
// **Why LVGL is absent.** The factory demo's LVGL is a general widget toolkit
// with its own event loop, and E2's vocabulary is four screen shapes. Direct
// LovyanGFX drawing is a few hundred lines against LVGL's buffer management,
// tick task and heap appetite, on a part whose RAM is also holding the frame
// buffer. If a later chunk needs real widgets, LVGL goes *behind* the same
// four handlers.
//
// **CDC on boot is not optional.** Arduino's `Serial` is UART0 on IO43/44
// unless `CDCOnBoot=cdc` is set at build time, in which case it is the native
// USB peripheral. Build it wrong and the board enumerates, looks healthy, and
// says nothing — which is exactly how this module arrived.

#include <ArduinoJson.h>

#include "framing.h"
#include "panel.h"

static const char *kFirmwareVersion = "nomad-face 0.1.0";

static nomad::NomadDisplay display;
static nomad::Framing framing;

// One document reused for decode and one for encode. Sized for the largest
// realistic `display.state` — a list of a dozen items with details. Static
// rather than per-frame so a long uptime cannot fragment the heap.
static StaticJsonDocument<6144> incoming;
static StaticJsonDocument<1024> outgoing;

static uint8_t frameBody[nomad::kMaxFrameBytes];
static uint8_t sendBuffer[1024];

// Outgoing sequence. D30: this resets on reboot, and `system.hello` goes first,
// so the Pi sees a fresh low sequence after a restart and re-establishes state
// rather than assuming continuity.
static uint32_t outSeq = 0;
static uint32_t lastSeqSeen = 0;
static uint32_t framesRendered = 0;

// Landscape. 320x240 logical, matching `[display] width/height` in nomad.toml.
static const int kRotation = 1;

// A deliberately plain palette: this is a status surface read at arm's length
// in whatever light the operator is standing in, not a UI.
static const uint16_t kBackground = 0x0000;
static const uint16_t kForeground = 0xFFFF;
static const uint16_t kMuted = 0x8410;
static const uint16_t kAccent = 0x07FF;
static const uint16_t kRule = 0x4208;

// ---------------------------------------------------------------- outgoing

static void sendMessage(const char *type) {
  outgoing["type"] = type;
  // `id` is required by the envelope. A counter is enough: the Pi correlates
  // responses by it, and D30 carries `seq` separately precisely so that an id
  // reused across a reboot cannot be mistaken for continuity.
  char id[24];
  snprintf(id, sizeof(id), "esp32-%lu", (unsigned long)outSeq);
  outgoing["id"] = id;
  outgoing["seq"] = outSeq++;

  const size_t length = serializeJson(outgoing, sendBuffer, sizeof(sendBuffer));
  if (length > 0 && length < sizeof(sendBuffer)) {
    nomad::writeFrame(Serial, sendBuffer, length);
  }
  outgoing.clear();
}

static void sendHello() {
  JsonObject payload = outgoing.createNestedObject("payload");
  payload["firmware_version"] = kFirmwareVersion;
  JsonArray capabilities = payload.createNestedArray("capabilities");
  // Exactly what is implemented below. Claiming `display.draw` here would make
  // the Pi send framebuffers this build silently drops.
  capabilities.add("display.state");
  capabilities.add("display.backlight");
  capabilities.add("input.touch");
  sendMessage("system.hello");
}

static void sendStatus() {
  JsonObject payload = outgoing.createNestedObject("payload");
  payload["uptime_ms"] = (uint32_t)millis();
  payload["free_heap"] = (uint32_t)ESP.getFreeHeap();
  payload["last_seq_seen"] = lastSeqSeen;
  sendMessage("system.status");
}

static void sendError(const char *code, const char *detail) {
  JsonObject payload = outgoing.createNestedObject("payload");
  payload["code"] = code;
  payload["detail"] = detail;
  sendMessage("system.error");
}

static void sendTouch(int x, int y, const char *phase) {
  JsonObject payload = outgoing.createNestedObject("payload");
  payload["x"] = x;
  payload["y"] = y;
  payload["phase"] = phase;
  sendMessage("input.touch");
}

// ---------------------------------------------------------------- rendering

// Header and footer are drawn on every screen so the operator can always tell
// the difference between "Nomad has nothing to say" and "the link is dead" —
// the two states that look identical on a screen showing stale content.
static int drawChrome(const char *title) {
  display.fillScreen(kBackground);

  display.setTextDatum(lgfx::top_left);
  display.setFont(&fonts::Font2);
  display.setTextColor(kAccent, kBackground);
  display.drawString(title && title[0] ? title : "Nomad", 8, 6);

  display.drawFastHLine(0, 26, display.width(), kRule);
  return 34;  // first free y
}

static void drawFooter(const char *note) {
  const int y = display.height() - 18;
  display.drawFastHLine(0, y - 4, display.width(), kRule);
  display.setFont(&fonts::Font0);
  display.setTextColor(kMuted, kBackground);
  display.setTextDatum(top_left);
  display.drawString(note, 8, y);
}

// Word-wrapped body text. LovyanGFX has no wrapping primitive that reports the
// height it used, and the card and list screens need to know where the text
// ended, so this measures as it goes.
static int drawWrapped(const char *text, int x, int y, int width, int lineHeight) {
  if (!text || !text[0]) {
    return y;
  }
  char line[96];
  size_t lineLen = 0;
  size_t lastBreak = 0;

  for (const char *p = text;; p++) {
    const bool end = (*p == '\0');
    const bool newline = (*p == '\n');

    if (!end && !newline) {
      if (*p == ' ') {
        lastBreak = lineLen;
      }
      if (lineLen + 1 < sizeof(line)) {
        line[lineLen++] = *p;
      }
      line[lineLen] = '\0';
      if (display.textWidth(line) <= width) {
        continue;
      }
      // Overflowed: break at the last space if there was one, otherwise break
      // mid-word rather than run off the glass.
      size_t cut = lastBreak > 0 ? lastBreak : lineLen - 1;
      const char keep = line[cut];
      line[cut] = '\0';
      display.drawString(line, x, y);
      y += lineHeight;
      line[cut] = keep;

      size_t rest = cut;
      while (line[rest] == ' ') {
        rest++;
      }
      lineLen = 0;
      while (line[rest] != '\0' && lineLen + 1 < sizeof(line)) {
        line[lineLen++] = line[rest++];
      }
      line[lineLen] = '\0';
      lastBreak = 0;
      continue;
    }

    if (lineLen > 0) {
      line[lineLen] = '\0';
      display.drawString(line, x, y);
      y += lineHeight;
      lineLen = 0;
      lastBreak = 0;
    }
    if (end) {
      break;
    }
  }
  return y;
}

static void renderText(JsonObjectConst payload) {
  const int y = drawChrome(payload["title"] | "Nomad");
  display.setFont(&fonts::Font2);
  display.setTextColor(kForeground, kBackground);
  display.setTextDatum(top_left);
  drawWrapped(payload["body"] | "", 8, y, display.width() - 16, 18);
}

static void renderCard(JsonObjectConst payload) {
  int y = drawChrome(payload["title"] | "Nomad");

  display.setFont(&fonts::Font2);
  display.setTextDatum(top_left);
  const char *body = payload["body"] | "";
  if (body[0]) {
    display.setTextColor(kForeground, kBackground);
    y = drawWrapped(body, 8, y, display.width() - 16, 18) + 6;
  }

  // Label/value pairs, values right-aligned: a column of numbers that jitters
  // horizontally is unreadable at a glance, and glanceability is the whole
  // point of a status screen.
  JsonArrayConst rows = payload["rows"];
  for (JsonVariantConst row : rows) {
    if (y > display.height() - 34) {
      display.setTextColor(kMuted, kBackground);
      display.drawString("...", 8, y);
      break;
    }
    const char *label = row[0] | "";
    const char *value = row[1] | "";

    display.setTextDatum(top_left);
    display.setTextColor(kMuted, kBackground);
    display.drawString(label, 8, y);

    display.setTextDatum(top_right);
    display.setTextColor(kForeground, kBackground);
    display.drawString(value, display.width() - 8, y);
    y += 18;
  }
}

static void renderList(JsonObjectConst payload) {
  int y = drawChrome(payload["title"] | "Nomad");
  const bool selectable = payload["selectable"] | false;

  display.setFont(&fonts::Font2);
  display.setTextDatum(top_left);

  JsonArrayConst items = payload["items"];
  int index = 0;
  for (JsonVariantConst item : items) {
    if (y > display.height() - 34) {
      display.setTextColor(kMuted, kBackground);
      display.drawString("...", 8, y);
      break;
    }
    const char *label = item[0] | "";
    const char *detail = item[1] | "";

    if (selectable) {
      // Numbered, because this module has no joystick: the operator's way to
      // pick an item is to say or type its number. See the note in
      // ARCHITECTURE.md about the hardware in hand.
      char prefix[8];
      snprintf(prefix, sizeof(prefix), "%d.", index + 1);
      display.setTextColor(kAccent, kBackground);
      display.drawString(prefix, 8, y);
    }
    display.setTextColor(kForeground, kBackground);
    display.drawString(label, selectable ? 30 : 8, y);

    if (detail && detail[0]) {
      display.setTextDatum(top_right);
      display.setTextColor(kMuted, kBackground);
      display.drawString(detail, display.width() - 8, y);
      display.setTextDatum(top_left);
    }
    y += 18;
    index++;
  }
}

static void renderChoice(JsonObjectConst payload) {
  int y = drawChrome(payload["title"] | "Nomad");

  display.setFont(&fonts::Font2);
  display.setTextDatum(top_left);
  display.setTextColor(kForeground, kBackground);
  y = drawWrapped(payload["question"] | "", 8, y, display.width() - 16, 18) + 8;

  JsonArrayConst options = payload["options"];
  int index = 0;
  for (JsonVariantConst option : options) {
    if (y > display.height() - 34) {
      break;
    }
    char prefix[8];
    snprintf(prefix, sizeof(prefix), "%d.", index + 1);
    display.setTextColor(kAccent, kBackground);
    display.drawString(prefix, 12, y);
    display.setTextColor(kForeground, kBackground);
    display.drawString(option.as<const char *>(), 34, y);
    y += 20;
    index++;
  }
}

static void renderState(JsonObjectConst payload) {
  const char *kind = payload["kind"] | "text";

  if (strcmp(kind, "card") == 0) {
    renderCard(payload);
  } else if (strcmp(kind, "list") == 0) {
    renderList(payload);
  } else if (strcmp(kind, "choice") == 0) {
    renderChoice(payload);
  } else {
    // Unknown kind renders as text rather than as an error. Newer Pi software
    // adding a fifth screen shape must not blank the operator's display.
    renderText(payload);
  }

  framesRendered++;
  char note[48];
  snprintf(note, sizeof(note), "link up  seq %lu  frames %lu",
           (unsigned long)lastSeqSeen, (unsigned long)framesRendered);
  drawFooter(note);
}

// What the screen says before the Pi has ever spoken. Not a fake status screen:
// it names the one thing that is actually known.
static void renderWaiting() {
  const int y = drawChrome("Nomad");
  display.setFont(&fonts::Font2);
  display.setTextColor(kMuted, kBackground);
  display.setTextDatum(top_left);
  display.drawString("waiting for the Pi", 8, y);
  display.setFont(&fonts::Font0);
  display.drawString(kFirmwareVersion, 8, y + 24);
  drawFooter("usb cdc up, no frames yet");
}

// ---------------------------------------------------------------- incoming

static void handleFrame(const uint8_t *body, size_t length) {
  incoming.clear();
  const DeserializationError error = deserializeJson(incoming, body, length);
  if (error) {
    // A body that survived its CRC but will not parse is one bad frame, never a
    // reason to tear down the link — same rule the Python codec follows.
    sendError("bad_json", error.c_str());
    return;
  }

  JsonObjectConst message = incoming.as<JsonObjectConst>();
  const char *type = message["type"] | "";
  lastSeqSeen = message["seq"] | 0;
  JsonObjectConst payload = message["payload"];

  if (strcmp(type, "display.state") == 0) {
    renderState(payload);
  } else if (strcmp(type, "display.backlight") == 0) {
    const bool on = payload["on"] | true;
    const int brightness = payload["brightness"] | 255;
    display.setBrightness(on ? (uint8_t)brightness : 0);
  } else if (strcmp(type, "system.status") == 0) {
    sendStatus();
  } else if (strcmp(type, "system.hello") == 0) {
    // The Pi says hello after it detects a reboot. Answering keeps the
    // handshake symmetric and tells it which capabilities came back up.
    sendHello();
  } else {
    // Unknown types are ignored deliberately, not errored: the envelope keeps
    // `type` a plain string so newer Pi software can send things this firmware
    // has never heard of without breaking the link.
  }
}

static void pumpSerial() {
  while (Serial.available()) {
    if (!framing.push((uint8_t)Serial.read())) {
      // Only reachable if a length prefix passed the size check and its body
      // never arrived. Dropping the buffer loses one frame; keeping it would
      // wedge the parser forever.
      framing.reset();
      sendError("frame_buffer_full", "parser reset");
      return;
    }
  }

  for (;;) {
    nomad::Framing::Loss loss = nomad::Framing::Loss::kNone;
    const size_t length = framing.next(frameBody, sizeof(frameBody), &loss);
    if (length == 0) {
      break;
    }
    handleFrame(frameBody, length);
  }
}

static void pumpTouch() {
#if NOMAD_TOUCH != NOMAD_TOUCH_NONE
  static bool wasDown = false;
  static int lastX = 0;
  static int lastY = 0;

  int32_t x = 0;
  int32_t y = 0;
  const bool down = display.getTouch(&x, &y);

  if (down) {
    if (!wasDown) {
      sendTouch(x, y, "down");
    } else if (abs((int)x - lastX) > 3 || abs((int)y - lastY) > 3) {
      // Threshold, because a finger resting on capacitive glass jitters by a
      // pixel or two continuously and every jitter would be a frame on a link
      // shared with screen updates.
      sendTouch(x, y, "move");
    }
    lastX = x;
    lastY = y;
  } else if (wasDown) {
    sendTouch(lastX, lastY, "up");
  }
  wasDown = down;
#endif
}

// ---------------------------------------------------------------- lifecycle

void setup() {
  Serial.begin(115200);

  display.init();
  display.setRotation(kRotation);
  display.setBrightness(255);
  renderWaiting();

  // Announce unconditionally rather than waiting for the port to be opened. The
  // Pi may already be listening, and if it is not, this frame is lost and its
  // own hello will prompt another.
  sendHello();
}

void loop() {
  pumpSerial();
  pumpTouch();

  // 5 ms: fast enough that touch feels immediate, slow enough that the loop is
  // not spinning the CPU at 100% on a battery-powered device.
  delay(5);
}
