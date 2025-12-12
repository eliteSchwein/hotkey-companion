//
// Created by tludwig on 12/12/25.
//

#include "usbserial.h"

#ifndef SERIAL_BAUDRATE
  #define SERIAL_BAUDRATE 250000
#endif

void UsbSerial::begin(uint32_t waitMs, bool ignoreFlowControl) {
  Serial.begin(SERIAL_BAUDRATE);

  if (ignoreFlowControl) {
    Serial.ignoreFlowControl(true);
  }

  if (waitMs) {
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < waitMs) delay(10);
  }

  lastDtr_ = Serial.dtr();
  connected_ = lastDtr_;
}

void UsbSerial::printf(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  Serial.printf(fmt, ap);
  va_end(ap);
}

size_t UsbSerial::readBytes(uint8_t* buf, size_t maxLen) {
  size_t n = 0;
  while (n < maxLen && Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    buf[n++] = (uint8_t)c;
  }
  return n;
}

void UsbSerial::tick() {
  // Detect connect/disconnect via DTR state. :contentReference[oaicite:5]{index=5}
  bool dtr = Serial.dtr();
  if (dtr != lastDtr_) {
    lastDtr_ = dtr;
    connected_ = dtr;

    if (connected_) {
      if (onConnect_) onConnect_();
    } else {
      if (onDisconnect_) onDisconnect_();
    }
  }

  // Read incoming data into a line buffer
  while (Serial.available() && !lineReady_) {
    int ci = Serial.read();
    if (ci < 0) break;
    char c = (char)ci;

    if (c == '\r') continue; // ignore CR
    if (c == '\n') {
      // end of line
      lineBuf_[lineLen_] = '\0';
      lineReady_ = true;
      break;
    }

    if (lineLen_ + 1 < LINE_BUF_SIZE) {
      lineBuf_[lineLen_++] = c;
    } else {
      lineLen_ = 0;
    }
  }
}

bool UsbSerial::readLine(char* out, size_t outSize) {
  if (!lineReady_) return false;

  // copy out
  size_t n = strnlen(lineBuf_, LINE_BUF_SIZE);
  if (outSize == 0) return false;
  if (n >= outSize) n = outSize - 1;
  memcpy(out, lineBuf_, n);
  out[n] = '\0';

  // reset for next line
  lineLen_ = 0;
  lineReady_ = false;
  return true;
}