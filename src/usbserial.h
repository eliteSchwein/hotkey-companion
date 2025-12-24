#pragma once
#include <Arduino.h>
#include <cstddef>

class UsbSerial {
public:
    using Callback = void (*)();

    void begin();   // non-blocking
    void close();   // non-blocking

    void onConnect(Callback cb) { onConnect_ = cb; }

    void tick(); // non-blocking

    bool readLine(char* out, size_t outSize); // non-blocking, CR/LF/CRLF

    static void println(const char* s);
    static void println();
    static void printf(const char* fmt, ...);

private:
    static constexpr size_t LINE_BUF_SIZE = 128;

    Callback onConnect_ = nullptr;

    bool cdcOpen_ = false;      // terminal open (DTR)
    bool connectFired_ = false; // fire onConnect once per open

    char lineBuf_[LINE_BUF_SIZE]{};
    size_t lineLen_ = 0;
    bool lineReady_ = false;

    void resetRx_();
    static bool cdcOpenNow_() ;
};
