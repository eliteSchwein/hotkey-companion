//
// Created by tludwig on 12/12/25.
//

#include "led.h"

#include "FastLED.h"
#include "usbserial.h"

#ifndef LED_PIN
  #define LED_PIN 29
#endif

#ifndef LEDS_PER_BUTTON
  #define LEDS_PER_BUTTON 2
#endif

#ifndef BRIGHTNESS
  #define BRIGHTNESS 64
#endif

#ifndef HOTKEY_BUTTONS
  #define HOTKEY_BUTTONS 12
#endif

#define LED_TYPE WS2812B
#define COLOR_ORDER GRB
#define NUM_LEDS HOTKEY_BUTTONS * LEDS_PER_BUTTON

CRGB leds[NUM_LEDS];

void Led::init()
{
  CFastLED::addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS)
    .setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(BRIGHTNESS);
  setAllLed(CRGB::Yellow);
}

void Led::setBrightness(fl::u8 brightness)
{
  FastLED.setBrightness(brightness);
  FastLED.show();
}

void Led::setLed(uint8_t buttonIndex, uint32_t color)
{
  const uint16_t base = (uint16_t)buttonIndex * (uint16_t)LEDS_PER_BUTTON;
  if (base >= NUM_LEDS) return;

  for (uint8_t i = 0; i < LEDS_PER_BUTTON; i++) {
    const uint16_t ledIndex = base + i;
    if (ledIndex < NUM_LEDS) leds[ledIndex] = (CRGB)color;
  }

  FastLED.show();
}

void Led::setAllLed(uint32_t color)
{
  fill_solid(leds, NUM_LEDS, color);
  FastLED.show();
}
