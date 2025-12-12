//
// Created by tludwig on 12/12/25.
//

#pragma once

#include "FastLED.h"

class Led{
public:
  void init();
  void setBrightness(fl::u8 brightness);
  void setLed(uint8_t led, uint32_t color);
  void setAllLed(uint32_t color);
};
