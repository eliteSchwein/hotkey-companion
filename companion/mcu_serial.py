# mcu_serial.py
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

try:
    import serial  # pyserial
except Exception as e:  # pragma: no cover
    serial = None
    _serial_import_error = e

Color = Union[str, int]
PressCallback = Callable[[str, int, str], None]


def _norm_color(color: Color) -> str:
    if isinstance(color, int):
        if color < 0 or color > 0xFFFFFF:
            raise ValueError(f"Color int out of range: {color}")
        return f"{color:06X}"
    s = str(color).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.strip().strip("'").strip('"')
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"Invalid color '{color}', expected RRGGBB (e.g. FF8800)")
    return s.upper()


def _parse_pressed_line(line: str) -> Optional[int]:
    parts = line.strip().split()
    if len(parts) != 2:
        return None
    if parts[0].lower() != "pressed":
        return None
    try:
        return int(parts[1], 10)
    except ValueError:
        return None


@dataclass(frozen=True)
class McuSpec:
    name: str
    port: str
    baudrate: int = 250000


class McuConnection:
    def __init__(self, spec: McuSpec, press_cb: Optional[PressCallback] = None):
        self.spec = spec
        self._press_cb = press_cb

        self._ser = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._txq: "queue.Queue[bytes]" = queue.Queue()
        self._rxbuf = bytearray()
        self._lock = threading.Lock()

    def _log(self, msg: str) -> None:
        print(f"[mcu:{self.spec.name}] {msg}", flush=True)

    def set_press_callback(self, cb: Optional[PressCallback]) -> None:
        self._press_cb = cb

    def is_connected(self) -> bool:
        with self._lock:
            return self._ser is not None and getattr(self._ser, "is_open", False)

    def connect(self) -> None:
        if serial is None:
            raise RuntimeError(f"pyserial import failed: {_serial_import_error}")

        with self._lock:
            if self._ser is not None and self._ser.is_open:
                return
            self._ser = serial.Serial(
                port=self.spec.port,
                baudrate=self.spec.baudrate,
                timeout=0,
                write_timeout=0,
            )

        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name=f"mcu-{self.spec.name}", daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)

        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

        while not self._txq.empty():
            try:
                self._txq.get_nowait()
            except Exception:
                break
        self._rxbuf.clear()

    def send_line(self, line: str) -> None:
        data = (line.strip() + "\n").encode("utf-8", errors="replace")
        self._txq.put(data)

    def color_all(self, color: Color) -> None:
        c = _norm_color(color)
        self.send_line(f"SET_ALL C={c}")
        self._log(f"colorAll -> {c}")

    def color_single(self, button_id: int, color: Color) -> None:
        if button_id < 0 or button_id > 255:
            raise ValueError("button_id must be 0..255")
        c = _norm_color(color)
        self.send_line(f"SET_SINGLE B={button_id} C={c}")
        self._log(f"colorSingle -> B={button_id} C={c}")

    def _worker(self) -> None:
        idle_sleep = 0.005
        while not self._stop.is_set():
            with self._lock:
                ser = self._ser

            if ser is None or not ser.is_open:
                time.sleep(idle_sleep)
                continue

            # TX
            try:
                for _ in range(8):
                    try:
                        pkt = self._txq.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        ser.write(pkt)
                    except Exception:
                        break
            except Exception:
                pass

            # RX
            try:
                n = getattr(ser, "in_waiting", 0) or 0
                if n:
                    chunk = ser.read(n)
                    if chunk:
                        self._rxbuf.extend(chunk)
                        self._process_rx_lines()
                else:
                    time.sleep(idle_sleep)
            except Exception:
                time.sleep(idle_sleep)

    def _process_rx_lines(self) -> None:
        while True:
            idx = self._rxbuf.find(b"\n")
            if idx < 0:
                return
            raw = self._rxbuf[: idx + 1]
            del self._rxbuf[: idx + 1]

            line = raw.decode("utf-8", errors="replace").strip()
            if line.endswith("\r"):
                line = line[:-1]

            bid = _parse_pressed_line(line)
            if bid is not None and self._press_cb:
                try:
                    self._press_cb(self.spec.name, bid, line)
                except Exception:
                    pass


class MultiMcuSerial:
    def __init__(self, press_cb: Optional[PressCallback] = None, startup_delay: float = 0.6):
        self._press_cb = press_cb
        self._mcus: Dict[str, McuConnection] = {}

        self._startup_all: Dict[str, str] = {}
        self._static_buttons: Dict[str, List[Tuple[int, str]]] = {}
        self._startup_delay = float(startup_delay)

    def set_press_callback(self, cb: Optional[PressCallback]) -> None:
        self._press_cb = cb
        for m in self._mcus.values():
            m.set_press_callback(cb)

    def configure_static_from_config(self, cfg) -> None:
        self._startup_all.clear()
        self._static_buttons.clear()

        if hasattr(cfg, "mcus"):
            for mcu_name, mcu_cfg in cfg.mcus.items():
                col = getattr(mcu_cfg, "color_all", None)
                if col:
                    self._startup_all[mcu_name] = _norm_color(col)

        for b in cfg.buttons.values():
            if str(getattr(b, "led_state", "")).lower() != "static":
                continue
            col = getattr(b, "led_color", None)
            if not col:
                continue
            mcu_name = getattr(b, "mcu")
            bid = int(getattr(b, "button_id"))
            self._static_buttons.setdefault(mcu_name, []).append((bid, _norm_color(col)))

    def _apply_startup_for_mcu(self, mcu_name: str) -> None:
        conn = self._mcus.get(mcu_name)
        if not conn or not conn.is_connected():
            return

        base = self._startup_all.get(mcu_name)
        if base:
            conn.color_all(base)

        for bid, col in self._static_buttons.get(mcu_name, []):
            conn.color_single(bid, col)

    def connect(self, specs: Dict[str, McuSpec]) -> None:
        for name, spec in specs.items():
            if name not in self._mcus:
                self._mcus[name] = McuConnection(spec, press_cb=self._press_cb)

        for name, conn in self._mcus.items():
            if name in specs:
                conn.connect()
                threading.Timer(self._startup_delay, self._apply_startup_for_mcu, args=(name,)).start()

    def disconnect(self, mcu: Optional[str] = None) -> None:
        if mcu is None:
            for c in self._mcus.values():
                c.disconnect()
            return
        if mcu in self._mcus:
            self._mcus[mcu].disconnect()

    def colorAll(self, color: Color, mcu: Optional[str] = None) -> None:
        if mcu is None:
            for c in self._mcus.values():
                c.color_all(color)
        else:
            self._mcus[mcu].color_all(color)

    def colorSingle(self, mcu: str, button_id: int, color: Color) -> None:
        self._mcus[mcu].color_single(button_id, color)

    def isConnected(self, mcu: str) -> bool:
        return self._mcus[mcu].is_connected()
