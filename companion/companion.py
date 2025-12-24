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

from confighelper import load_config, HotkeyConfig, ButtonConfig
from mcu_serial import MultiMcuSerial, McuSpec
from moonraker_ws import MoonrakerWS, MoonrakerConn


# ---------- helpers ----------

def build_button_index(cfg: HotkeyConfig) -> Dict[str, Dict[int, List[ButtonConfig]]]:
    idx: Dict[str, Dict[int, List[ButtonConfig]]] = {}
    for b in cfg.buttons.values():
        idx.setdefault(b.mcu, {}).setdefault(int(b.button_id), []).append(b)
    return idx


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


def _get_attr(b: Any, *names: str) -> Any:
    for n in names:
        if hasattr(b, n):
            v = getattr(b, n)
            if v is not None and v != "":
                return v
        if isinstance(b, dict) and n in b and b[n] not in (None, ""):
            return b[n]
    return None


def _norm_color(c: Any) -> Optional[str]:
    if c is None:
        return None
    s = str(c).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.strip().strip("'").strip('"')
    if len(s) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in s):
        return s.upper()
    return None


def _get_thresh_with_source(b: ButtonConfig, default: float = 0.5) -> tuple[float, str]:
    v = getattr(b, "led_threshould", None)  # keep typo
    if v is not None:
        x = _num(v)
        return (float(default) if x is None else float(x), f"led_threshould={v!r}")

    v2 = getattr(b, "led_threshold", None)
    if v2 is not None:
        x = _num(v2)
        return (float(default) if x is None else float(x), f"led_threshold={v2!r}")

    return (float(default), f"default={default}")


def _make_obj_lut(objects: List[str]) -> Dict[str, str]:
    lut: Dict[str, str] = {}
    for o in objects:
        if isinstance(o, str):
            lut[o.lower()] = o
    return lut


def _resolve_output_object(lut: Dict[str, str], out_name: str) -> Optional[str]:
    n = str(out_name).strip().lower()
    if not n:
        return None
    for cand in (f"output_pin {n}", n):
        if cand in lut:
            return lut[cand]
    return None


def _resolve_fan_object(lut: Dict[str, str], fan_name: str) -> Optional[str]:
    n = str(fan_name).strip().lower()
    if not n:
        return None
    cands = [
        f"fan_generic {n}",
        f"heater_fan {n}",
        f"controller_fan {n}",
        n,
    ]
    if n == "fan":
        cands.insert(0, "fan")
    for cand in cands:
        if cand in lut:
            return lut[cand]
    return None


def build_subscribe_objects(cfg: HotkeyConfig, objects: List[str]) -> Dict[str, Any]:
    lut = _make_obj_lut(objects)
    sub: Dict[str, Any] = {}

    if "toolhead" in lut:
        sub["toolhead"] = ["homed_axes", "position"]

    if "gcode_move" in lut:
        sub["gcode_move"] = ["gcode_position"]

    if "z_tilt" in lut:
        sub["z_tilt"] = ["applied"]
    if "quad_gantry_level" in lut:
        sub["quad_gantry_level"] = ["applied"]
    if "bed_mesh" in lut:
        sub["bed_mesh"] = ["profile_name"]

    for b in cfg.buttons.values():
        st = str(getattr(b, "led_state", "")).lower()

        if st == "fan":
            fan_name = _get_attr(b, "led_fan")
            if fan_name:
                obj = _resolve_fan_object(lut, str(fan_name))
                if obj:
                    sub[obj] = ["speed"]

        elif st == "output":
            out_name = _get_attr(b, "led_output")
            if out_name:
                obj = _resolve_output_object(lut, str(out_name))
                if obj:
                    sub[obj] = ["value"]

        elif st == "heater":
            heater = _get_attr(b, "led_heater", "led_header")  # typo compat
            if heater:
                h = str(heater).strip()
                obj = lut.get(h.lower(), h)
                sub[obj] = ["power", "target", "temperature"]

    return sub


