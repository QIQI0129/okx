# -*- coding: utf-8 -*-
"""execution/portfolio.py (Final - bugfixed)

你遇到的两个核心问题：
1) positions 返回的是 dict，必须遍历 resp['data']，不能遍历 resp 本身。
2) 由于历史版本 Portfolio 构造参数顺序不一致，导致 self.ex/self.store/self.cfg 被错位。

本文件做了“防呆”：
- __init__ 支持两种调用方式：
  - Portfolio(ex, store, cfg)   (旧main)
  - Portfolio(cfg, ex, store)   (更直观的新main)
- 刷新账户/持仓时都做了严格类型检查与兜底。

输出字段（供 OrderManager 使用）：
- equity, avail_usdt
- pos_long, pos_short
- 兼容字段：has_position, pos_side, pos_sz
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger()


class Portfolio:
    def __init__(self, a, b, c):
        """支持两种参数顺序：
        - (ex, store, cfg)
        - (cfg, ex, store)
        """
        if isinstance(a, dict):
            self.cfg = a
            self.ex = b
            self.store = c
        elif isinstance(c, dict):
            self.ex = a
            self.store = b
            self.cfg = c
        else:
            raise TypeError(
                "Portfolio expects (ex, store, cfg) or (cfg, ex, store); got types: "
                f"{type(a)=}, {type(b)=}, {type(c)=}"
            )

        # Account
        self.equity: float = 0.0
        self.avail_usdt: float = 0.0

        # Positions in long_short_mode
        self.pos_long: float = 0.0
        self.pos_short: float = 0.0

        # Backward compatible fields
        self.has_position: bool = False
        self.pos_side: Optional[str] = None  # "long"/"short"/None
        self.pos_sz: float = 0.0

    # -----------------------------
    # Public APIs
    # -----------------------------

    def refresh(self) -> None:
        self._refresh_account()
        self._refresh_pos()

    def refresh_light(self) -> None:
        # 当前实现与 refresh 相同，预留以后做轻量刷新
        self.refresh()

    # -----------------------------
    # Private helpers
    # -----------------------------

    def _ws_fresh(self, max_age_sec: float = 5.0) -> bool:
        """私有WS如果在最近 max_age_sec 内有更新，则优先使用 store 里的快照。"""
        try:
            ts = self.store.get_kv_float("ws_private:last_uptime")
        except Exception:
            return False
        if ts is None:
            return False
        return (time.time() - float(ts)) <= float(max_age_sec)

    def _refresh_account(self) -> None:
        """刷新 equity / avail_usdt"""
        try:
            if self._ws_fresh():
                ws_eq = self.store.get_kv_float("ws:equity_usd")
                ws_av = self.store.get_kv_float("ws:avail_usdt")
                self.equity = float(ws_eq) if ws_eq is not None else float(self.ex.get_account_equity_usd() or 0.0)
                self.avail_usdt = float(ws_av) if ws_av is not None else float(self.ex.get_balance_usdt() or 0.0)
            else:
                self.equity = float(self.ex.get_account_equity_usd() or 0.0)
                self.avail_usdt = float(self.ex.get_balance_usdt() or 0.0)
        except Exception as e:
            log.warning("ACCOUNT REFRESH FAILED", extra={"err": str(e)})
            # 不要抛异常，主循环继续
            self.equity = float(self.equity or 0.0)
            self.avail_usdt = float(self.avail_usdt or 0.0)

    def _refresh_pos(self) -> None:
        """刷新持仓（long_short_mode: long/short 两条）"""
        inst_id = str((self.cfg.get("trade") or {}).get("inst_id") or "").strip()
        if not inst_id:
            self._set_pos(0.0, 0.0)
            return

        # WS 快照
        if self._ws_fresh():
            try:
                # 新版建议WS侧也写 long/short
                pl = self.store.get_kv_float("ws:pos_long")
                ps = self.store.get_kv_float("ws:pos_short")
                if pl is not None or ps is not None:
                    self._set_pos(float(pl or 0.0), float(ps or 0.0))
                    return

                # 兼容旧版只写单向
                has_pos = self.store.get_kv("ws:has_pos")
                side = self.store.get_kv("ws:pos_side")
                sz = float(self.store.get_kv_float("ws:pos_sz") or 0.0)
                if has_pos == "1" and side in ("long", "short"):
                    self._set_pos(sz if side == "long" else 0.0, sz if side == "short" else 0.0)
                    return
            except Exception:
                # WS 快照坏了就走 REST
                pass

        # REST 拉取
        try:
            resp = self.ex.get_positions(inst_id)
        except Exception as e:
            log.warning("POSITIONS REFRESH FAILED", extra={"err": str(e)})
            self._set_pos(0.0, 0.0)
            return

        pos_list = []
        if isinstance(resp, dict):
            pos_list = resp.get("data") or []
        elif isinstance(resp, list):
            # 极端兼容：如果有人把 get_positions 写成直接返回 list
            pos_list = resp

        # 兼容：data 被错误序列化成字符串
        if isinstance(pos_list, str):
            try:
                pos_list = json.loads(pos_list)
            except Exception:
                pos_list = []

        if not isinstance(pos_list, list):
            pos_list = []

        long_sz = 0.0
        short_sz = 0.0

        for p in pos_list:
            if not isinstance(p, dict):
                continue

            try:
                sz = float(p.get("pos", "0") or "0")
            except Exception:
                sz = 0.0

            side = str(p.get("posSide") or "").lower().strip()
            if side == "long":
                long_sz += sz
            elif side == "short":
                short_sz += sz

        self._set_pos(long_sz, short_sz)

    def _set_pos(self, long_sz: float, short_sz: float) -> None:
        self.pos_long = float(long_sz or 0.0)
        self.pos_short = float(short_sz or 0.0)

        self.has_position = (self.pos_long > 0) or (self.pos_short > 0)
        # 兼容单向字段：若同时有多空（理论上 max_positions=1 会禁止），这里优先标记净值更大的那边
        if self.pos_long > 0 and self.pos_short <= 0:
            self.pos_side = "long"
            self.pos_sz = self.pos_long
        elif self.pos_short > 0 and self.pos_long <= 0:
            self.pos_side = "short"
            self.pos_sz = self.pos_short
        elif self.pos_long == 0 and self.pos_short == 0:
            self.pos_side = None
            self.pos_sz = 0.0
        else:
            # 同时存在（不应该），做一个稳定选择
            if self.pos_long >= self.pos_short:
                self.pos_side = "long"
                self.pos_sz = self.pos_long
            else:
                self.pos_side = "short"
                self.pos_sz = self.pos_short
