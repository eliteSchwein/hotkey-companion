# companion.py
from __future__ import annotations

import json
import queue
import random
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from confighelper import load_config, HotkeyConfig
from mcu_serial import MultiMcuSerial, McuSpec


def _norm_color(color: Any) -> str:
    if color is None:
        return ""
    s = str(color).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.strip().strip("'").strip('"')
    if len(s) != 6:
        return ""
    try:
        int(s, 16)
    except Exception:
        return ""
    return s.upper()


def _get_attr(obj: Any, *names: str) -> Any:
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return None


def build_button_index(cfg: HotkeyConfig) -> Dict[str, Dict[int, List[Any]]]:
    idx: Dict[str, Dict[int, List[Any]]] = {}
    for b in cfg.buttons.values():
        idx.setdefault(b.mcu, {}).setdefault(int(b.button_id), []).append(b)
    return idx


def build_subscribe_objects(cfg: HotkeyConfig, objects_list: List[str]) -> Dict[str, Any]:
    """
    Build a minimal subscription set based on configured dynamic buttons.
    """
    objset = {o.lower(): o for o in objects_list}

    def pick(name: str) -> Optional[str]:
        return objset.get(name.lower(), None)

    want: Dict[str, Any] = {}

    # baseline for common states
    th = pick("toolhead")
    if th:
        want[th] = ["position", "homed_axes"]

    ps = pick("print_stats")
    if ps:
        want[ps] = ["state"]

    # Dynamic button needs
    for b in cfg.buttons.values():
        st = str(getattr(b, "led_state", "")).lower()
        if st in ("", "static"):
            continue

        if st == "homed":
            # already covered by toolhead
            continue

        if st == "printing":
            # already covered by print_stats
            continue

        if st == "heater":
            real = pick("heaters")  # not an object; ignore
            _ = real
            # actual heater objects are like "heater_bed", "extruder"
            name = str(_get_attr(b, "led_heater") or "").strip()
            if name:
                real = pick(name)
                if real:
                    want[real] = ["temperature", "target"]

        if st == "fan":
            name = str(_get_attr(b, "led_fan") or "").strip()
            if name:
                real = pick(name)
                if real:
                    want[real] = ["speed"]

        if st == "output":
            name = str(_get_attr(b, "led_output") or "").strip()
            if name:
                real = pick(name)
                if real:
                    want[real] = None  # subscribe whole object

        if st == "bed_mesh":
            real = pick("bed_mesh")
            if real:
                want[real] = None

    return want