def normalize_jsonrpc(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure payload is a JSON-RPC 2.0 request with an id.
    Auto-fills:
      - jsonrpc="2.0"
      - id=random int
    """
    out = dict(payload)

    if out.get("jsonrpc") != "2.0":
        out["jsonrpc"] = "2.0"

    if "id" not in out or out["id"] in (None, ""):
        out["id"] = random.randint(1, 2_000_000_000)

    return out


# ---------- LED engine (Moonraker-driven) ----------

class LedEngine:
    def __init__(self, cfg: HotkeyConfig, bus: MultiMcuSerial, button_index: Dict[str, Dict[int, List[ButtonConfig]]]):
        self.cfg = cfg
        self.bus = bus
        self.button_index = button_index

        self.obj_lut: Dict[str, str] = {}
        self.state: Dict[str, Dict[str, Any]] = {}

        self.update_seq: int = 0
        self.pending_seq: Dict[Tuple[str, int], int] = {}

        self.busy_until: Dict[Tuple[str, int], float] = {}
        self.last_color: Dict[Tuple[str, int], str] = {}

        self._lock = threading.Lock()

    def set_objects_list(self, objects: List[str]) -> None:
        with self._lock:
            self.obj_lut = _make_obj_lut(objects)

    def on_update(self, changes: Dict[str, Any], _eventtime: float) -> None:
        with self._lock:
            self.update_seq += 1
            for obj, fields in changes.items():
                if isinstance(fields, dict):
                    self.state.setdefault(obj, {}).update(fields)

            seq = self.update_seq
            lut = dict(self.obj_lut)
            state_copy = {k: dict(v) for k, v in self.state.items()}
            busy_copy = dict(self.busy_until)
            pending_copy = dict(self.pending_seq)

        self.apply_dynamic(seq=seq, lut=lut, state=state_copy, busy_copy=busy_copy, pending_copy=pending_copy)

    def press_busy(self, mcu: str, bid: int, hold_s: float = 0.8) -> None:
        btns = self.button_index.get(mcu, {}).get(bid, [])
        b0 = btns[0] if btns else None
        mcu_cfg = self.cfg.mcus.get(mcu)

        busy_color = (
                         _norm_color(_get_attr(b0, "led_busy_color")) if b0 is not None else None
                     ) or (
                         _norm_color(getattr(mcu_cfg, "color_busy", "FFE600")) if mcu_cfg else "FFE600"
                     )

        key = (mcu, bid)
        with self._lock:
            self.busy_until[key] = time.monotonic() + float(hold_s)
            self.pending_seq[key] = self.update_seq

        self._set(mcu, bid, busy_color, why="press->busy")

    def tick(self) -> None:
        now = time.monotonic()
        expired: List[Tuple[str, int]] = []
        with self._lock:
            for k, until in list(self.busy_until.items()):
                if now >= until:
                    expired.append(k)
                    del self.busy_until[k]

        for mcu, bid in expired:
            self.revert_after_busy(mcu, bid)

    def revert_after_busy(self, mcu: str, bid: int) -> None:
        mcu_cfg = self.cfg.mcus.get(mcu)
        base = _norm_color(getattr(mcu_cfg, "color_all", "000000")) if mcu_cfg else "000000"

        btns = self.button_index.get(mcu, {}).get(bid, [])
        if not btns:
            self._set(mcu, bid, base, why="revert no cfg -> base")
            return

        b = btns[0]
        st = str(getattr(b, "led_state", "")).lower()

        if st == "static":
            col = _norm_color(_get_attr(b, "led_color")) or base
            self._set(mcu, bid, col, why="revert static")
            return

        with self._lock:
            lut = dict(self.obj_lut)
            state_copy = {k: dict(v) for k, v in self.state.items()}

        res = self._desired_color_for_button(b, lut, state_copy)
        if res is not None:
            col, logmsg = res
            self._set(mcu, bid, col, why=f"revert dynamic -> {logmsg}")
            return

        inactive = _norm_color(_get_attr(b, "led_inactive_color", "inactive_color")) or base
        self._set(mcu, bid, inactive, why="revert dynamic (no moonraker data) -> inactive/base")

    def apply_dynamic(
            self,
            seq: int,
            lut: Dict[str, str],
            state: Dict[str, Dict[str, Any]],
            busy_copy: Dict[Tuple[str, int], float],
            pending_copy: Dict[Tuple[str, int], int],
    ) -> None:
        now = time.monotonic()

        for b in self.cfg.buttons.values():
            st = str(getattr(b, "led_state", "")).lower()
            if not st or st == "static":
                continue

            mcu = b.mcu
            bid = int(b.button_id)
            key = (mcu, bid)

            if busy_copy.get(key, 0.0) > now:
                continue

            pend = pending_copy.get(key)
            if pend is not None and seq <= pend:
                continue

            res = self._desired_color_for_button(b, lut, state)
            if res is None:
                continue

            col, logmsg = res
            self._set(mcu, bid, col, why=f"dyn {logmsg}")

            if pend is not None and seq > pend:
                with self._lock:
                    self.pending_seq.pop(key, None)

    def _desired_color_for_button(
            self, b: ButtonConfig, lut: Dict[str, str], state: Dict[str, Dict[str, Any]]
    ) -> Optional[tuple[str, str]]:
        st = str(getattr(b, "led_state", "")).lower()

        mcu_cfg = self.cfg.mcus.get(b.mcu)
        base = _norm_color(getattr(mcu_cfg, "color_all", "FF8800")) if mcu_cfg else "FF8800"

        active_col = _norm_color(_get_attr(b, "led_active_color", "active_color")) or "00FF00"
        inactive_col = _norm_color(_get_attr(b, "led_inactive_color", "inactive_color")) or base
        busy_col = _norm_color(_get_attr(b, "led_busy_color")) or "FFE600"

        # OUTPUT
        if st == "output":
            out_name = _get_attr(b, "led_output")
            if not out_name:
                return None
            obj = _resolve_output_object(lut, str(out_name))
            if not obj:
                return None

            raw_val = state.get(obj, {}).get("value", 0.0)
            val = _num(raw_val)
            thr, thr_src = _get_thresh_with_source(b, 0.5)

            is_active = (val is not None) and (val >= thr)
            col = active_col if is_active else inactive_col
            return col, f"output {out_name} {obj}.value={raw_val!r} parsed={val} thr={thr}({thr_src}) active={is_active}"

        # FAN
        if st == "fan":
            fan_name = _get_attr(b, "led_fan")
            if not fan_name:
                return None
            obj = _resolve_fan_object(lut, str(fan_name))
            if not obj:
                return None

            raw = state.get(obj, {}).get("speed", 0.0)
            val = _num(raw)
            thr, thr_src = _get_thresh_with_source(b, 0.5)

            is_active = (val is not None) and (val >= thr)
            col = active_col if is_active else inactive_col
            return col, f"fan {fan_name} {obj}.speed={raw!r} parsed={val} thr={thr}({thr_src}) active={is_active}"

        # HEATER
        if st == "heater":
            heater = _get_attr(b, "led_heater", "led_header")
            if not heater:
                return None
            h = str(heater).strip()
            obj = lut.get(h.lower(), h)

            fields = state.get(obj, {})
            raw = None
            key = None
            for k in ("power", "target", "temperature"):
                if k in fields and fields[k] is not None:
                    raw = fields[k]
                    key = k
                    break

            val = _num(raw)
            thr, thr_src = _get_thresh_with_source(b, 0.5)

            is_active = (val is not None) and (val >= thr)
            col = active_col if is_active else inactive_col
            return col, f"heater {heater} {obj}.{key}={raw!r} parsed={val} thr={thr}({thr_src}) active={is_active}"

        # HOMED (active only when homed; busy when gcode_position < 0)
        if st == "homed":
            toolhead = state.get("toolhead", {})
            raw_homed = str(toolhead.get("homed_axes", "")).lower()
            homed_set = {c for c in raw_homed if c in ("x", "y", "z")}

            axis = str(_get_attr(b, "led_axis") or "").strip().lower()
            is_all = axis in ("", "all", "xyz")

            pos_src = "toolhead.position"
            pos = toolhead.get("position")

            gcm = state.get("gcode_move", {})
            gpos = gcm.get("gcode_position")
            if gpos is not None:
                pos_src = "gcode_move.gcode_position"
                pos = gpos

            x = y = z = None
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                x, y, z = pos[0], pos[1], pos[2]
            elif isinstance(pos, dict):
                x, y, z = pos.get("x"), pos.get("y"), pos.get("z")

            def below0(v: Any) -> bool:
                n = _num(v)
                return (n is not None) and (n < -0.001)

            homing_now = {"x": below0(x), "y": below0(y), "z": below0(z)}

            # if already homed, don't count as "currently homing"
            for a in ("x", "y", "z"):
                if a in homed_set:
                    homing_now[a] = False

            current = next((a for a in ("x", "y", "z") if homing_now[a]), None)

            if is_all:
                if {"x", "y", "z"}.issubset(homed_set):
                    return active_col, f"homed ALL ACTIVE homed_set={''.join(sorted(homed_set))}"
                if current is not None:
                    return busy_col, f"homed ALL BUSY current={current} pos_src={pos_src} pos={pos!r} homed_set={''.join(sorted(homed_set))}"
                return inactive_col, f"homed ALL INACTIVE pos_src={pos_src} pos={pos!r} homed_set={''.join(sorted(homed_set))}"

            ax = axis if axis in ("x", "y", "z") else "x"
            if ax in homed_set:
                return active_col, f"homed {ax} ACTIVE homed_set={''.join(sorted(homed_set))}"
            if current == ax:
                return busy_col, f"homed {ax} BUSY pos_src={pos_src} pos={pos!r} homed_set={''.join(sorted(homed_set))}"
            return inactive_col, f"homed {ax} INACTIVE pos_src={pos_src} pos={pos!r} homed_set={''.join(sorted(homed_set))}"

        # Z_TILT
        if st == "z_tilt":
            applied = bool(state.get("z_tilt", {}).get("applied", False))
            return (active_col if applied else inactive_col), f"z_tilt applied={applied}"

        # QGL/QCL
        if st in ("qgl", "qcl", "quad_gantry_level"):
            applied = bool(state.get("quad_gantry_level", {}).get("applied", False))
            return (active_col if applied else inactive_col), f"qgl applied={applied}"

        # BED_MESH
        if st in ("bed_mesh", "mesh"):
            prof = state.get("bed_mesh", {}).get("profile_name", None)
            applied = bool(prof)
            return (active_col if applied else inactive_col), f"bed_mesh profile_name={prof!r}"

        return None

    def _set(self, mcu: str, bid: int, color: str, why: str) -> None:
        c = _norm_color(color) or "000000"
        key = (mcu, bid)
        if self.last_color.get(key) == c:
            return
        self.last_color[key] = c
        print(f"[led] mcu={mcu} bid={bid} -> {c} ({why})", flush=True)
        self.bus.colorSingle(mcu, bid, c)


# ---------- startup paint ----------

def paint_startup(cfg: HotkeyConfig, bus: MultiMcuSerial) -> None:
    """
    Startup policy:
      1) SET_ALL(mcu.color_all)
      2) For every configured button:
         - static -> led_color
         - dynamic -> led_inactive_color (fallback to base if missing)
    """
    for name, m in cfg.mcus.items():
        base = _norm_color(getattr(m, "color_all", None)) or "000000"
        bus.colorAll(base, mcu=name)

    for b in cfg.buttons.values():
        mcu = b.mcu
        bid = int(b.button_id)
        st = str(getattr(b, "led_state", "")).lower()

        mcu_cfg = cfg.mcus.get(mcu)
        base = _norm_color(getattr(mcu_cfg, "color_all", None)) or "000000"

        if st == "static":
            col = _norm_color(getattr(b, "led_color", None))
            if col:
                bus.colorSingle(mcu, bid, col)
        else:
            inactive = _norm_color(getattr(b, "led_inactive_color", None)) or base
            bus.colorSingle(mcu, bid, inactive)


# ---------- main ----------

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python companion.py <config-file>")
        return 2

    cfg_path = sys.argv[1]
    cfg: HotkeyConfig = load_config(cfg_path)
    button_index = build_button_index(cfg)

    specs: Dict[str, McuSpec] = {
        name: McuSpec(name=name, port=m.serial, baudrate=250000)
        for name, m in cfg.mcus.items()
    }

    stop = {"flag": False}

    def request_stop(*_args):
        stop["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    actions: "queue.Queue[Tuple[str, int]]" = queue.Queue()

    bus = MultiMcuSerial(press_cb=None, startup_delay=0.0)
    bus.connect(specs)

    time.sleep(0.25)
    paint_startup(cfg, bus)

    moon_cfg = cfg.moonraker
    ws = MoonrakerWS(
        MoonrakerConn(
            host=getattr(moon_cfg, "host", "127.0.0.1"),
            port=int(getattr(moon_cfg, "port", 7125)),
            api_key=getattr(moon_cfg, "api_key", None),
            scheme=getattr(moon_cfg, "scheme", "ws"),
            path=getattr(moon_cfg, "path", "/websocket"),
        ),
        debug=getattr(moon_cfg, "debug", False),
        auto_reconnect=True,
    )

    engine = LedEngine(cfg, bus, button_index)
    ws.set_status_callback(engine.on_update)

    def action_worker() -> None:
        while not stop["flag"]:
            try:
                mcu, bid = actions.get(timeout=0.2)
            except queue.Empty:
                continue

            btns = button_index.get(mcu, {}).get(bid, [])
            for b in btns:
                msg = getattr(b, "websocket_message", None)
                if msg:
                    try:
                        # msg can be either a JSON string OR already a dict
                        if isinstance(msg, dict):
                            payload = msg
                        elif isinstance(msg, (str, bytes, bytearray)):
                            payload = json.loads(msg)
                        else:
                            raise TypeError(f"websocket_message must be dict or JSON string, got {type(msg).__name__}")

                        if not isinstance(payload, dict):
                            raise ValueError("websocket_message must be a JSON object (dict)")

                        req = normalize_jsonrpc(payload)

                        method = req.get("method")
                        if not method:
                            raise ValueError("websocket_message missing 'method'")

                        params = req.get("params", None)

                        if ws.is_connected:
                            ws.call(method, params=params, timeout=5.0)
                            print(f"[action] moonraker rpc sent id={req['id']} method={method}", flush=True)
                        else:
                            print(f"[action] moonraker offline, skipped rpc id={req['id']} method={method}", flush=True)

                    except Exception as e:
                        print(f"[action] websocket_message failed: {e}", flush=True)


                gc = getattr(b, "gcode", None)
                if gc:
                    try:
                        if ws.is_connected:
                            ws.send_gcode(str(gc), timeout=30.0)
                            print(f"[action] gcode sent: {gc}", flush=True)
                        else:
                            print(f"[action] moonraker offline, skipped gcode: {gc}", flush=True)
                    except Exception as e:
                        print(f"[action] gcode failed: {e}", flush=True)

    threading.Thread(target=action_worker, name="actions", daemon=True).start()

    def on_press(mcu_name: str, button_id: int, _raw: str) -> None:
        print(f"[{mcu_name}] pressed {button_id}", flush=True)

        btns = button_index.get(mcu_name, {}).get(button_id, [])
        if not btns:
            mcu_cfg = cfg.mcus.get(mcu_name)
            base = _norm_color(getattr(mcu_cfg, "color_all", "000000")) if mcu_cfg else "000000"
            bus.colorSingle(mcu_name, button_id, base)
            return

        engine.press_busy(mcu_name, button_id, hold_s=0.8)
        actions.put((mcu_name, button_id))

    bus.set_press_callback(on_press)

    subscribed = False
    last_try = 0.0

    try:
        ws.connect(timeout=5.0)
    except Exception as e:
        print(f"[moonraker] connect failed: {e}", flush=True)

    print("Hotkey Companion running. Ctrl+C to exit.", flush=True)

    try:
        while not stop["flag"]:
            engine.tick()

            if ws.is_connected and not subscribed and (time.monotonic() - last_try) > 1.0:
                last_try = time.monotonic()
                try:
                    obj_res = ws.objects_list() or {}
                    objects = obj_res.get("objects", []) or []
                    engine.set_objects_list(objects)

                    sub = build_subscribe_objects(cfg, objects)
                    print(f"[moonraker] subscribing to {len(sub)} objects", flush=True)

                    sub_res = ws.objects_subscribe(sub) or {}
                    status = (sub_res.get("status") or {}) if isinstance(sub_res, dict) else {}

                    engine.on_update(status, 0.0)

                    subscribed = True
                    print("[moonraker] subscribed OK", flush=True)
                except Exception as e:
                    print(f"[moonraker] subscribe failed: {e}", flush=True)
                    subscribed = False

            if not ws.is_connected:
                subscribed = False

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
