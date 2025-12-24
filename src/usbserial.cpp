#include "usbserial.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>

#ifndef SERIAL_BAUDRATE
  #define SERIAL_BAUDRATE 250000
#endif

#if defined(ARDUINO_ARCH_RP2040)
  #include "tusb.h"
#endif

void UsbSerial::resetRx_() {
  lineLen_ = 0;
  lineReady_ = false;
  lineBuf_[0] = '\0';
}

bool UsbSerial::cdcOpenNow_() {
#if defined(ARDUINO_ARCH_RP2040)
  // true when host has opened the CDC port (DTR)
  return tud_cdc_connected();
#else
  return Serial.dtr();
#endif
}

void UsbSerial::begin() {
  Serial.begin(SERIAL_BAUDRATE);
  Serial.ignoreFlowControl(true); // avoid blocking writes

  resetRx_();

  cdcOpen_ = cdcOpenNow_();
  connectFired_ = false;

  // If already open (rare), fire immediately
  if (cdcOpen_ && onConnect_) {
    connectFired_ = true;
    onConnect_();
  }
}

void UsbSerial::close() {
  cdcOpen_ = false;
  connectFired_ = false;
  resetRx_();
  Serial.end();
}

void UsbSerial::tick() {
#if defined(ARDUINO_ARCH_RP2040)
  // If USB isn't even mounted, don't touch Serial I/O
  if (!tud_mounted()) return;
#endif

  bool nowOpen = cdcOpenNow_();

  // Fire onConnect once each time the terminal opens
  if (nowOpen && !connectFired_) {
    connectFired_ = true;
    if (onConnect_) onConnect_();
  }

  // If terminal closed, allow firing again next open and drop partial RX
  if (!nowOpen && cdcOpen_) {
    connectFired_ = false;
    resetRx_();
  }
  cdcOpen_ = nowOpen;

  // Non-blocking RX line accumulation
  while (Serial.available() && !lineReady_) {
    int ci = Serial.read();
    if (ci < 0) break;
    char c = (char)ci;

    // EOL on CR or LF; swallow LF after CR (CRLF)
    if (c == '\r' || c == '\n') {
      lineBuf_[lineLen_] = '\0';
      lineReady_ = true;

      if (c == '\r' && Serial.available() && Serial.peek() == '\n') {
        (void)Serial.read();
      }
      break;
    }

    if (lineLen_ + 1 < LINE_BUF_SIZE) {
      lineBuf_[lineLen_++] = c;
    } else {
      resetRx_(); // overflow -> drop line
    }
  }
}

bool UsbSerial::readLine(char* out, size_t outSize) {
  if (!lineReady_) return false;
  if (!out || outSize == 0) return false;

  size_t n = strnlen(lineBuf_, LINE_BUF_SIZE);
  if (n >= outSize) n = outSize - 1;

  memcpy(out, lineBuf_, n);
  out[n] = '\0';

  resetRx_();
  return true;
}

void UsbSerial::println(const char* s) {
#if defined(ARDUINO_ARCH_RP2040)
  if (!tud_mounted()) return;
#endif
  Serial.println(s);
}

void UsbSerial::println() {
#if defined(ARDUINO_ARCH_RP2040)
  if (!tud_mounted()) return;
#endif
  Serial.println();
}

void UsbSerial::printf(const char* fmt, ...) {
#if defined(ARDUINO_ARCH_RP2040)
  if (!tud_mounted()) return;
#endif
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.print(buf);
}
