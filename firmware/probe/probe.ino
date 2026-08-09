// Nomad chunk W, step 1: find out what this board actually is.
//
// ARCHITECTURE.md records the vendor pin map but *not* which panel controller
// or touch controller the module carries, and the two plausible panels
// (ILI9341, ST7789) need different initialisation. MISO is wired and the touch
// controller sits on I2C, so both answers are readable at runtime. Guessing
// costs a reflash per guess; this costs one.
//
// It is also the cheapest possible proof of the whole toolchain path: if this
// sketch's output arrives on the Pi over USB CDC, then build, flash, and the
// control link's physical layer all work, and chunk W's remaining risk is
// entirely in software we write.
//
// Build with CDC on boot enabled (`CDCOnBoot=cdc`). Without it, Arduino's
// `Serial` is UART0 on IO43/44 and this prints into a header nobody is
// listening to — which is exactly the silence this board arrived in.

#include <SPI.h>
#include <Wire.h>

// Vendor pin map, ARCHITECTURE.md "Pin map (vendor documentation)".
static const int LCD_CS = 10;
static const int LCD_DC = 46;
static const int LCD_SCLK = 12;
static const int LCD_MOSI = 11;
static const int LCD_MISO = 13;
static const int LCD_BL = 45;

static const int TOUCH_SDA = 16;
static const int TOUCH_SCL = 15;
static const int TOUCH_RST = 18;

// Panel reads are the one place a slow clock matters: the controller drives
// MISO from its own internal timing, and 1 MHz is inside every datasheet's
// read window. Writes later can go far faster.
static SPISettings kReadSpi(1000000, MSBFIRST, SPI_MODE0);

// Reads `count` bytes of a panel register. `dummy` covers the controllers that
// clock out one garbage byte before the real answer (ILI9341's 0xD3 does).
static void readPanelRegister(uint8_t command, uint8_t *out, size_t count, bool dummy) {
  SPI.beginTransaction(kReadSpi);
  digitalWrite(LCD_CS, LOW);

  digitalWrite(LCD_DC, LOW);  // command phase
  SPI.transfer(command);
  digitalWrite(LCD_DC, HIGH);  // data phase

  if (dummy) {
    SPI.transfer(0x00);
  }
  for (size_t i = 0; i < count; i++) {
    out[i] = SPI.transfer(0x00);
  }

  digitalWrite(LCD_CS, HIGH);
  SPI.endTransaction();
}

static void printRegister(const char *label, uint8_t command, size_t count, bool dummy) {
  uint8_t buffer[8] = {0};
  readPanelRegister(command, buffer, count, dummy);

  Serial.printf("  %-22s cmd 0x%02X ->", label, command);
  for (size_t i = 0; i < count; i++) {
    Serial.printf(" 0x%02X", buffer[i]);
  }
  Serial.println();
}

// Every byte identical is the signature of a bus that is not answering at all
// (all 0x00 = MISO held low, all 0xFF = floating), as opposed to a controller
// returning an ID we do not recognise. Worth distinguishing, because the fixes
// are completely different.
static bool looksLikeSilence(const uint8_t *buffer, size_t count) {
  for (size_t i = 1; i < count; i++) {
    if (buffer[i] != buffer[0]) {
      return false;
    }
  }
  return buffer[0] == 0x00 || buffer[0] == 0xFF;
}

static void identifyPanel() {
  Serial.println("Panel (SPI read-ID on MISO):");

  // 0x04 RDDID: ST7789 answers 0x85 0x85 0x52. 0xD3 RDDID4: ILI9341 answers
  // 0x00 0x93 0x41, ILI9488 answers 0x00 0x94 0x88. 0x09 RDDST is a liveness
  // check that both support.
  printRegister("RDDID", 0x04, 3, true);
  printRegister("RDDID4", 0xD3, 3, true);
  printRegister("RDDST", 0x09, 4, true);

  uint8_t id4[3] = {0};
  readPanelRegister(0xD3, id4, sizeof(id4), true);
  uint8_t id[3] = {0};
  readPanelRegister(0x04, id, sizeof(id), true);

  Serial.print("  verdict: ");
  if (id4[1] == 0x93 && id4[2] == 0x41) {
    Serial.println("ILI9341");
  } else if (id4[1] == 0x94 && id4[2] == 0x88) {
    Serial.println("ILI9488");
  } else if (id[0] == 0x85 && id[1] == 0x85) {
    Serial.println("ST7789");
  } else if (looksLikeSilence(id4, sizeof(id4)) && looksLikeSilence(id, sizeof(id))) {
    Serial.println("no answer on MISO - the panel may be write-only wired");
  } else {
    Serial.println("unrecognised - record the raw bytes above before choosing a driver");
  }
}

static void identifyTouch() {
  Serial.println("Touch (I2C scan on IO16/IO15):");

  // The vendor map says RST is active low, so it has to be released before the
  // controller will acknowledge anything.
  pinMode(TOUCH_RST, OUTPUT);
  digitalWrite(TOUCH_RST, LOW);
  delay(10);
  digitalWrite(TOUCH_RST, HIGH);
  delay(60);  // GT911 wants ~50ms after reset before it answers

  Wire.begin(TOUCH_SDA, TOUCH_SCL, 100000);

  int found = 0;
  for (uint8_t address = 0x08; address < 0x78; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() != 0) {
      continue;
    }
    found++;

    const char *guess = "unknown";
    switch (address) {
      case 0x5D:
      case 0x14:
        guess = "GT911";
        break;
      case 0x15:
        guess = "CST816";
        break;
      case 0x38:
        guess = "FT6236";
        break;
      default:
        break;
    }
    Serial.printf("  0x%02X responds  (%s)\n", address, guess);
  }

  if (found == 0) {
    Serial.println("  nothing responded - check RST/INT wiring before assuming no touch");
  }
}

void setup() {
  Serial.begin(115200);

  // Wait, but not forever: the device has to survive being powered from a
  // battery with nothing listening. 3 seconds is enough for the Pi to open the
  // port after enumeration.
  const unsigned long deadline = millis() + 3000;
  while (!Serial && millis() < deadline) {
    delay(10);
  }
  delay(200);

  // Backlight on, so the operator can see the board is running this sketch and
  // not the factory demo.
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);

  pinMode(LCD_CS, OUTPUT);
  digitalWrite(LCD_CS, HIGH);
  pinMode(LCD_DC, OUTPUT);
  digitalWrite(LCD_DC, HIGH);
  SPI.begin(LCD_SCLK, LCD_MISO, LCD_MOSI, LCD_CS);

  Serial.println();
  Serial.println("=== nomad probe ===");
  Serial.printf("chip: %s rev %d, %d MHz, %d cores\n", ESP.getChipModel(),
                ESP.getChipRevision(), getCpuFrequencyMhz(), ESP.getChipCores());
  Serial.printf("flash: %u bytes   psram: %u bytes   free heap: %u bytes\n",
                ESP.getFlashChipSize(), ESP.getPsramSize(), ESP.getFreeHeap());
  Serial.println();

  identifyPanel();
  Serial.println();
  identifyTouch();
  Serial.println();
  Serial.println("=== end probe ===");
}

void loop() {
  // Reprint on request rather than on a timer, so the port is quiet unless
  // asked. Any byte will do.
  if (Serial.available()) {
    while (Serial.available()) {
      Serial.read();
    }
    Serial.println();
    identifyPanel();
    Serial.println();
    identifyTouch();
  }
  delay(50);
}
