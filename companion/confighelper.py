# confighelper.py
from __future__ import annotations

import configparser
import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Iterable

_HEX6_RE = re.compile(r"^[0-9a-fA-F]{6}$")


class ConfigError(ValueError):
    pass


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "'") or (s[0] == s[-1] == '"')):
        return s[1:-1]
    return s


def _norm_color_hex(s: str) -> str:
    s = _strip_quotes(s).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if not _HEX6_RE.match(s):
        raise ConfigError(f"Invalid hex color '{s}', expected RRGGBB (e.g. FF8800)")
    return s.upper()


_UNSET = object()


class Section:
    """Tiny typed accessor wrapper around a ConfigParser section."""

    def __init__(self, cp: configparser.ConfigParser, section: str):
        self._cp = cp
        self._section = section

    @property
    def name(self) -> str:
        return self._section

    def has(self, key: str) -> bool:
        return self._cp.has_option(self._section, key)

    def get(
            self,
            key: str,
            default: Any = _UNSET,
            *,
            required: bool = False,
            allow_empty: bool = True,
            strip_quotes: bool = True,
    ) -> str:
        if self._cp.has_option(self._section, key):
            val = self._cp.get(self._section, key, fallback="")
            val = val.strip()
            if strip_quotes:
                val = _strip_quotes(val)
            if not allow_empty and val == "":
                raise ConfigError(f"[{self._section}] '{key}' must not be empty")
            return val

        if required:
            raise ConfigError(f"[{self._section}] missing required option '{key}'")
        if default is _UNSET:
            raise ConfigError(f"[{self._section}] missing option '{key}' (no default)")
        return default

    def getint(
            self,
            key: str,
            default: int | None = None,
            *,
            required: bool = False,
            minval: int | None = None,
            maxval: int | None = None,
    ) -> int:
        raw = self.get(key, default, required=required)
        if raw is None:
            # required=False + no default
            raise ConfigError(f"[{self._section}] missing required key '{key}'")

        # allow numeric strings and quoted numbers
        if isinstance(raw, int):
            val = raw
        else:
            s = str(raw).strip().strip("'").strip('"')
            try:
                val = int(s, 10)
            except Exception:
                raise ConfigError(f"[{self._section}] '{key}' must be int, got '{raw}'") from None

        if minval is not None and val < minval:
            raise ConfigError(f"[{self._section}] '{key}' must be >= {minval}, got {val}")
        if maxval is not None and val > maxval:
            raise ConfigError(f"[{self._section}] '{key}' must be <= {maxval}, got {val}")
        return val


    def getfloat(self, key: str, default: Any = _UNSET, *, required: bool = False,
                 minval: Optional[float] = None, maxval: Optional[float] = None) -> float:
        s = self.get(key, default, required=required)
        try:
            v = float(s)
        except Exception:
            raise ConfigError(f"[{self._section}] '{key}' must be float, got '{s}'") from None
        if minval is not None and v < minval:
            raise ConfigError(f"[{self._section}] '{key}' must be >= {minval}, got {v}")
        if maxval is not None and v > maxval:
            raise ConfigError(f"[{self._section}] '{key}' must be <= {maxval}, got {v}")
        return v

    def getbool(self, key: str, default: Any = _UNSET, *, required: bool = False) -> bool:
        s = self.get(key, default, required=required).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"[{self._section}] '{key}' must be boolean, got '{s}'")

    def getcolor(self, key: str, default: Any = _UNSET, *, required: bool = False) -> str:
        s = self.get(key, default, required=required)
        return _norm_color_hex(s)

    def getjson(self, key: str, default: Any = _UNSET, *, required: bool = False) -> Any:
        s = self.get(key, default, required=required)
        try:
            return json.loads(_strip_quotes(s))
        except Exception as e:
            raise ConfigError(f"[{self._section}] '{key}' invalid JSON: {e}") from None

    def getenum(self, key: str, allowed: Iterable[str], default: Any = _UNSET, *, required: bool = False) -> str:
        s = self.get(key, default, required=required)
        allowed_l = {a.lower(): a for a in allowed}
        k = s.lower()
        if k not in allowed_l:
            raise ConfigError(f"[{self._section}] '{key}' must be one of {list(allowed)}, got '{s}'")
        return allowed_l[k]


@dataclass(frozen=True)
class MoonrakerConfig:
    host: str = "127.0.0.1"
    port: int = 7125


@dataclass(frozen=True)
class McuConfig:
    name: str
    serial: str
    color_all: str = "FF8800"
    color_busy: str = "FFE600"


@dataclass(frozen=True)
class ButtonConfig:
    name: str
    mcu: str
    button_id: int

    led_state: str

    # generic LED fields
    led_color: Optional[str] = None
    led_active_color: Optional[str] = None
    led_inactive_color: Optional[str] = None
    led_busy_color: Optional[str] = None

    led_axis: Optional[str] = None
    led_fan: Optional[str] = None
    led_output: Optional[str] = None
    led_heater: Optional[str] = None  # NOTE: your sample uses led_header typo; we alias it

    led_threshold: Optional[float] = None  # NOTE: aliases led_threshould typo

    gcode: Optional[str] = None
    websocket_message: Optional[Any] = None  # parsed JSON if provided


