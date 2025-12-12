//
// Created by tludwig on 12/12/25.
//

#include "led.h"
#include <cstdint>

#include "FastLED.h"

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
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS)
    .setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(BRIGHTNESS);
  setAllLed(CRGB::Yellow);
}

void Led::setBrightness(fl::u8 brightness)
{
  FastLED.setBrightness(brightness);
  FastLED.show();
}

void Led::setLed(uint8_t index, uint32_t color)
{
  if (index >= NUM_LEDS) return;
  leds[index] = color;
  FastLED.show();
}

void Led::setAllLed(uint32_t color)
{
  fill_solid(leds, NUM_LEDS, color);
  FastLED.show();
}
