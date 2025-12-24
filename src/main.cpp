#include <Arduino.h>
#include <cstring>
#include <strings.h>

#include "bootloader.h"
#include "led.h"
#include "usbserial.h"

#ifndef HOTKEY_BUTTON_PINS_MAP
  #error "Define HOTKEY_BUTTON_PINS_MAP in platformio.ini, e.g. -DHOTKEY_BUTTON_PINS_MAP=2,3,4,..."
#endif

#ifndef BRIGHTNESS
  #define BRIGHTNESS 64
#endif

#ifndef HOTKEY_BUTTONS
  #define HOTKEY_BUTTONS 12
#endif

inline constexpr uint8_t HOTKEY_BUTTON_PINS[] = { HOTKEY_BUTTON_PINS_MAP };

Led led;
extern UsbSerial usbSerial;
Bootloader bootloader;

static constexpr uint32_t DEBOUNCE_MS = 20;
static bool stablePressed[HOTKEY_BUTTONS]{};
static bool lastRaw[HOTKEY_BUTTONS]{};
static uint32_t lastChange[HOTKEY_BUTTONS]{};

#define STR_HELPER(x) #x
#define STR(x) STR_HELPER(x)

#define STRVA_HELPER(...) #__VA_ARGS__
#define STRVA(...) STRVA_HELPER(__VA_ARGS__)

static void printConfig()
{
  UsbSerial::println("=== CONFIG ===");
  
  // Core settings
  UsbSerial::printf("SERIAL_BAUDRATE=%s\n", STR(SERIAL_BAUDRATE));
  UsbSerial::printf("BRIGHTNESS=%s\n", STR(BRIGHTNESS));
  UsbSerial::printf("HOTKEY_BUTTONS=%s\n", STR(HOTKEY_BUTTONS));

  // Boot key
#if BOOT_KEY_PIN >= 0
  UsbSerial::printf("BOOT_KEY_PIN=%s\n", STR(BOOT_KEY_PIN));
#else
  usbSerial.println("BOOT_KEY_PIN=<disabled>");
#endif
  UsbSerial::printf("BOOT_KEY_ACTIVE_LOW=%s\n", STR(BOOT_KEY_ACTIVE_LOW));
  UsbSerial::printf("BOOT_DBL_MS=%s\n", STR(BOOT_DBL_MS));

  // LED-related (only if defined)
#ifdef LED_PIN
  UsbSerial::printf("LED_PIN=%s\n", STR(LED_PIN));
#else
  usbSerial.println("LED_PIN=<not defined>");
#endif

#ifdef LEDS_PER_BUTTON
  UsbSerial::printf("LEDS_PER_BUTTON=%s\n", STR(LEDS_PER_BUTTON));
#else
  usbSerial.println("LEDS_PER_BUTTON=<not defined>");
#endif

  // Button pin map (macro text + resolved values)
  UsbSerial::printf("HOTKEY_BUTTON_PINS_MAP=%s\n", STRVA(HOTKEY_BUTTON_PINS_MAP));

  UsbSerial::println("HOTKEY_BUTTON_PINS=[");
  for (uint8_t i = 0; i < HOTKEY_BUTTONS; i++) {
    UsbSerial::printf("%u", (unsigned)HOTKEY_BUTTON_PINS[i]);
    if (i + 1 < HOTKEY_BUTTONS) UsbSerial::println(",");
  }
  UsbSerial::println("]");
}

static bool parseKeyVal(const char* tok, const char* key, const char** valOut) {
  size_t klen = strlen(key);
  if (strncasecmp(tok, key, klen) != 0) return false;
  if (tok[klen] != '=') return false;
  *valOut = tok + klen + 1;
  return true;
}

static bool parseU8Dec(const char* s, uint8_t* out) {
  if (!s || !*s) return false;
  char* end = nullptr;
  long v = strtol(s, &end, 10);
  if (!end || *end != '\0') return false;
  if (v < 0 || v > 255) return false;
  *out = (uint8_t)v;
  return true;
}

static bool parseColor24(const char* s, uint32_t* out) {
  if (!s || !*s) return false;
  if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s += 2;

  char* end = nullptr;
  unsigned long v = strtoul(s, &end, 16);
  if (!end || *end != '\0') return false;
  *out = (uint32_t)v & 0x00FFFFFFu;
  return true;
}


