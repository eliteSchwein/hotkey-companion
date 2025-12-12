#include <Arduino.h>
#include <cstring>
#include <strings.h>

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
UsbSerial usbSerial;

static constexpr uint32_t DEBOUNCE_MS = 20;
static bool stablePressed[HOTKEY_BUTTONS]{};
static bool lastRaw[HOTKEY_BUTTONS]{};
static uint32_t lastChange[HOTKEY_BUTTONS]{};

bool handleLedCommand(char *line)
{
  char *cmd = strtok(line, " \t");
  if (!cmd) return false;

  if (strcasecmp(cmd, "LED") != 0) return false;

  char *idStr = strtok(nullptr, " \t");
  char *colStr = strtok(nullptr, " \t");
  if (!idStr || !colStr) return false;

  if (strtok(nullptr, " \t") != nullptr) return false;

  char *end1 = nullptr;
  long id = strtol(idStr, &end1, 10);
  if (!end1 || *end1 != '\0' || id < 0 || id > 255) return false;

  if (colStr[0] == '0' && (colStr[1] == 'x' || colStr[1] == 'X')) colStr += 2;

  char *end2 = nullptr;
  unsigned long rgb = strtoul(colStr, &end2, 16);
  if (!end2 || *end2 != '\0') return false;

  uint32_t color = (uint32_t)rgb & 0x00FFFFFFu;

  led.setLed((uint8_t)id, color);
  return true;
}

void onDisconnect()
{
  led.setAllLed(CRGB::Black);
  led.setBrightness(BRIGHTNESS);
}

void onConnect()
{
  usbSerial.println("Connected");
}

void setup() {
  led.init();

  for (uint8_t i = 0; i < HOTKEY_BUTTONS; i++)
  {
    pinMode(HOTKEY_BUTTON_PINS[i], INPUT_PULLUP);
  }

  usbSerial.onDisconnect(onDisconnect);
  usbSerial.onConnect(onConnect);
  usbSerial.begin(1500, true);
}

void loop() {
  usbSerial.tick();

  char line[128];
  if (usbSerial.readLine(line, sizeof(line)))
  {
    handleLedCommand(line);
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
        led.setLed(i, CRGB::Yellow);
        usbSerial.printf("pressed %s\n", i+1);
      }
    }
  }
}