# -*- coding: utf-8 -*-
"""
OKX Public WebSocket
- candle subscription
- auto reconnect
- ping / pong
- HTTP proxy support (for Windows + VPN/Clash)
"""

import json
import os
import threading
import time
from typing import Callable, Optional

import websocket

from utils.logger import get_logger
from utils.proxy import parse_http_proxy

log = get_logger()


class OKXPublicWS:
    def __init__(
        self,
        url: str,
        inst_id: str,
        bar: str,
        on_candle: Callable[[list], None],
        ping_interval: int = 15,
        reconnect_delay: int = 3,
        proxy_url: str = "",
    ):
        self.url = url
        self.inst_id = inst_id
        self.bar = bar
        self.on_candle = on_candle

        self.ping_interval = ping_interval
        self.reconnect_delay = reconnect_delay

        # HTTP proxy（WS 只推荐这个）
        self.proxy_url = (proxy_url or os.getenv("PROXY_URL", "")).strip()

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None

        self._stop = threading.Event()
        self._connected = threading.Event()

    # ---------------- public ----------------

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ---------------- internal ----------------

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._connected.clear()

                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                # === WS run_forever（代理/非代理） ===
                ph = parse_http_proxy(self.proxy_url)
                if ph:
                    host, port, auth = ph
                    kwargs = {
                        "http_proxy_host": host,
                        "http_proxy_port": port,
                    }
                    # websocket-client 只支持 proxy_type: http/socks4/socks5；这里强制 http
                    kwargs["proxy_type"] = "http"

                    if auth:
                        kwargs["http_proxy_auth"] = auth

                    log.info(
                        "Public WS using HTTP proxy",
                        extra={"host": host, "port": port},
                    )

                    self._ws.run_forever(
                        ping_interval=None,
                        ping_timeout=None,
                        reconnect=0,
                        **kwargs,
                    )
                else:
                    self._ws.run_forever(
                        ping_interval=None,
                        ping_timeout=None,
                        reconnect=0,
                    )

            except Exception as e:
                log.exception("Public WS loop exception", extra={"err": str(e)})

            if not self._stop.is_set():
                time.sleep(self.reconnect_delay)

    # ---------------- callbacks ----------------

    def _on_open(self, ws):
        self._connected.set()
        log.info("Public WS connected", extra={"inst": self.inst_id})

        channel = f"candle{self.bar}"
        sub = {
            "op": "subscribe",
            "args": [{"channel": channel, "instId": self.inst_id}],
        }
        ws.send(json.dumps(sub))

        # start ping thread
        if not self._ping_thread or not self._ping_thread.is_alive():
            self._ping_thread = threading.Thread(
                target=self._ping_loop, daemon=True
            )
            self._ping_thread.start()

    def _on_close(self, ws, code, msg):
        self._connected.clear()
        log.warning(
            "Public WS closed", extra={"code": code, "msg": str(msg)}
        )

    def _on_error(self, ws, err):
        self._connected.clear()
        log.error("Public WS error", extra={"err": str(err)})

    def _ping_loop(self):
        while not self._stop.is_set():
            if self._ws and self.is_connected():
                try:
                    self._ws.send("ping")
                except Exception:
                    pass
            time.sleep(self.ping_interval)

    def _on_message(self, ws, message: str):
        if message == "pong":
            return

        try:
            obj = json.loads(message)
        except Exception:
            return

        # subscribe ack / error
        if isinstance(obj, dict) and obj.get("event"):
            if obj.get("event") == "subscribe":
                log.info("Public WS subscribed", extra=obj.get("arg"))
            elif obj.get("event") == "error":
                log.error("Public WS subscribe error", extra=obj)
            return

        # candle data
        if isinstance(obj, dict) and "data" in obj:
            data = obj.get("data") or []
            for row in data:
                try:
                    self.on_candle(row)
                except Exception as e:
                    log.warning(
                        "on_candle failed", extra={"err": str(e)}
                    )