enum class CmdResult : uint8_t { Unknown, Ok, Err };

CmdResult handleCommand(char *line)
{
  char *cmd = strtok(line, " \t");
  if (!cmd) return CmdResult::Err;

  if (strcasecmp(cmd, "BOOT_BOOTLOADER") == 0) {
    if (strtok(nullptr, " \t") != nullptr) return CmdResult::Err;
    UsbSerial::println("Rebooting to bootloader...");
    delay(50);
    bootloader.loadBootloader();
    return CmdResult::Ok; // (won't return on RP2040, but fine)
  }

  if (strcasecmp(cmd, "CONFIG") == 0) {
    if (strtok(nullptr, " \t") != nullptr) return CmdResult::Err;
    printConfig();                 // implement as discussed earlier
    return CmdResult::Ok;
  }

  if (strcasecmp(cmd, "SET_SINGLE") == 0) {
    const char* bVal = nullptr;
    const char* cVal = nullptr;

    for (char* tok = strtok(nullptr, " \t"); tok; tok = strtok(nullptr, " \t")) {
      const char* v = nullptr;
      if (parseKeyVal(tok, "B", &v)) { bVal = v; continue; }
      if (parseKeyVal(tok, "C", &v)) { cVal = v; continue; }
      return CmdResult::Err; // unknown token
    }

    uint8_t id;
    uint32_t color;
    if (!bVal || !cVal) return CmdResult::Err;
    if (!parseU8Dec(bVal, &id)) return CmdResult::Err;
    if (!parseColor24(cVal, &color)) return CmdResult::Err;

    Led::setLed(id, color);      // button index -> lights LEDS_PER_BUTTON block
    return CmdResult::Ok;
  }

  // --- SET_ALL C=<hex> ---
  if (strcasecmp(cmd, "SET_ALL") == 0) {
    const char* cVal = nullptr;

    for (char* tok = strtok(nullptr, " \t"); tok; tok = strtok(nullptr, " \t")) {
      const char* v = nullptr;
      if (parseKeyVal(tok, "C", &v)) { cVal = v; continue; }
      return CmdResult::Err;
    }

    uint32_t color;
    if (!cVal) return CmdResult::Err;
    if (!parseColor24(cVal, &color)) return CmdResult::Err;

    Led::setAllLed(color);
    return CmdResult::Ok;
  }

  return CmdResult::Unknown;
}

void onConnect()
{
  UsbSerial::println("Hotkey Companion Firmware V0.0.1");
  Led::setAllLed(CRGB::Black);
  Led::setBrightness(BRIGHTNESS);
}

void setup() {
  bootloader.init();
  Led::init();

  for (unsigned char i : HOTKEY_BUTTON_PINS)
  {
    pinMode(i, INPUT_PULLUP);
  }

  usbSerial.onConnect(onConnect);
  usbSerial.begin();
}

void loop() {
  delay(10);
  bootloader.check();
  usbSerial.tick();

  char line[128];
  if (usbSerial.readLine(line, sizeof(line)))
  {
    handleCommand(line);
  }

  if (usbSerial.readLine(line, sizeof(line))) {
    CmdResult r = handleCommand(line);
    if (r == CmdResult::Ok) UsbSerial::println("OK");
    else if (r == CmdResult::Err) UsbSerial::println("ERR");
    else UsbSerial::println("UNKNOWN");
  }

  uint32_t now = millis();

  for (uint8_t i = 0; i < HOTKEY_BUTTONS; i++)
  {
    bool rawPressed = digitalRead(HOTKEY_BUTTON_PINS[i]) == LOW;

    if (rawPressed != lastRaw[i]) {
      lastRaw[i] = rawPressed;
      lastChange[i] = now;
    }

    if ((now - lastChange[i]) >= DEBOUNCE_MS && rawPressed != stablePressed[i]) {
      stablePressed[i] = rawPressed;

      if (stablePressed[i])
      {
        //Led::setLed(i, CRGB::Yellow);
        UsbSerial::printf("pressed %u\n", i);
      }
    }
  }
}