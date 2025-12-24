#include "bootloader.h"
#include <Arduino.h>

#include "usbserial.h"

#ifndef BOOT_KEY_PIN
  #define BOOT_KEY_PIN -1
#endif

#ifndef BOOT_KEY_ACTIVE_LOW
  #define BOOT_KEY_ACTIVE_LOW 1
#endif

#ifndef BOOT_DBL_MS
  #define BOOT_DBL_MS 400
#endif

static constexpr uint32_t DEBOUNCE_MS = 20;
UsbSerial usbSerial;

void Bootloader::init() {
#if BOOT_KEY_PIN >= 0
  pinMode(BOOT_KEY_PIN, INPUT_PULLUP);
  lastRaw_ = lastStable_ =
      (digitalRead(BOOT_KEY_PIN) == (BOOT_KEY_ACTIVE_LOW ? LOW : HIGH));
  lastChangeMs_ = millis();
#else
  // No boot key configured. Nothing to init.
#endif
}

void Bootloader::check() {
#if BOOT_KEY_PIN >= 0
  const uint32_t now = millis();
  const bool raw =
      (digitalRead(BOOT_KEY_PIN) == (BOOT_KEY_ACTIVE_LOW ? LOW : HIGH));

  if (raw != lastRaw_) {
    lastRaw_ = raw;
    lastChangeMs_ = now;
  }

  // debounce to stable state
  if ((now - lastChangeMs_) >= DEBOUNCE_MS && raw != lastStable_) {
    lastStable_ = raw;

    // detect press edge
    if (lastStable_) {
      if ((now - lastPressMs_) <= BOOT_DBL_MS) {
        loadBootloader();
      }
      lastPressMs_ = now;
    }
  }
#else
  // No boot key configured: no periodic check.
#endif
}

void Bootloader::loadBootloader() {
#if defined(ARDUINO_ARCH_RP2040)
  // Reboot into ROM USB UF2 mode (BOOTSEL)
  usbSerial.close();
  rp2040.rebootToBootloader();

#elif defined(ARDUINO_ARCH_STM32)
  #ifndef STM32_SYSMEM_BOOTLOADER_ADDR
    #error "Define STM32_SYSMEM_BOOTLOADER_ADDR (from AN2606) e.g. -DSTM32_SYSMEM_BOOTLOADER_ADDR=0x1FFF0000"
  #endif
  Bootloader::jumpToAddress((uint32_t)STM32_SYSMEM_BOOTLOADER_ADDR);

#else
  NVIC_SystemReset();
#endif
}

#if defined(ARDUINO_ARCH_STM32)
void Bootloader::jumpToAddress(uint32_t addr) {
  __disable_irq();

  // Stop SysTick
  SysTick->CTRL = 0;
  SysTick->LOAD = 0;
  SysTick->VAL  = 0;

  // Optionally de-init clocks/peripherals if HAL is available
  #if defined(HAL_RCC_MODULE_ENABLED)
    HAL_RCC_DeInit();
  #endif
  #if defined(HAL_MODULE_ENABLED)
    HAL_DeInit();
  #endif

  // Set vector table to system memory
  SCB->VTOR = addr;

  // Stack pointer is first word; reset handler is second word
  uint32_t sp = *(volatile uint32_t*)(addr + 0);
  uint32_t rh = *(volatile uint32_t*)(addr + 4);

  __set_MSP(sp);
  __DSB();
  __ISB();

  void (*boot)(void) = (void (*)(void))(rh);
  boot();

  while (1) {}
}
#endif
