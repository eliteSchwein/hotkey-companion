//
// Created by tludwig on 12/12/25.
//

#pragma once

#include "FastLED.h"

class Led{
public:
  static void init();
  static void setBrightness(fl::u8 brightness);
  static void setLed(uint8_t led, uint32_t color);
  static void setAllLed(uint32_t color);
};
