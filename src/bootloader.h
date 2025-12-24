//
// Created by tludwig on 12/24/25.
//

#pragma once
#include <Arduino.h>

class Bootloader {
public:
    void init();
    void check();
    void loadBootloader();

#if defined(ARDUINO_ARCH_STM32)
    static void jumpToAddress(uint32_t addr);
#endif

    bool lastRaw_ = false;
    bool lastStable_ = false;
    uint32_t lastChangeMs_ = 0;
    uint32_t lastPressMs_ = 0;
};