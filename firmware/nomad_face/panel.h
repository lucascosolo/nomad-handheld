// The panel and touch controllers, configured in code rather than in a
// library's global header.
//
// LovyanGFX is used instead of TFT_eSPI for one reason: TFT_eSPI is configured
// by editing `User_Setup.h` inside the installed library, which means the pin
// map lives outside this repository and a fresh checkout builds a sketch that
// drives the wrong pins. Here the whole configuration is a class in the source
// tree, which is what makes `firmware/` self-contained.
//
// `NOMAD_PANEL` selects the controller. It is a switch and not a guess: run
// `firmware/probe` first, which reads the controller's ID over MISO, and set
// this to what it reports. See "Pin map (vendor documentation)" in
// ARCHITECTURE.md.

#pragma once

#define LGFX_USE_V1
#include <LovyanGFX.hpp>

#define NOMAD_PANEL_ILI9341 1
#define NOMAD_PANEL_ST7789 2

#ifndef NOMAD_PANEL
#define NOMAD_PANEL NOMAD_PANEL_ILI9341
#endif

// Same rule for touch: the probe's I2C scan names the controller, and these are
// the three addresses worth recognising (GT911 0x5D/0x14, CST816 0x15,
// FT6236 0x38). `NOMAD_TOUCH_NONE` builds a working status screen with no
// touch at all, which is the honest configuration if the scan finds nothing —
// the screen is still Nomad's face even when it cannot be poked.
#define NOMAD_TOUCH_NONE 0
#define NOMAD_TOUCH_GT911 1
#define NOMAD_TOUCH_CST816 2
#define NOMAD_TOUCH_FT5X06 3

#ifndef NOMAD_TOUCH
#define NOMAD_TOUCH NOMAD_TOUCH_GT911
#endif

namespace nomad {

// Vendor pin map. Duplicated in `firmware/probe/probe.ino` on purpose — the
// probe must stand alone so it can be flashed on a board this sketch cannot
// yet drive.
static const int kLcdCs = 10;
static const int kLcdDc = 46;
static const int kLcdSclk = 12;
static const int kLcdMosi = 11;
static const int kLcdMiso = 13;
static const int kLcdBacklight = 45;

static const int kTouchSda = 16;
static const int kTouchScl = 15;
static const int kTouchRst = 18;
static const int kTouchInt = 17;

// The glass is 240x320 portrait. Nomad renders landscape (rotation 1), so the
// logical screen is 320x240 — matching `[display] width/height` in nomad.toml.
static const int kPanelWidth = 240;
static const int kPanelHeight = 320;

class NomadDisplay : public lgfx::LGFX_Device {
 public:
  NomadDisplay() {
    {
      auto cfg = bus_.config();
      cfg.spi_host = SPI2_HOST;
      cfg.spi_mode = 0;
      cfg.freq_write = 40000000;
      // Reads are the slow path and only used for identification; the write
      // clock is what sets the frame rate.
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = kLcdSclk;
      cfg.pin_mosi = kLcdMosi;
      cfg.pin_miso = kLcdMiso;
      cfg.pin_dc = kLcdDc;
      bus_.config(cfg);
      panel_.setBus(&bus_);
    }
    {
      auto cfg = panel_.config();
      cfg.pin_cs = kLcdCs;
      // The vendor map says panel RST is tied to the ESP32-S3's own reset, so
      // there is no GPIO to drive and the panel is only ever reset with the
      // chip. -1 tells LovyanGFX not to look for one.
      cfg.pin_rst = -1;
      cfg.pin_busy = -1;
      cfg.panel_width = kPanelWidth;
      cfg.panel_height = kPanelHeight;
      cfg.offset_x = 0;
      cfg.offset_y = 0;
      cfg.offset_rotation = 0;
      cfg.readable = true;
      cfg.invert = (NOMAD_PANEL == NOMAD_PANEL_ST7789);
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      cfg.bus_shared = false;
      panel_.config(cfg);
    }
    {
      auto cfg = light_.config();
      cfg.pin_bl = kLcdBacklight;
      cfg.invert = false;  // vendor map: high = on
      cfg.freq = 44100;
      cfg.pwm_channel = 7;
      light_.config(cfg);
      panel_.setLight(&light_);
    }
#if NOMAD_TOUCH != NOMAD_TOUCH_NONE
    {
      auto cfg = touch_.config();
      cfg.pin_sda = kTouchSda;
      cfg.pin_scl = kTouchScl;
      cfg.pin_rst = kTouchRst;
      cfg.pin_int = kTouchInt;
      cfg.i2c_port = 0;
      cfg.freq = 400000;
      // Touch coordinates are the panel's own, in panel orientation. Rotation
      // is applied by LovyanGFX, so the sketch reads logical screen
      // coordinates and never has to know the glass is mounted portrait.
      cfg.x_min = 0;
      cfg.x_max = kPanelWidth - 1;
      cfg.y_min = 0;
      cfg.y_max = kPanelHeight - 1;
      cfg.bus_shared = false;
#if NOMAD_TOUCH == NOMAD_TOUCH_GT911
      cfg.i2c_addr = 0x5D;
#elif NOMAD_TOUCH == NOMAD_TOUCH_CST816
      cfg.i2c_addr = 0x15;
#else
      cfg.i2c_addr = 0x38;
#endif
      touch_.config(cfg);
      panel_.setTouch(&touch_);
    }
#endif
    setPanel(&panel_);
  }

 private:
  lgfx::Bus_SPI bus_;
#if NOMAD_PANEL == NOMAD_PANEL_ST7789
  lgfx::Panel_ST7789 panel_;
#else
  lgfx::Panel_ILI9341 panel_;
#endif
  lgfx::Light_PWM light_;
#if NOMAD_TOUCH == NOMAD_TOUCH_GT911
  lgfx::Touch_GT911 touch_;
#elif NOMAD_TOUCH == NOMAD_TOUCH_CST816
  lgfx::Touch_CST816S touch_;
#elif NOMAD_TOUCH == NOMAD_TOUCH_FT5X06
  lgfx::Touch_FT5x06 touch_;
#endif
};

}  // namespace nomad
