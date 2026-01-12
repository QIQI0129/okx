# -*- coding: utf-8 -*-
"""
OKX Private WebSocket (demo/prod stable)

Fixes your current pitfall:
- If base_url_demo is https://www.okx.com (global), demo WS MUST use wspap.okx.com
- If base_url_demo is https://eea.okx.com, demo WS uses wseeapap.okx.com
- If base_url_demo is https://us.okx.com, demo WS uses wsuspap.okx.com

Other stability features:
- Force websocket-client proxy_type="http"/"socks4"/"socks5"
- Login failure guard: after N failures -> disable private ws (no infinite reconnect spam)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any, Dict, Optional, Callable
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import websocket

from utils.logger import get_logger

log = get_logger()


def _parse_proxy(proxy_url: str) -> Optional[Dict[str, Any]]:
    if not proxy_url:
        return None
    u = urlparse(proxy_url.strip())
    host = u.hostname
    port = u.port
    if not host or not port:
        return None
    scheme = (u.scheme or "http").lower().strip()
    if scheme == "https":
        scheme = "http"
    if scheme not in ("http", "socks4", "socks5"):
        scheme = "http"
    return {"type": scheme, "host": host, "port": int(port)}


def _ensure_demo_broker_id(ws_url: str) -> str:
    """
    OKX demo WS often uses ?brokerId=9999
    If missing, append it.
    """
    if not ws_url:
        return ws_url
    u = urlparse(ws_url)
    qs = parse_qs(u.query or "")
    if "brokerId" not in qs:
        qs["brokerId"] = ["9999"]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        u = u._replace(query=new_query)
        return urlunparse(u)
    return ws_url


def _infer_demo_ws_host_from_base_url(base_url_demo: str) -> str:
    """
    Infer demo WS host from REST base url domain.
    """
    h = (urlparse(base_url_demo or "").hostname or "").lower()
    if "eea." in h:
        return "wseeapap.okx.com"
    if "us." in h:
        return "wsuspap.okx.com"
    # default global
    return "wspap.okx.com"


def _infer_prod_ws_host_from_base_url(base_url_prod: str) -> str:
    h = (urlparse(base_url_prod or "").hostname or "").lower()
    if "eea." in h:
        return "wseea.okx.com"
    if "us." in h:
        return "wsus.okx.com"
    return "ws.okx.com"


class OKXPrivateWS:
    def __init__(self, cfg: dict, store, on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.cfg = cfg or {}
        self.store = store
        self.on_event = on_event

        env = self.cfg.get("env") or {}
        self.demo = bool(env.get("demo", False))

        base_url_demo = str(env.get("base_url_demo", "") or "").strip()
        base_url_prod = str(env.get("base_url_prod", "") or "").strip()

        # Choose endpoint (prefer config; else infer from base_url_*)
        if self.demo:
            url = str(env.get("ws_private_demo", "") or "").strip()
            if not url:
                host = _infer_demo_ws_host_from_base_url(base_url_demo or "https://www.okx.com")
                url = f"wss://{host}:8443/ws/v5/private"
            url = _ensure_demo_broker_id(url)
        else:
            url = str(env.get("ws_private_prod", "") or "").strip()
            if not url:
                host = _infer_prod_ws_host_from_base_url(base_url_prod or "https://www.okx.com")
                url = f"wss://{host}:8443/ws/v5/private"

        self.url = url

        # Auth
        auth = self.cfg.get("auth") or {}
        self.api_key = str(auth.get("api_key", "")).strip()
        self.api_secret = str(auth.get("api_secret", "")).strip()
        self.passphrase = str(auth.get("passphrase", "")).strip()

        # Proxy
        p_cfg = self.cfg.get("proxy") or {}
        self.proxy_enabled = bool(p_cfg.get("enabled", False))
        self.proxy_url = str(p_cfg.get("url", "")).strip() if self.proxy_enabled else ""

        # Reconnect & ping
        self.ping_interval = int(env.get("ws_ping_interval_sec", 15) or 15)
        self.reconnect_delay = int(env.get("ws_reconnect_delay_sec", 3) or 3)

        # Failure guard
        self.max_login_failures = int(env.get("ws_private_max_login_failures", 3) or 3)
        self._login_failures = 0
        self.disabled = False
        self.last_error: str = ""

        self._ws: Optional[websocket.WebSocketApp] = None
        self._th: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._authed = False
        self._subscribed = False

    def start(self) -> None:
        if self.disabled:
            log.warning("Private WS disabled, skip start", extra={"reason": self.last_error or "disabled"})
            return
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._run_loop, name="OKXPrivateWS", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def is_ready(self) -> bool:
        return bool(self._connected and self._authed and not self.disabled)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self.disabled:
                return
            try:
                self._connect_once()
            except Exception as e:
                self.last_error = str(e)
                log.error("Private WS connect loop error", extra={"err": self.last_error})

            if self._stop.is_set() or self.disabled:
                return
            time.sleep(self.reconnect_delay)

    def _connect_once(self) -> None:
        self._connected = False
        self._authed = False
        self._subscribed = False

        def _on_open(ws):
            self._connected = True
            log.info("Private WS connected", extra={"url": self.url})
            self._login()

        def _on_message(ws, message: str):
            self._handle_message(message)

        def _on_error(ws, err):
            self.last_error = str(err)
            log.error("Private WS error", extra={"err": self.last_error})

        def _on_close(ws, code, msg):
            log.warning("Private WS closed", extra={"code": code, "msg": msg})
            self._connected = False
            self._authed = False

        self._ws = websocket.WebSocketApp(
            self.url,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        run_kwargs = {"ping_interval": self.ping_interval, "ping_timeout": 10}

        if self.proxy_enabled and self.proxy_url:
            ph = _parse_proxy(self.proxy_url)
            if ph:
                log.info("Private WS using HTTP proxy", extra={"host": ph["host"], "port": ph["port"], "type": ph["type"]})
                run_kwargs.update({
                    "http_proxy_host": ph["host"],
                    "http_proxy_port": ph["port"],
                    "proxy_type": ph["type"],
                })

        self._ws.run_forever(**run_kwargs)

    def _login(self) -> None:
        if not self._ws:
            return
        if not (self.api_key and self.api_secret and self.passphrase):
            self._disable("Missing API credentials (auth.api_key/api_secret/passphrase)")
            return

        # OKX WS login prehash: f"{ts}GET/users/self/verify"
        ts = str(int(time.time()))
        prehash = f"{ts}GET/users/self/verify"
        mac = hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        sign = base64.b64encode(mac.digest()).decode("utf-8")

        payload = {
            "op": "login",
            "args": [{
                "apiKey": self.api_key,
                "passphrase": self.passphrase,
                "timestamp": ts,
                "sign": sign
            }]
        }
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            self.last_error = str(e)
            log.error("Private WS send login failed", extra={"err": self.last_error})

    def _subscribe_after_login(self) -> None:
        if not self._ws or self._subscribed:
            return

        subs = [
            {"channel": "account"},
            {"channel": "positions", "instType": "SWAP"},
            {"channel": "orders", "instType": "SWAP"},
        ]
        for s in subs:
            payload = {"op": "subscribe", "args": [s]}
            try:
                self._ws.send(json.dumps(payload))
                log.info("Private WS subscribed", extra=s)
            except Exception as e:
                log.error("Private WS subscribe send failed", extra={"err": str(e), "sub": s})

        self._subscribed = True

    def _handle_message(self, message: str) -> None:
        try:
            msg = json.loads(message)
        except Exception:
            return

        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

        ev = str(msg.get("event") or "").lower()
        if ev == "login":
            code = str(msg.get("code") or "")
            if code == "0":
                self._authed = True
                self._login_failures = 0
                log.info("Private WS login ok", extra={})
                self._subscribe_after_login()
            else:
                self._login_failures += 1
                err_msg = str(msg.get("msg") or "")
                self.last_error = f"login failed code={code} msg={err_msg}"
                log.error("Private WS login failed", extra={"code": code, "msg": err_msg, "failures": self._login_failures})

                if self._login_failures >= self.max_login_failures:
                    self._disable(self.last_error)
                else:
                    try:
                        if self._ws:
                            self._ws.close()
                    except Exception:
                        pass

        elif ev == "error":
            code = str(msg.get("code") or "")
            err_msg = str(msg.get("msg") or "")
            self.last_error = f"event error code={code} msg={err_msg}"
            log.error("Private WS event error", extra={"event": "error", "msg": err_msg, "code": code, "connId": msg.get("connId")})

            # Treat key/env mismatch as failure
            if code in ("60031", "60032", "50119", "50101") or "API key" in err_msg:
                self._login_failures += 1

            if self._login_failures >= self.max_login_failures:
                self._disable(self.last_error)
            else:
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass

    def _disable(self, reason: str) -> None:
        self.disabled = True
        self.last_error = reason
        log.error("Private WS DISABLED", extra={"reason": reason, "max_failures": self.max_login_failures, "url": self.url})
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
