# moonraker_ws.py
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import websocket  # websocket-client

StatusUpdateCb = Callable[[Dict[str, Any], float], None]


@dataclass
class MoonrakerConn:
    host: str = "127.0.0.1"
    port: int = 7125
    api_key: Optional[str] = None
    scheme: str = "ws"
    path: str = "/websocket"


class MoonrakerWS:
    def __init__(self, conn: MoonrakerConn, debug: bool = False, auto_reconnect: bool = True):
        self.conn = conn
        self.debug = debug
        self.auto_reconnect = auto_reconnect

        self._wsapp: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

        self._id = 1
        self._id_lock = threading.Lock()

        self._pending: Dict[int, tuple[threading.Event, Dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        self._status_cb: Optional[StatusUpdateCb] = None

    def set_status_callback(self, cb: Optional[StatusUpdateCb]) -> None:
        self._status_cb = cb

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _tag(self) -> str:
        return f"[moonraker {self.conn.host}:{self.conn.port}]"

    def _dlog(self, msg: str) -> None:
        if self.debug:
            print(f"{self._tag()} {msg}", flush=True)

    def _url(self) -> str:
        return f"{self.conn.scheme}://{self.conn.host}:{self.conn.port}{self.conn.path}"

    def _headers(self) -> list[str]:
        hdrs: list[str] = []
        if self.conn.api_key:
            hdrs.append(f"X-Api-Key: {self.conn.api_key}")
        return hdrs

    def _next_id(self) -> int:
        with self._id_lock:
            rid = self._id
            self._id += 1
            return rid

    def connect(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        print(f"{self._tag()} connect(): url={self._url()} headers={self._headers()}", flush=True)

        self._stop.clear()
        self._connected.clear()

        self._thread = threading.Thread(target=self._run, name="moonraker-ws", daemon=True)
        self._thread.start()

        if timeout > 0 and not self._connected.wait(timeout):
            raise TimeoutError(f"Moonraker websocket connect timeout ({timeout}s)")

    def close(self) -> None:
        print(f"{self._tag()} close(): stopping websocket thread", flush=True)
        self._stop.set()
        self._connected.clear()

        wsapp = self._wsapp
        if wsapp is not None:
            try:
                wsapp.close()
            except Exception:
                pass

        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)

        self._thread = None
        self._wsapp = None

        with self._pending_lock:
            for rid, (ev, box) in list(self._pending.items()):
                box["error"] = {"code": -1, "message": "Moonraker disconnected"}
                ev.set()
            self._pending.clear()

    # ---------------- JSON-RPC ----------------

    def _send(self, msg: Dict[str, Any]) -> None:
        raw = json.dumps(msg)
        self._dlog(f"TX {raw}")
        wsapp = self._wsapp
        if wsapp is None:
            return
        try:
            wsapp.send(raw)
        except Exception as e:
            self._dlog(f"send failed: {e!r}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Any:
        rid = self._next_id()
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            msg["params"] = params

        ev = threading.Event()
        box: Dict[str, Any] = {}

        with self._pending_lock:
            self._pending[rid] = (ev, box)

        self._send(msg)

        if not ev.wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"Moonraker call timeout: {method}")

        if "error" in box and box["error"] is not None:
            raise RuntimeError(f"Moonraker error for {method}: {box['error']}")
        return box.get("result")

    def send_gcode(self, script: str, timeout: float = 20.0) -> Any:
        return self.call("printer.gcode.script", {"script": script}, timeout=timeout)

    def objects_list(self) -> Dict[str, Any]:
        return self.call("printer.objects.list") or {}

    def objects_subscribe(self, objects: Dict[str, Any]) -> Dict[str, Any]:
        return self.call("printer.objects.subscribe", {"objects": objects}) or {}

    # ---------------- internals ----------------

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                url = self._url()
                headers = self._headers()
                self._dlog(f"run(): connecting url={url} headers={headers}")

                self._wsapp = websocket.WebSocketApp(
                    url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                # IMPORTANT: ping_interval must be > ping_timeout
                self._wsapp.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    reconnect=0,
                )
            except Exception as e:
                print(f"{self._tag()} run loop exception: {e!r}", flush=True)

            self._connected.clear()

            if not self.auto_reconnect or self._stop.is_set():
                break

            self._dlog(f"run(): reconnect in {backoff:.1f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 5.0)

    def _on_open(self, _ws) -> None:
        print(f"{self._tag()} connected", flush=True)
        self._connected.set()

    def _on_close(self, _ws, code, reason) -> None:
        print(f"{self._tag()} closed code={code} reason={reason}", flush=True)
        self._connected.clear()

    def _on_error(self, _ws, err) -> None:
        print(f"{self._tag()} error: {err!r}", flush=True)

    def _on_message(self, _ws, message: str) -> None:
        self._dlog(f"RX {message}")
        try:
            data = json.loads(message)
        except Exception:
            return

        # response
        if isinstance(data, dict) and "id" in data:
            rid = data.get("id")
            if not isinstance(rid, int):
                return
            with self._pending_lock:
                entry = self._pending.pop(rid, None)
            if entry is None:
                return
            ev, box = entry
            if data.get("error") is not None:
                box["error"] = data.get("error")
            else:
                box["result"] = data.get("result")
            ev.set()
            return

        # notify_status_update
        if isinstance(data, dict) and data.get("method") == "notify_status_update":
            params = data.get("params") or []
            if isinstance(params, list) and len(params) >= 2 and isinstance(params[0], dict):
                changes = params[0]
                try:
                    eventtime = float(params[1])
                except Exception:
                    eventtime = 0.0
                cb = self._status_cb
                if cb:
                    try:
                        cb(changes, eventtime)
                    except Exception:
                        pass
