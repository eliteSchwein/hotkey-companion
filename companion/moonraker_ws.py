# moonraker_ws.py
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import websocket  # websocket-client


StatusUpdateCb = Callable[[Dict[str, Any], float], None]
NotifyCb = Callable[[str, Any], None]  # (method, params)


@dataclass
class MoonrakerConn:
    host: str = "127.0.0.1"
    port: int = 7125
    api_key: Optional[str] = None

    scheme: str = "ws"
    path: str = "/websocket"

    client_name: str = "hotkey-companion"
    version: str = "0.0.1"
    client_type: str = "agent"
    url: str = ""


class MoonrakerWS:
    """
    Moonraker JSON-RPC over websocket-client (threaded).

    - connect()/close()
    - call(method, params, timeout) -> result
    - notify(method, params) fire-and-forget
    - status callback: notify_status_update (diffs)
    - notify callback: any notify_* (klippy ready/shutdown/etc)
    """

    def __init__(self, conn: MoonrakerConn, debug: bool = False, auto_reconnect: bool = True):
        self.conn = conn
        self.debug = bool(debug)
        self.auto_reconnect = bool(auto_reconnect)

        self._wsapp: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None

        self._stop = threading.Event()
        self._connected = threading.Event()

        self._pending_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}

        self._status_cb: Optional[StatusUpdateCb] = None
        self._notify_cb: Optional[NotifyCb] = None

    def set_status_callback(self, cb: Optional[StatusUpdateCb]) -> None:
        self._status_cb = cb

    def set_notify_callback(self, cb: Optional[NotifyCb]) -> None:
        self._notify_cb = cb

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _tag(self) -> str:
        return f"[moonraker {self.conn.host}:{self.conn.port}]"

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"{self._tag()} {msg}", flush=True)

    def _url(self) -> str:
        path = self.conn.path if self.conn.path.startswith("/") else ("/" + self.conn.path)
        return f"{self.conn.scheme}://{self.conn.host}:{self.conn.port}{path}"

    def _headers(self) -> list[str]:
        hdrs: list[str] = []
        if self.conn.api_key:
            hdrs.append(f"X-Api-Key: {self.conn.api_key}")
        return hdrs

    def connect(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._connected.clear()

        self._log(f"connect(): target={self.conn.host}:{self.conn.port} url={self._url()}")
        self._thread = threading.Thread(target=self._run, name="moonraker-ws", daemon=True)
        self._thread.start()

        if not self._connected.wait(timeout):
            raise TimeoutError(
                f"Moonraker websocket connect timeout ({timeout}s) to {self.conn.host}:{self.conn.port}"
            )

    def close(self) -> None:
        self._log("close(): stopping websocket thread")
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
            for _rid, (ev, box) in list(self._pending.items()):
                box["error"] = {"code": -1, "message": "Moonraker disconnected"}
                ev.set()
            self._pending.clear()

    def _send(self, msg: Dict[str, Any]) -> None:
        raw = json.dumps(msg)
        self._log(f"TX {raw}")
        wsapp = self._wsapp
        if wsapp is None:
            return
        try:
            wsapp.send(raw)
        except Exception as e:
            self._log(f"send failed: {e!r}")

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Any:
        if not self.is_connected:
            raise RuntimeError("Moonraker not connected")

        rid = random.randint(1000, 2_000_000_000)
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

        if box.get("error") is not None:
            raise RuntimeError(f"Moonraker error for {method}: {box['error']}")
        return box.get("result")

    # helpers
    def server_info(self, timeout: float = 2.0) -> Dict[str, Any]:
        return self.call("server.info", timeout=timeout) or {}

    def objects_list(self, timeout: float = 5.0) -> Dict[str, Any]:
        return self.call("printer.objects.list", timeout=timeout) or {}

    def objects_subscribe(self, objects: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        return self.call("printer.objects.subscribe", {"objects": objects}, timeout=timeout) or {}

    def send_gcode(self, script: str, timeout: float = 20.0) -> Any:
        return self.call("printer.gcode.script", {"script": script}, timeout=timeout)

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                url = self._url()
                headers = self._headers()
                self._log(f"run(): connecting url={url} headers={headers}")

                self._wsapp = websocket.WebSocketApp(
                    url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                # websocket-client requires ping_interval > ping_timeout
                self._wsapp.run_forever(ping_interval=25, ping_timeout=20, reconnect=0)

            except Exception as e:
                self._log(f"run loop exception: {e!r}")

            self._connected.clear()

            if not self.auto_reconnect or self._stop.is_set():
                break

            self._log(f"run(): reconnect in {backoff:.1f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 5.0)

    def _on_open(self, _ws) -> None:
        self._log("connected")
        self._connected.set()

        # Fire-and-forget identify
        try:
            params: Dict[str, Any] = {
                "client_name": self.conn.client_name,
                "version": self.conn.version,
                "type": self.conn.client_type,
            }
            if self.conn.url:
                params["url"] = self.conn.url
            if self.conn.api_key:
                params["api_key"] = self.conn.api_key
            self.notify("server.connection.identify", params=params)
        except Exception:
            pass

    def _on_close(self, _ws, code, reason) -> None:
        self._log(f"closed code={code} reason={reason}")
        self._connected.clear()

    def _on_error(self, _ws, err) -> None:
        self._log(f"error: {err!r}")

    def _on_message(self, _ws, message: str) -> None:
        self._log(f"RX {message}")

        try:
            data = json.loads(message)
        except Exception:
            return

        # Response
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

        # Notification
        if isinstance(data, dict) and isinstance(data.get("method"), str):
            method = data["method"]
            params = data.get("params")

            if method == "notify_status_update":
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
                return

            cb2 = self._notify_cb
            if cb2:
                try:
                    cb2(method, params)
                except Exception:
                    pass