@dataclass(frozen=True)
class HotkeyConfig:
    moonraker: MoonrakerConfig
    mcus: Dict[str, McuConfig]
    buttons: Dict[str, ButtonConfig]


def load_config(path: str | Path) -> HotkeyConfig:
    path = Path(path)

    cp = configparser.ConfigParser(
        interpolation=None,
        delimiters=("=", ":"),
        inline_comment_prefixes=("#", ";"),
        strict=False,
    )
    # lower-case all keys so config is case-insensitive
    cp.optionxform = str.lower  # type: ignore

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    cp.read(path, encoding="utf-8")

    # --- moonraker defaults ---
    if cp.has_section("moonraker"):
        sec = Section(cp, "moonraker")
        host = sec.get("host", "127.0.0.1")
        port = sec.getint("port", 7125, minval=1, maxval=65535)
    else:
        host, port = "127.0.0.1", 7125
    moon = MoonrakerConfig(host=host, port=port)

    # --- parse MCUs + Buttons ---
    mcus: Dict[str, McuConfig] = {}
    buttons: Dict[str, ButtonConfig] = {}

    for sec_name in cp.sections():
        low = sec_name.strip().lower()

        if low.startswith("mcu "):
            mcu_name = sec_name.split(" ", 1)[1].strip()
            sec = Section(cp, sec_name)

            serial = sec.get("serial", required=True, allow_empty=False)
            color_all = sec.getcolor("color_all", "FF8800")
            color_busy = sec.getcolor("color_busy", "FFE600")

            mcus[mcu_name] = McuConfig(
                name=mcu_name,
                serial=serial,
                color_all=color_all,
                color_busy=color_busy,
            )

        elif low.startswith("button "):
            btn_name = sec_name.split(" ", 1)[1].strip()
            sec = Section(cp, sec_name)

            mcu = sec.get("mcu", required=True, allow_empty=False)
            button_id = sec.getint("button_id", required=True, minval=0, maxval=255)

            led_state = sec.get("led_state", required=True, allow_empty=False).lower()

            # common/optional fields
            led_color = sec.getcolor("led_color", _UNSET) if sec.has("led_color") else None
            led_active_color = sec.getcolor("led_active_color", _UNSET) if sec.has("led_active_color") else None
            led_inactive_color = sec.getcolor("led_inactive_color", _UNSET) if sec.has("led_inactive_color") else None
            led_busy_color = sec.getcolor("led_busy_color", _UNSET) if sec.has("led_busy_color") else None

            led_axis = sec.get("led_axis", _UNSET) if sec.has("led_axis") else None
            led_fan = sec.get("led_fan", _UNSET) if sec.has("led_fan") else None
            led_output = sec.get("led_output", _UNSET) if sec.has("led_output") else None

            # heater name (support your "led_header" typo)
            if sec.has("led_heater"):
                led_heater = sec.get("led_heater")
            elif sec.has("led_header"):
                led_heater = sec.get("led_header")
            else:
                led_heater = None

            # threshold (support your "led_threshould" typo)
            if sec.has("led_threshold"):
                led_threshold = sec.getfloat("led_threshold")
            elif sec.has("led_threshould"):
                led_threshold = sec.getfloat("led_threshould")
            else:
                led_threshold = None

            gcode = sec.get("gcode", _UNSET) if sec.has("gcode") else None

            # websocket_message: if present and non-empty, parse JSON
            websocket_message = None
            if sec.has("websocket_message"):
                raw = sec.get("websocket_message", "")
                raw = raw.strip()
                if raw:
                    websocket_message = sec.getjson("websocket_message")

            buttons[btn_name] = ButtonConfig(
                name=btn_name,
                mcu=mcu,
                button_id=button_id,
                led_state=led_state,
                led_color=led_color,
                led_active_color=led_active_color,
                led_inactive_color=led_inactive_color,
                led_busy_color=led_busy_color,
                led_axis=led_axis,
                led_fan=led_fan,
                led_output=led_output,
                led_heater=led_heater,
                led_threshold=led_threshold,
                gcode=_strip_quotes(gcode) if gcode is not None else None,
                websocket_message=websocket_message,
            )

    # --- validation: referenced mcu must exist ---
    for b in buttons.values():
        if b.mcu not in mcus:
            raise ConfigError(f"[button {b.name}] references unknown mcu '{b.mcu}'")

    return HotkeyConfig(moonraker=moon, mcus=mcus, buttons=buttons)


# Optional: quick debug runner
if __name__ == "__main__":
    import sys
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else ".hotkey-companion.cfg")
    print(cfg)
