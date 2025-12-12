//
// Created by tludwig on 12/12/25.
//

#pragma once
#include <Arduino.h>

class UsbSerial
{
public:
    void begin(uint32_t waitMs = 0, bool ignoreFlowControl = false);

    // Callbacks (optional)
    void onConnect(void (*cb)())    { onConnect_ = cb; }
    void onDisconnect(void (*cb)()) { onDisconnect_ = cb; }

    // Call this often (e.g. every loop) to update connection state + buffer input
    void tick();

    bool connected() const { return connected_; }

    // Sending
    size_t send(const char* s) { return Serial.print(s); }
    void println(const char* s) { Serial.println(s); }
    void printf(const char* fmt, ...);

    // Reading (raw)
    int available() { return Serial.available(); }
    int read() { return Serial.read(); }
    size_t readBytes(uint8_t* buf, size_t maxLen);

    // Reading (line-based, non-blocking)
    // Returns true when a full line is available in out (without \r/\n)
    bool readLine(char* out, size_t outSize);

private:
    static constexpr size_t LINE_BUF_SIZE = 128;

    bool connected_ = false;
    bool lastDtr_ = false;

    char lineBuf_[LINE_BUF_SIZE]{};
    size_t lineLen_ = 0;
    bool lineReady_ = false;

    void (*onConnect_)() = nullptr;
    void (*onDisconnect_)() = nullptr;
};