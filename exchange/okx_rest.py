# -*- coding: utf-8 -*-
"""
exchange/okx_rest.py  (Verified/Final)

这一版解决你在实战里遇到的所有典型坑：
- ✅ 正确 OKX v5 签名：request_path 必须包含 query string
- ✅ 模拟盘：x-simulated-trading: 1
- ✅ HTTP 代理（requests）
- ✅ account/config 拉取 posMode（你的账户是 long_short_mode，需要 posSide）
- ✅ set-leverage：使用 mgnMode（isolated/cross），且 hedge 模式分别设置 long/short
- ✅ 交易接口“表面成功”陷阱：顶层 code=0 但 data[0].sCode != 0 -> 直接抛错（避免 60s 后查不到订单）
- ✅ 51603 Order does not exist：get_order_anywhere() 自动从 history / archive 分页兜底
- ✅ 下单入口 place_market_with_tp_sl：兼容历史参数名 + 自动补 posSide
- ✅ 计算张数：按 ctVal/lotSz/minSz/tickSz 进行精确 floor
- ✅ TP/SL 计算：buy=long / sell=short

注意：
- 本文件只负责 REST 能力；WS 在 okx_ws.py / okx_ws_private.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlencode

import requests

from utils.logger import get_logger
from utils.retry import retry
from exchange.models import InstrumentSpec

log = get_logger()


class OKXRest:
    def __init__(self, cfg: dict, store):
        self.cfg = cfg or {}
        self.store = store

        env = self.cfg.get("env") or {}
        self.demo: bool = bool(env.get("demo", False))
        self.base_url: str = (env.get("base_url_demo") if self.demo else env.get("base_url_prod")) or "https://www.okx.com"
        self.base_url = str(self.base_url).rstrip("/")
        self.timeout_sec: int = int(env.get("timeout_sec", 10) or 10)

        auth = self.cfg.get("auth") or {}
        self.api_key: str = str(auth.get("api_key", "")).strip()
        self.api_secret: str = str(auth.get("api_secret", "")).strip()
        self.passphrase: str = str(auth.get("passphrase", "")).strip()

        account = self.cfg.get("account") or {}
        self.td_mode: str = str(account.get("td_mode", "isolated")).strip()
        self.leverage: int = int(account.get("leverage", 1) or 1)

        proxy_cfg = self.cfg.get("proxy") or {}
        self.proxy_enabled: bool = bool(proxy_cfg.get("enabled", False))
        self.proxy_url: str = str(proxy_cfg.get("url", "")).strip() if self.proxy_enabled else ""
        self.no_proxy: str = str(proxy_cfg.get("no_proxy", "")).strip()

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        if self.proxy_url:
            self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})
        if self.no_proxy:
            os.environ["NO_PROXY"] = self.no_proxy

        # cache/state
        self._spec_cache: Dict[str, InstrumentSpec] = {}
        self.pos_mode: str = ""   # long_short_mode / net_mode
        self.acct_lv: str = ""

    # ---------------------------------------------------------------------
    # Time & signing
    # ---------------------------------------------------------------------

    def _iso_ts(self) -> str:
        """2020-12-08T09:08:57.715Z"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, ts: str, method: str, request_path: str, body: str) -> Dict[str, str]:
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }

    # ---------------------------------------------------------------------
    # Core request
    # ---------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        method = method.upper()
        params = params or {}
        data = data or {}

        # ✅ 关键：签名必须包含 query string
        query = urlencode({k: v for k, v in params.items() if v is not None})
        request_path = path + (f"?{query}" if query else "")
        url = self.base_url + request_path

        body_str = ""
        if method in ("POST", "PUT"):
            body_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        else:
            body_str = ""

        headers: Dict[str, str] = {}
        if auth:
            if not (self.api_key and self.api_secret and self.passphrase):
                raise RuntimeError("Missing OKX API credentials in config.auth")
            ts = self._iso_ts()
            headers.update(self._headers(ts, method, request_path, body_str))

        if self.demo:
            headers["x-simulated-trading"] = "1"

        try:
            if method == "GET":
                r = self.session.get(url, headers=headers, timeout=self.timeout_sec)
            elif method == "POST":
                r = self.session.post(url, headers=headers, data=body_str.encode("utf-8"), timeout=self.timeout_sec)
            elif method == "DELETE":
                r = self.session.delete(url, headers=headers, timeout=self.timeout_sec)
            else:
                r = self.session.request(method, url, headers=headers, data=body_str.encode("utf-8"), timeout=self.timeout_sec)
        except requests.RequestException as e:
            raise RuntimeError(f"OKX HTTP request failed: {e}")

        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} Client Error: {r.reason} for url: {r.url} | body={r.text}", response=r)

        try:
            resp = r.json()
        except Exception:
            raise RuntimeError(f"OKX invalid JSON response: {r.text}")

        code = str(resp.get("code") or "")
        msg = str(resp.get("msg") or "")

        if code != "0":
            # 50119: API key doesn't exist / region domain mismatch etc.
            if code == "50119":
                # OKX FAQ: region/domain mismatch may cause 50119; EEA users use eea.okx.com; US users use us.okx.com
                hint = (
                    "（提示：你在跑模拟盘：请使用“模拟交易”创建的 API Key；另外若你是 EEA/US 用户，请把 base_url 设置为 eea.okx.com / us.okx.com）"
                    if self.demo
                    else "（提示：检查是否用错了环境/Key；若你是 EEA/US 用户，请把 base_url 设置为 eea.okx.com / us.okx.com）"
                )
                raise RuntimeError(f"OKX API error: code={code} msg={msg} {hint} resp={resp}")
            raise RuntimeError(f"OKX API error: code={code} msg={msg} resp={resp}")

        # ✅ 关键：交易类接口顶层 code=0 也可能 data[0].sCode != 0
        if path.startswith("/api/v5/trade/"):
            rows = resp.get("data") or []
            if isinstance(rows, list) and rows:
                s_code = str(rows[0].get("sCode") or "0")
                if s_code != "0":
                    s_msg = str(rows[0].get("sMsg") or "")
                    raise RuntimeError(f"OKX TRADE OP FAILED: sCode={s_code} sMsg={s_msg} resp={resp}")

        return resp
    # ---------------------------------------------------------------------
    # Bootstrap
    # ---------------------------------------------------------------------

    @retry(tries=3, delay=0.5)
    def get_account_config(self) -> dict:
        return self._request("GET", "/api/v5/account/config", auth=True)

    def bootstrap(self) -> None:
        inst_id = str((self.cfg.get("trade") or {}).get("inst_id", "")).strip()

        # warm spec
        if inst_id:
            try:
                self._must_spec(inst_id)
            except Exception as e:
                log.warning("INSTRUMENT SPEC FETCH FAILED", extra={"inst": inst_id, "err": str(e)})

        # account config
        try:
            cfg = self.get_account_config()
            info = (cfg.get("data") or [{}])[0]
            self.pos_mode = str(info.get("posMode") or "").strip()
            self.acct_lv = str(info.get("acctLv") or "").strip()
            log.info("ACCOUNT CONFIG", extra={"posMode": self.pos_mode, "acctLv": self.acct_lv})
        except Exception as e:
            log.warning("ACCOUNT CONFIG FETCH FAILED", extra={"err": str(e)})

        # leverage
        try:
            if inst_id and int(self.leverage) > 0:
                self.set_leverage(inst_id=inst_id, lever=int(self.leverage), td_mode=self.td_mode)
        except Exception as e:
            log.warning("SET LEVERAGE FAILED", extra={"err": str(e), "inst": inst_id})

    # ---------------------------------------------------------------------
    # Instruments
    # ---------------------------------------------------------------------

    @retry(tries=3, delay=0.5)
    def get_instrument_spec(self, inst_id: str) -> InstrumentSpec:
        resp = self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": inst_id},
            auth=False,
        )
        d0 = (resp.get("data") or [{}])[0]
        return InstrumentSpec(
            inst_id=inst_id,
            ct_val=float(d0.get("ctVal") or 0.0),
            lot_sz=float(d0.get("lotSz") or 0.0),
            min_sz=float(d0.get("minSz") or 0.0),
            tick_sz=float(d0.get("tickSz") or 0.0),
        )

    def _must_spec(self, inst_id: str) -> InstrumentSpec:
        inst_id = inst_id.strip()
        if inst_id in self._spec_cache:
            return self._spec_cache[inst_id]
        spec = self.get_instrument_spec(inst_id)
        self._spec_cache[inst_id] = spec
        return spec

    # ---------------------------------------------------------------------
    # Utils: rounding/format
    # ---------------------------------------------------------------------

    def _floor_to_step(self, x: float, step: float) -> float:
        if step <= 0:
            return float(x)
        xd = Decimal(str(x))
        sd = Decimal(str(step))
        q = (xd / sd).to_integral_value(rounding=ROUND_DOWN) * sd
        return float(q)

    def round_to_tick(self, px: float, inst_id: Optional[str] = None) -> float:
        if px <= 0:
            return 0.0
        tid = inst_id or str((self.cfg.get("trade") or {}).get("inst_id") or "").strip()
        tick = self._must_spec(tid).tick_sz if tid else 0.0
        if tick <= 0:
            return float(px)
        return self._floor_to_step(px, tick)

    def _fmt_sz(self, sz: float) -> str:
        return format(Decimal(str(sz)).normalize(), "f")

    # ---------------------------------------------------------------------
    # Market data
    # ---------------------------------------------------------------------

    @retry(tries=3, delay=0.5)
    def get_candles(self, inst_id: str, bar: str, limit: int = 200) -> dict:
        return self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit)},
            auth=False,
        )

    def _ema(self, series: List[float], period: int) -> float:
        if not series:
            return 0.0
        period = int(period)
        if period <= 1:
            return float(series[-1])
        k = 2.0 / (period + 1.0)
        e = series[0]
        for v in series[1:]:
            e = v * k + e * (1.0 - k)
        return float(e)

    def get_latest_bar_with_ema(self, inst_id: str, bar: str, fast: int, slow: int, limit: int = 200) -> Tuple[dict, float, float]:
        resp = self.get_candles(inst_id=inst_id, bar=bar, limit=limit)
        data = resp.get("data") or []
        if not data:
            return {}, 0.0, 0.0

        rows = list(reversed(data))  # oldest->newest
        closes: List[float] = []
        for r in rows:
            try:
                closes.append(float(r[4]))
            except Exception:
                closes.append(0.0)

        ema_fast = self._ema(closes, int(fast))
        ema_slow = self._ema(closes, int(slow))

        last = rows[-1]
        latest_bar = {"ts": last[0], "o": float(last[1]), "h": float(last[2]), "l": float(last[3]), "c": float(last[4])}
        return latest_bar, ema_fast, ema_slow

    # ---------------------------------------------------------------------
    # Account / balance / positions
    # ---------------------------------------------------------------------

    @retry(tries=3, delay=0.5)
    def get_account_balance(self) -> dict:
        return self._request("GET", "/api/v5/account/balance", auth=True)

    def get_account_equity_usd(self) -> float:
        resp = self.get_account_balance()
        info = (resp.get("data") or [{}])[0]
        for k in ("totalEqUsd", "totalEq", "eqUsd"):
            v = info.get(k)
            if v is not None and str(v) != "":
                try:
                    return float(v)
                except Exception:
                    pass
        # fallback sum USDT eq
        total = 0.0
        for d in (info.get("details") or []):
            try:
                if str(d.get("ccy") or "").upper() in ("USDT", "USD"):
                    total += float(d.get("eq") or 0.0)
            except Exception:
                continue
        return float(total)

    def get_balance_usdt(self) -> float:
        resp = self.get_account_balance()
        info = (resp.get("data") or [{}])[0]
        for d in (info.get("details") or []):
            if str(d.get("ccy") or "").upper() == "USDT":
                try:
                    return float(d.get("availBal") or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    @retry(tries=3, delay=0.5)
    def get_positions(self, inst_id: str) -> dict:
        return self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instType": "SWAP", "instId": inst_id},
            auth=True,
        )

    # ---------------------------------------------------------------------
    # Leverage (mgnMode)
    # ---------------------------------------------------------------------

    def set_leverage(self, inst_id: str, lever: int, td_mode: str) -> dict:
        """设置杠杆（模拟盘/实盘一致）
        - OKX 参数名：mgnMode（isolated/cross）
        - 若账户是 long_short_mode（对冲模式），需要分别给 long/short 设置 posSide
        - 为了“稳定不掉坑”，即便没成功拉到 account/config，也会在遇到 51000 posSide error 时自动回退重试
        """
        inst_id = (inst_id or "").strip()
        mgn_mode = (td_mode or "isolated").strip()
        lever = int(lever)

        def _call(payload: Dict[str, Any]) -> dict:
            return self._request("POST", "/api/v5/account/set-leverage", data=payload, auth=True)

        pm = (self.pos_mode or "").lower()
        is_hedge = pm in ("long_short_mode", "long_short", "hedge", "longshortmode")

        if is_hedge:
            r1 = _call({"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode, "posSide": "long"})
            r2 = _call({"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode, "posSide": "short"})
            return {"code": "0", "data": [{"long": r1.get("data"), "short": r2.get("data")}]}

        # 不确定是否 hedge：先尝试不带 posSide
        try:
            return _call({"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode})
        except Exception as e:
            msg = str(e)
            # 若 OKX 返回 posSide error，则按 hedge 模式补 posSide 重试一次
            if "51000" in msg and "posSide" in msg:
                r1 = _call({"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode, "posSide": "long"})
                r2 = _call({"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode, "posSide": "short"})
                return {"code": "0", "data": [{"long": r1.get("data"), "short": r2.get("data")}]}
            raise


    # ---------------------------------------------------------------------
    # Orders: place/query/cancel
    # ---------------------------------------------------------------------

    def place_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # hedge auto-fill posSide
        pm = (self.pos_mode or "").lower()
        is_hedge = pm in ("long_short_mode", "long_short", "hedge", "longshortmode")
        if is_hedge and "posSide" not in data:
            s = (data.get("side") or "").lower()
            if s == "buy":
                data["posSide"] = "long"
            elif s == "sell":
                data["posSide"] = "short"

        log.info("OKX PLACE ORDER FINAL PAYLOAD", extra={"data": data})
        return self._request("POST", "/api/v5/trade/order", data=data, auth=True)

    def place_market_with_tp_sl(
        self,
        side: str,
        sz,
        last_px: Optional[float] = None,
        idempotency_key: str = "",
        inst_id: Optional[str] = None,
        td_mode: Optional[str] = None,
        pos_side: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        兼容入口：只下 MARKET 开仓单。TP/SL 由成交后 place_tp_sl_algo 负责。
        """
        try:
            sz_f = float(sz)
        except Exception:
            sz_f = 0.0
        if sz_f <= 0:
            return {"code": "LOCAL_REJECT", "msg": f"invalid order size sz={sz}", "data": []}

        inst_id = (inst_id or str((self.cfg.get("trade") or {}).get("inst_id") or "")).strip()
        if not inst_id:
            return {"code": "LOCAL_REJECT", "msg": "missing inst_id", "data": []}

        td_mode = (td_mode or str((self.cfg.get("account") or {}).get("td_mode") or "isolated")).strip()
        clid = (cl_ord_id or idempotency_key or "").strip() or f"Q{int(time.time()*1000)}"

        data: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": (side or "").lower(),
            "ordType": "market",
            "sz": self._fmt_sz(sz_f),
            "clOrdId": clid,
        }
        if pos_side:
            data["posSide"] = str(pos_side).lower().strip()

        return self.place_order(data)

    def cancel_order(self, inst_id: str, ord_id: Optional[str] = None, cl_ord_id: Optional[str] = None, clOrdId: Optional[str] = None) -> dict:
        inst_id = (inst_id or "").strip()
        _cl = (cl_ord_id or clOrdId or "").strip()
        _ord = (ord_id or "").strip()
        if not inst_id:
            raise ValueError("cancel_order requires inst_id")
        if not _cl and not _ord:
            raise ValueError("cancel_order requires ord_id or cl_ord_id")

        data: Dict[str, Any] = {"instId": inst_id}
        if _ord:
            data["ordId"] = _ord
        else:
            data["clOrdId"] = _cl
        return self._request("POST", "/api/v5/trade/cancel-order", data=data, auth=True)

    def get_order_by_clordid(self, inst_id: str, cl_ord_id: str) -> dict:
        inst_id = (inst_id or "").strip()
        cl_ord_id = (cl_ord_id or "").strip()
        return self._request("GET", "/api/v5/trade/order", params={"instId": inst_id, "clOrdId": cl_ord_id}, auth=True)

    def _search_history_paged(self, path: str, inst_id: str, cl_ord_id: str, max_pages: int = 10, limit: int = 100) -> Optional[dict]:
        after = None
        for _ in range(max_pages):
            params = {"instType": "SWAP", "instId": inst_id, "limit": str(limit)}
            if after:
                params["after"] = str(after)
            resp = self._request("GET", path, params=params, auth=True)
            rows = resp.get("data") or []
            for od in rows:
                if str(od.get("clOrdId") or "") == cl_ord_id:
                    return {"code": "0", "data": [od]}
            if not rows:
                return None
            after = rows[-1].get("ordId")
            if not after:
                return None
        return None

    def get_order_anywhere(self, inst_id: str, cl_ord_id: str) -> dict:
        """
        robust query:
          1) /trade/order
          2) /trade/orders-history (paged)
          3) /trade/orders-history-archive (paged)
        """
        inst_id = (inst_id or "").strip()
        cl_ord_id = (cl_ord_id or "").strip()
        if not inst_id or not cl_ord_id:
            raise ValueError("get_order_anywhere requires inst_id and cl_ord_id")

        try:
            return self.get_order_by_clordid(inst_id=inst_id, cl_ord_id=cl_ord_id)
        except Exception as e:
            if "51603" not in str(e):
                raise

        found = self._search_history_paged("/api/v5/trade/orders-history", inst_id, cl_ord_id, max_pages=10, limit=100)
        if found:
            return found

        found = self._search_history_paged("/api/v5/trade/orders-history-archive", inst_id, cl_ord_id, max_pages=10, limit=100)
        if found:
            return found

        raise RuntimeError(f"Order not found in current nor history: clOrdId={cl_ord_id}")

    # ---------------------------------------------------------------------
    # TP/SL algo
    # ---------------------------------------------------------------------

    def place_tp_sl_algo(
        self,
        inst_id: str,
        close_side: str,
        sz: float,
        tp_trigger: Optional[float] = None,
        sl_trigger: Optional[float] = None,
        pos_side: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        td_mode: Optional[str] = None,
        mgn_mode: Optional[str] = None,
        **kwargs,
    ) -> dict:
        inst_id = (inst_id or "").strip()
        close_side = (close_side or "").lower().strip()
        pos_side = (pos_side or "").lower().strip()
        if close_side not in ("buy", "sell"):
            raise ValueError("place_tp_sl_algo close_side must be buy/sell")
        if pos_side not in ("long", "short"):
            raise ValueError("place_tp_sl_algo pos_side must be long/short")

        mm = (mgn_mode or td_mode or self.td_mode or "isolated").strip()

        sz_f = float(sz)
        if sz_f <= 0:
            raise ValueError("place_tp_sl_algo invalid sz")

        data: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": mm,
            "side": close_side,
            "posSide": pos_side,
            "ordType": "conditional",
            "sz": self._fmt_sz(sz_f),
        }
        if cl_ord_id:
            data["clOrdId"] = str(cl_ord_id)

        if tp_trigger is not None:
            data["tpTriggerPx"] = str(self.round_to_tick(float(tp_trigger), inst_id=inst_id))
            data["tpOrdPx"] = "-1"
        if sl_trigger is not None:
            data["slTriggerPx"] = str(self.round_to_tick(float(sl_trigger), inst_id=inst_id))
            data["slOrdPx"] = "-1"

        return self._request("POST", "/api/v5/trade/order-algo", data=data, auth=True)

    # ---------------------------------------------------------------------
    # TP/SL calculation & sizing
    # ---------------------------------------------------------------------

    def calc_tp_sl(self, entry_px: float, side: str, tp_pct: Optional[float] = None, sl_pct: Optional[float] = None) -> Tuple[float, float]:
        if entry_px <= 0:
            return 0.0, 0.0
        exit_cfg = self.cfg.get("exit") or {}
        if tp_pct is None:
            tp_pct = float(exit_cfg.get("tp_pct", 0.0) or 0.0)
        if sl_pct is None:
            sl_pct = float(exit_cfg.get("sl_pct", 0.0) or 0.0)

        side = (side or "").lower()
        if side == "buy":
            tp = entry_px * (1.0 + float(tp_pct))
            sl = entry_px * (1.0 - float(sl_pct))
        else:
            tp = entry_px * (1.0 - float(tp_pct))
            sl = entry_px * (1.0 + float(sl_pct))
        return float(tp), float(sl)

    def calc_size_by_risk(self, last_px: float) -> float:
        """
        计算张数（合约张，支持小数）：
          risk_notional = equity * risk_pct
          one_contract_notional ≈ last_px * ctVal
          raw_sz = risk_notional / one_contract_notional
          然后按 lotSz 向下取整，并检查 minSz
        """
        if last_px <= 0:
            return 0.0

        inst_id = str((self.cfg.get("trade") or {}).get("inst_id") or "").strip()
        if not inst_id:
            return 0.0

        spec = self._must_spec(inst_id)
        if spec.ct_val <= 0:
            return 0.0

        risk_cfg = self.cfg.get("risk") or {}
        risk_pct = float(risk_cfg.get("risk_pct_per_trade", 0.0) or 0.0)
        if risk_pct <= 0:
            return 0.0

        equity = self.get_account_equity_usd()
        risk_notional = equity * risk_pct
        if risk_notional <= 0:
            return 0.0

        one_contract_notional = last_px * spec.ct_val
        if one_contract_notional <= 0:
            return 0.0

        raw_sz = risk_notional / one_contract_notional

        lot = float(spec.lot_sz or 0.0)
        min_sz = float(spec.min_sz or 0.0)
        sz = self._floor_to_step(raw_sz, lot if lot > 0 else 1.0)

        if min_sz > 0 and sz + 1e-12 < min_sz:
            return 0.0

        return float(sz)