class LedEngine:
    """
    Owns "desired LED state" derived from Moonraker data.
    Busy states are temporary and revert to desired state.
    """

    def __init__(self, cfg: HotkeyConfig, bus: MultiMcuSerial, button_index: Dict[str, Dict[int, List[Any]]]):
        self.cfg = cfg
        self.bus = bus
        self.button_index = button_index

        self._lock = threading.RLock()

        self._objects_map: Dict[str, str] = {}  # lower -> real
        self.state: Dict[str, Dict[str, Any]] = {}  # real_object -> fields dict

        self._busy_until: Dict[Tuple[str, int], float] = {}  # (mcu,bid) -> monotonic time
        self._busy_color: Dict[Tuple[str, int], str] = {}  # (mcu,bid) -> color

        self._last_sent: Dict[Tuple[str, int], str] = {}

    def set_objects_list(self, objects: List[str]) -> None:
        with self._lock:
            self._objects_map = {o.lower(): o for o in objects}

    def _find_obj(self, key: str) -> Optional[str]:
        with self._lock:
            return self._objects_map.get(key.lower())

    def press_busy(self, mcu: str, bid: int, hold_s: float = 0.8) -> None:
        with self._lock:
            btns = self.button_index.get(mcu, {}).get(bid, [])
            mcu_cfg = self.cfg.mcus.get(mcu)
            base_busy = _norm_color(getattr(mcu_cfg, "color_busy", "")) if mcu_cfg else ""
            col = ""

            if btns:
                b = btns[0]
                col = _norm_color(_get_attr(b, "led_busy_color")) or ""

            if not col:
                col = base_busy or "000000"

            self._busy_until[(mcu, bid)] = time.monotonic() + float(hold_s)
            self._busy_color[(mcu, bid)] = col
            self._set(mcu, bid, col, reason=f"BUSY hold={hold_s}s")

    def tick(self) -> None:
        with self._lock:
            now = time.monotonic()
            expired = [k for k, t in self._busy_until.items() if now >= t]
            for k in expired:
                self._busy_until.pop(k, None)
                self._busy_color.pop(k, None)
                mcu, bid = k
                self._revert_after_busy(mcu, bid)

    def _set(self, mcu: str, bid: int, color: str, reason: str = "") -> None:
        with self._lock:
            if not color:
                return
            key = (mcu, bid)
            if self._last_sent.get(key) == color:
                return
            self._last_sent[key] = color
            if reason:
                print(f"[led] mcu={mcu} bid={bid} -> {color} ({reason})", flush=True)
            else:
                print(f"[led] mcu={mcu} bid={bid} -> {color}", flush=True)
            self.bus.colorSingle(mcu, bid, color)

    def _desired_for_button(self, b: Any) -> str:
        mcu_cfg = self.cfg.mcus.get(b.mcu)
        base = _norm_color(getattr(mcu_cfg, "color_all", "000000")) if mcu_cfg else "000000"

        st = str(getattr(b, "led_state", "")).lower()

        if st == "static":
            return _norm_color(_get_attr(b, "led_color")) or base

        # default colors for dynamic
        active = _norm_color(_get_attr(b, "led_active_color", "active_color")) or base
        inactive = _norm_color(_get_attr(b, "led_inactive_color", "inactive_color")) or base

        # HOMED (uses toolhead)
        if st == "homed":
            axis = str(_get_attr(b, "led_axis") or "x").lower().strip()
            toolhead = self._find_obj("toolhead") or "toolhead"
            th = self.state.get(toolhead, {}) or {}

            homed_axes = str(th.get("homed_axes") or "").lower()
            pos = th.get("position")
            x = y = z = 0.0
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                try:
                    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                except Exception:
                    pass

            if axis == "x":
                ok = "x" in homed_axes
                _ = x
            elif axis == "y":
                ok = "y" in homed_axes
                _ = y
            else:
                ok = "z" in homed_axes
                _ = z
            return active if ok else inactive

        # PRINTING (uses print_stats)
        if st == "printing":
            ps = self._find_obj("print_stats") or "print_stats"
            obj = self.state.get(ps, {}) or {}
            state = str(obj.get("state") or "").lower()
            ok = state in ("printing", "paused")
            return active if ok else inactive

        # HEATER: led_heater = "extruder" / "heater_bed"
        if st == "heater":
            name = str(_get_attr(b, "led_heater") or "").strip()
            if not name:
                return inactive
            real = self._find_obj(name) or name
            obj = self.state.get(real, {}) or {}
            try:
                tgt = float(obj.get("target") or 0.0)
            except Exception:
                tgt = 0.0
            ok = tgt > 0.0
            return active if ok else inactive

        # FAN: led_fan = "fan" / "controller_fan ..."
        if st == "fan":
            name = str(_get_attr(b, "led_fan") or "").strip()
            if not name:
                return inactive
            real = self._find_obj(name) or name
            obj = self.state.get(real, {}) or {}
            try:
                spd = float(obj.get("speed") or 0.0)
            except Exception:
                spd = 0.0
            ok = spd > 0.0
            return active if ok else inactive

        # OUTPUT: led_output = "output_pin somepin"
        if st == "output":
            name = str(_get_attr(b, "led_output") or "").strip()
            if not name:
                return inactive
            real = self._find_obj(name) or name
            obj = self.state.get(real, {}) or {}
            # common key: "value" or "state"
            val = obj.get("value", obj.get("state", 0))
            ok = bool(val)
            return active if ok else inactive

        # BED_MESH: active if profile set or mesh exists
        if st == "bed_mesh":
            bm = self._find_obj("bed_mesh") or "bed_mesh"
            obj = self.state.get(bm, {}) or {}
            prof = obj.get("profile_name", "") or ""
            has_mesh = ("mesh_matrix" in obj) or ("probed_matrix" in obj)
            ok = bool(str(prof).strip()) or bool(has_mesh)
            return active if ok else inactive

        return inactive

    def _revert_after_busy(self, mcu: str, bid: int) -> None:
        btns = self.button_index.get(mcu, {}).get(bid, [])
        mcu_cfg = self.cfg.mcus.get(mcu)
        base = _norm_color(getattr(mcu_cfg, "color_all", "000000")) if mcu_cfg else "000000"

        if not btns:
            self._set(mcu, bid, base, reason="revert no cfg -> base")
            return

        b = btns[0]
        desired = self._desired_for_button(b)
        if not desired:
            desired = base
        self._set(mcu, bid, desired, reason="revert -> desired(from moonraker/static)")

    def on_update(self, changes: Dict[str, Any], eventtime: float) -> None:
        with self._lock:
            # merge diffs
            for obj, fields in (changes or {}).items():
                if not isinstance(fields, dict):
                    continue
                if obj not in self.state:
                    self.state[obj] = {}
                self.state[obj].update(fields)

            # recompute all dynamic buttons (but DO NOT override busy)
            for b in self.cfg.buttons.values():
                st = str(getattr(b, "led_state", "")).lower()
                if not st or st == "static":
                    continue

                key = (b.mcu, int(b.button_id))
                if key in self._busy_until:
                    # still busy; ignore moonraker update for now
                    continue

                col = self._desired_for_button(b)
                if col:
                    self._set(b.mcu, int(b.button_id), col, reason=f"dyn {st}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python companion.py <config-file>")
        return 2

    cfg_path = sys.argv[1]
    cfg: HotkeyConfig = load_config(cfg_path)

    specs: Dict[str, McuSpec] = {
        name: McuSpec(name=name, port=m.serial, baudrate=250000)
        for name, m in cfg.mcus.items()
    }

    button_index = build_button_index(cfg)

    # connect to MCUs
    bus = MultiMcuSerial(specs)
    bus.connect()
    bus.colorAll("000000")
    time.sleep(0.1)

    # Moonraker WS client
    from moonraker_ws import MoonrakerWS, MoonrakerConn  # local import to avoid cycles

    moon_cfg = cfg.moonraker
    ws = MoonrakerWS(
        MoonrakerConn(
            host=getattr(moon_cfg, "host", "127.0.0.1"),
            port=int(getattr(moon_cfg, "port", 7125)),
            api_key=getattr(moon_cfg, "api_key", None),
            scheme=getattr(moon_cfg, "scheme", "ws"),
            path=getattr(moon_cfg, "path", "/websocket"),
            client_name=getattr(moon_cfg, "client_name", "hotkey-companion"),
            version=getattr(moon_cfg, "version", "0.0.1"),
            client_type=getattr(moon_cfg, "client_type", "agent"),
            url=getattr(moon_cfg, "url", ""),
        ),
        debug=getattr(moon_cfg, "debug", False),
        auto_reconnect=True,
    )

    engine = LedEngine(cfg, bus, button_index)
    ws.set_status_callback(engine.on_update)

    # stop handling
    stop = {"flag": False}

    def request_stop(*_args):
        stop["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    # action queue
    actions: "queue.Queue[Tuple[str, int]]" = queue.Queue()

    # printer restart detection state
    klippy_state = "unknown"
    subscribed = False
    last_info_check = 0.0
    last_sub_attempt = 0.0
    # 100ms object polling to keep LEDs in sync even if notifications are missed
    last_objects_poll = 0.0
    poll_objects: Dict[str, Any] = {}

    def on_notify(method: str, params: Any) -> None:
        nonlocal subscribed
        # we still use polling as the reliable source, but this helps responsiveness
        if method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
            print(f"[moonraker] {method} -> will re-subscribe when ready", flush=True)
            subscribed = False
            return
        if method == "notify_klippy_ready":
            print("[moonraker] notify_klippy_ready -> will re-subscribe", flush=True)
            subscribed = False
            return
        if method == "notify_klippy_state_changed":
            print(f"[moonraker] notify_klippy_state_changed params={params!r}", flush=True)
            subscribed = False

    ws.set_notify_callback(on_notify)

    # action worker: sends gcode / websocket messages on button press
    def action_worker() -> None:
        while not stop["flag"]:
            try:
                mcu, bid = actions.get(timeout=0.2)
            except queue.Empty:
                continue

            btns = button_index.get(mcu, {}).get(bid, [])
            for b in btns:
                # websocket_message
                msg = getattr(b, "websocket_message", None)
                if msg:
                    try:
                        payload = msg
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        if isinstance(payload, dict):
                            method = payload.get("method")
                            params = payload.get("params", None)
                            if method:
                                # ID random & jsonrpc are handled by ws.call()
                                if ws.is_connected:
                                    ws.call(method, params=params, timeout=5.0)
                                    print(f"[action] ws.call method={method}", flush=True)
                                else:
                                    print(f"[action] moonraker offline, skipped ws.call method={method}", flush=True)
                    except Exception as e:
                        print(f"[action] websocket_message failed: {e}", flush=True)

                # gcode
                gc = getattr(b, "gcode", None)
                if gc:
                    try:
                        if ws.is_connected:
                            ws.gcode_script(gc, timeout=5.0)
                            print(f"[action] gcode sent: {gc}", flush=True)
                        else:
                            print(f"[action] moonraker offline, skipped gcode: {gc}", flush=True)
                    except Exception as e:
                        print(f"[action] gcode failed: {e}", flush=True)

    threading.Thread(target=action_worker, name="actions", daemon=True).start()

    # press handler
    def on_press(mcu_name: str, button_id: int, _raw: str) -> None:
        print(f"[{mcu_name}] pressed {button_id}", flush=True)

        btns = button_index.get(mcu_name, {}).get(button_id, [])
        if not btns:
            # no config => revert immediately to base
            mcu_cfg = cfg.mcus.get(mcu_name)
            base = _norm_color(getattr(mcu_cfg, "color_all", "000000")) if mcu_cfg else "000000"
            bus.colorSingle(mcu_name, button_id, base)
            return

        # set busy color only (no active logic here)
        hold = float(_get_attr(btns[0], "busy_hold_s", "led_busy_hold_s") or 0.8)
        engine.press_busy(mcu_name, int(button_id), hold_s=hold)

        # enqueue action
        actions.put((mcu_name, int(button_id)))

    bus.set_press_callback(on_press)

    # connect moonraker thread (it auto-reconnects)
    try:
        ws.connect(timeout=5.0)
    except Exception as e:
        print(f"[moonraker] connect failed: {e}", flush=True)

    print("Hotkey Companion running. Ctrl+C to exit.", flush=True)

    try:
        while not stop["flag"]:
            engine.tick()

            # if websocket down, we can't talk to moonraker
            if not ws.is_connected:
                subscribed = False
                time.sleep(0.05)
                continue

            now = time.monotonic()

            # ALWAYS poll server.info -> reliably detects printer restart even while ws stays up
            if (now - last_info_check) > 1.5:
                last_info_check = now
                try:
                    info = ws.server_info(timeout=2.0) or {}
                    new_state = str(info.get("klippy_state", "")).lower() or "unknown"

                    if new_state != klippy_state:
                        print(f"[moonraker] klippy_state {klippy_state} -> {new_state}", flush=True)
                        klippy_state = new_state

                    if new_state != "ready":
                        subscribed = False
                    # else ready -> we may subscribe below

                except Exception as e:
                    print(f"[moonraker] server.info failed: {e}", flush=True)
                    subscribed = False

            # (Re)subscribe once ready
            if klippy_state == "ready" and (not subscribed) and (now - last_sub_attempt) > 1.0:
                last_sub_attempt = now
                try:
                    obj_res = ws.objects_list(timeout=5.0) or {}
                    objects = obj_res.get("objects", []) if isinstance(obj_res, dict) else []
                    engine.set_objects_list(objects)

                    sub = build_subscribe_objects(cfg, objects)
                    poll_objects = sub
                    print(f"[moonraker] subscribing to {len(sub)} objects", flush=True)

                    resp = ws.objects_subscribe(sub, timeout=10.0) or {}
                    status = resp.get("status", {}) if isinstance(resp, dict) else {}
                    if isinstance(status, dict) and status:
                        # apply initial full state like an update
                        engine.on_update(status, 0.0)

                    subscribed = True
                    print("[moonraker] subscribed OK", flush=True)

                except Exception as e:
                    subscribed = False
                    print(f"[moonraker] subscribe failed: {e}", flush=True)

            # Every 100ms: query current object state and refresh LEDs
            if subscribed and poll_objects and (now - last_objects_poll) >= 0.1:
                last_objects_poll = now
                try:
                    res = ws.call(
                        "printer.objects.query",
                        params={"objects": poll_objects},
                        timeout=2.0,
                    ) or {}
                    status = res.get("status", {}) if isinstance(res, dict) else {}
                    if isinstance(status, dict) and status:
                        engine.on_update(status, 0.0)
                except Exception as e:
                    if getattr(moon_cfg, "debug", False):
                        print(f"[moonraker] objects.query failed: {e}", flush=True)

            time.sleep(0.02)

    finally:
        try:
            bus.colorAll("000000")
            time.sleep(0.15)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
        try:
            bus.disconnect()
        except Exception:
            pass
        print("Bye.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
