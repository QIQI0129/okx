# -*- coding: utf-8 -*-
"""
OrderManager (stable)

Stability upgrades:
- Pre-trade margin/balance gate: avoid repeated 51008 spam
- Place order errors are caught; never crash main loop
- Pending lifecycle is robust:
  - timeout -> query with get_order_anywhere()
  - not found -> mark done & cleanup (avoid infinite timeout loop)
- After fill -> place TP/SL algo with idempotency guard
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple

from utils.logger import get_logger

log = get_logger()


def make_cl_ord_id(idempotency_key: str) -> str:
    h = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:20]
    suffix = str(int(time.time() * 1000) % 100000).zfill(5)
    return f"Q{h}{suffix}"


class OrderManager:
    def __init__(self, ex, store, cfg: dict, portfolio, risk):
        self.ex = ex
        self.store = store
        self.cfg = cfg or {}
        self.portfolio = portfolio
        self.risk = risk

    # 兼容旧调用名（避免 main.py/strategy 入口不一致）
    def handle_signal(self, signal: Any, bar: Dict[str, Any]) -> None:
        self.on_signal(signal, bar)

    # -----------------------------
    # KV keys
    # -----------------------------
    def _pending_key(self, idem: str) -> str:
        return f"pending:{idem}:clOrdId"

    def _pending_ts_key(self, idem: str) -> str:
        return f"pending:{idem}:ts"

    def _done_key(self, idem: str) -> str:
        return f"done:{idem}"

    def _reject_ts_key(self) -> str:
        return "last_reject_ts"

    # -----------------------------
    # Signal entry
    # -----------------------------
    def on_signal(self, signal: Any, bar: Dict[str, Any]) -> None:
        action = getattr(signal, "action", "") or ""
        idem = getattr(signal, "idempotency_key", "") or ""
        reason = getattr(signal, "reason", "") or ""

        if not action.startswith("OPEN"):
            return
        if not idem:
            return

        # 已 done 不再处理
        if self.store.get_kv(self._done_key(idem)) == "1":
            return

        # 已 pending 不再重复下单
        if self.store.get_kv(self._pending_key(idem)):
            return

        # reject 冷却：避免余额不足/权限不足的错误无限刷
        reject_cd = float((self.cfg.get("trade") or {}).get("reject_cooldown_sec", 15) or 15)
        last_reject_ts = float(self.store.get_kv_float(self._reject_ts_key()) or 0.0)
        if reject_cd > 0 and (time.time() - last_reject_ts) < reject_cd:
            return

        # 冷却（正常下单间隔）
        cooldown = float((self.cfg.get("trade") or {}).get("cooldown_sec", 0) or 0)
        last_trade_ts = float(self.store.get_kv_float("last_trade_ts") or 0.0)
        if cooldown > 0 and (time.time() - last_trade_ts) < cooldown:
            return

        inst_id = str((self.cfg.get("trade") or {}).get("inst_id", "") or "").strip()
        td_mode = str((self.cfg.get("account") or {}).get("td_mode", "isolated") or "isolated").strip()
        if not inst_id:
            return

        entry_px = self._bar_close(bar)
        if entry_px <= 0:
            return

        # 方向
        if action == "OPEN_LONG":
            pos_side = "long"
            side = "buy"
        elif action == "OPEN_SHORT":
            pos_side = "short"
            side = "sell"
        else:
            return

        # max_positions=1 语义：禁止同时持有反向仓
        pos_long = float(getattr(self.portfolio, "pos_long", 0.0) or 0.0)
        pos_short = float(getattr(self.portfolio, "pos_short", 0.0) or 0.0)
        max_pos = int((self.cfg.get("trade") or {}).get("max_positions", 1) or 1)

        if max_pos <= 1:
            if action == "OPEN_LONG" and pos_short > 0:
                log.warning("BLOCK OPEN_LONG: short position exists", extra={"pos_short": pos_short})
                return
            if action == "OPEN_SHORT" and pos_long > 0:
                log.warning("BLOCK OPEN_SHORT: long position exists", extra={"pos_long": pos_long})
                return

        # 计算下单张数
        try:
            sz = float(self.ex.calc_size_by_risk(entry_px) or 0.0)
        except Exception as e:
            log.error("SIZE CALC FAILED", extra={"err": str(e)})
            return

        if sz <= 0:
            return

        # 余额/保证金门禁（避免反复 51008）
        if not self._margin_gate(inst_id=inst_id, last_px=entry_px, sz=sz):
            # 标记 reject 冷却，避免狂刷
            self.store.set_kv(self._reject_ts_key(), str(time.time()))
            # 同一个 idem 可标 done，避免同一根信号无限尝试（你也可以改成不 done 让它下根信号重试）
            self.store.set_kv(self._done_key(idem), "1")
            return

        # TP/SL 方向修复：用 posSide 映射
        calc_side = "buy" if pos_side == "long" else "sell"
        tp, sl = self.ex.calc_tp_sl(entry_px, side=calc_side)

        cl_ord_id = make_cl_ord_id(idem)

        log.info(
            "PLACE ORDER SUBMIT",
            extra={
                "signal": action,
                "inst": inst_id,
                "entry": entry_px,
                "sz": sz,
                "tp": tp,
                "sl": sl,
                "clOrdId": cl_ord_id,
                "reason": reason,
            },
        )

        # 写入 pending
        self.store.set_kv("pending_current_idem", idem)
        self.store.set_kv(self._pending_key(idem), cl_ord_id)
        self.store.set_kv(self._pending_ts_key(idem), str(time.time()))

        # 下单：必须 catch，不能把主循环炸掉
        try:
            resp = self.ex.place_market_with_tp_sl(
                inst_id=inst_id,
                td_mode=td_mode,
                side=side,
                pos_side=pos_side,
                sz=sz,
                last_px=entry_px,
                cl_ord_id=cl_ord_id,
            )
        except Exception as e:
            log.error("PLACE ORDER FAILED", extra={"err": str(e), "clOrdId": cl_ord_id})
            # 清理 pending，防止 timeout 死循环
            self._cleanup_pending(idem)
            # 记录 reject 冷却
            self.store.set_kv(self._reject_ts_key(), str(time.time()))
            # 该信号标 done，避免同一 idem 无限重试
            self.store.set_kv(self._done_key(idem), "1")
            return

        # 入库（可选）
        try:
            self.store.save_order(
                inst_id=inst_id,
                side=side,
                pos_side=pos_side,
                sz=str(sz),
                tp_trigger=tp,
                sl_trigger=sl,
                resp_json=resp,
                note=reason,
                cl_ord_id=cl_ord_id,
            )
        except Exception as e:
            log.warning("SAVE ORDER FAILED", extra={"err": str(e)})

        self.store.set_kv("last_trade_ts", str(time.time()))

    # -----------------------------
    # Housekeeping
    # -----------------------------
    def housekeep(self) -> None:
        idem = self.store.get_kv("pending_current_idem")
        if not idem:
            return

        if self.store.get_kv(self._done_key(idem)) == "1":
            self._cleanup_pending(idem)
            return

        cl = self.store.get_kv(self._pending_key(idem))
        if not cl:
            self._cleanup_pending(idem)
            return

        ts = float(self.store.get_kv_float(self._pending_ts_key(idem)) or 0.0)
        if ts <= 0:
            return

        timeout_sec = float((self.cfg.get("trade") or {}).get("order_timeout_sec", 60) or 60)
        if (time.time() - ts) < timeout_sec:
            return

        inst_id = str((self.cfg.get("trade") or {}).get("inst_id", "") or "").strip()
        td_mode = str((self.cfg.get("account") or {}).get("td_mode", "isolated") or "isolated").strip()

        log.warning("ORDER TIMEOUT CHECK", extra={"idem": idem, "clOrdId": cl, "timeoutSec": timeout_sec})

        # timeout 查单：必须用 get_order_anywhere（51603/历史翻页兜底）
        try:
            od = self.ex.get_order_anywhere(inst_id=inst_id, cl_ord_id=cl)
            info = (od.get("data") or [{}])[0]
            state = (info.get("state") or "").lower()
            acc_fill = float(info.get("accFillSz") or 0.0)
        except Exception as e:
            # 查不到订单：说明下单根本没成功 or 数据已经不可查（极少）
            # 为了稳定：直接 done + 清理 pending，避免无限 timeout 刷屏
            log.error("ORDER QUERY FAILED", extra={"clOrdId": cl, "err": str(e)})
            self.store.set_kv(self._done_key(idem), "1")
            self._cleanup_pending(idem)
            return

        log.warning("ORDER STATUS ON TIMEOUT", extra={"clOrdId": cl, "state": state, "accFillSz": acc_fill})

        # 终态处理
        if state in ("filled", "canceled"):
            if state == "filled":
                self._after_fill_set_tp_sl(
                    idem=idem,
                    inst_id=inst_id,
                    td_mode=td_mode,
                    cl_ord_id=cl,
                    info=info,
                )
            self.store.set_kv(self._done_key(idem), "1")
            self._cleanup_pending(idem)
            log.warning("ORDER DONE ON QUERY", extra={"clOrdId": cl, "state": state})
            return

        # 部分成交：撤剩余 + done
        if state == "partially_filled":
            try:
                self.ex.cancel_order(inst_id=inst_id, cl_ord_id=cl)
            except Exception:
                pass

            self._after_fill_set_tp_sl(
                idem=idem,
                inst_id=inst_id,
                td_mode=td_mode,
                cl_ord_id=cl,
                info=info,
                force_sz=acc_fill,
            )

            self.store.set_kv(self._done_key(idem), "1")
            self._cleanup_pending(idem)
            log.warning("PARTIAL FILLED - MARK DONE", extra={"clOrdId": cl, "accFillSz": acc_fill})
            return

        # 仍然 live：按配置撤单
        cancel_on_timeout = bool((self.cfg.get("trade") or {}).get("cancel_on_timeout", True))
        if cancel_on_timeout:
            try:
                self.ex.cancel_order(inst_id=inst_id, cl_ord_id=cl)
                log.warning("ORDER CANCELED ON TIMEOUT", extra={"clOrdId": cl})
            except Exception as e:
                log.error("CANCEL FAILED", extra={"clOrdId": cl, "err": str(e)})

    # -----------------------------
    # TP/SL after fill
    # -----------------------------
    def _after_fill_set_tp_sl(
        self,
        idem: str,
        inst_id: str,
        td_mode: str,
        cl_ord_id: str,
        info: Dict[str, Any],
        force_sz: Optional[float] = None,
    ) -> None:
        # 幂等：避免重复挂
        if self.store.get_kv(f"tp_sl_set:{idem}") == "1":
            return

        try:
            avg_px = float(info.get("avgPx") or 0.0)
            if avg_px <= 0:
                avg_px = float(info.get("lastPx") or info.get("px") or 0.0)

            acc_fill = float(info.get("accFillSz") or 0.0)
            sz = float(force_sz if force_sz is not None else acc_fill)
            if sz <= 0 or avg_px <= 0:
                return

            pos_side = (info.get("posSide") or "").lower().strip()
            if pos_side not in ("long", "short"):
                open_side = (info.get("side") or "").lower()
                pos_side = "long" if open_side == "buy" else "short"

            calc_side = "buy" if pos_side == "long" else "sell"
            tp_px, sl_px = self.ex.calc_tp_sl(avg_px, side=calc_side)

            close_side = "sell" if pos_side == "long" else "buy"
            algo_cl = f"TPSL_{cl_ord_id}"

            self.ex.place_tp_sl_algo(
                inst_id=inst_id,
                td_mode=td_mode,
                close_side=close_side,
                sz=sz,
                tp_trigger=tp_px,
                sl_trigger=sl_px,
                pos_side=pos_side,
                cl_ord_id=algo_cl,
            )

            self.store.set_kv(f"tp_sl_set:{idem}", "1")
            log.warning(
                "TP/SL SET AFTER FILL",
                extra={
                    "clOrdId": cl_ord_id,
                    "algoClOrdId": algo_cl,
                    "avgPx": avg_px,
                    "sz": sz,
                    "posSide": pos_side,
                    "tp": tp_px,
                    "sl": sl_px,
                },
            )
        except Exception as e:
            log.error("SET TP/SL FAILED", extra={"clOrdId": cl_ord_id, "err": str(e)})

    # -----------------------------
    # Margin gate
    # -----------------------------
    def _margin_gate(self, inst_id: str, last_px: float, sz: float) -> bool:
        """
        目的：下单前拦截余额/保证金不足，避免反复 51008。
        简化估算：
          notional ≈ last_px * ctVal * sz   (线性合约)
          margin  ≈ notional / leverage
        若拿不到 ctVal，就退化为“检查 avail_usdt > 0”。
        """
        # 先拿可用 USDT（portfolio.refresh 会更新）
        avail = float(getattr(self.portfolio, "avail_usdt", 0.0) or 0.0)
        if avail <= 0:
            log.warning("BLOCK ORDER: avail_usdt <= 0", extra={"avail_usdt": avail})
            return False

        lev = float((self.cfg.get("account") or {}).get("leverage", 1) or 1)
        if lev <= 0:
            lev = 1.0

        ct_val = None
        try:
            # 使用 OKXRest 内部 spec cache（若存在）
            if hasattr(self.ex, "_must_spec"):
                spec = self.ex._must_spec(inst_id)
                ct_val = float(getattr(spec, "ct_val", 0.0) or 0.0)
        except Exception:
            ct_val = None

        if not ct_val or ct_val <= 0:
            # 拿不到合约规格，只做最低限度拦截
            min_avail = float((self.cfg.get("trade") or {}).get("min_avail_usdt", 5) or 5)
            if avail < min_avail:
                log.warning("BLOCK ORDER: low avail_usdt", extra={"avail_usdt": avail, "min_avail_usdt": min_avail})
                return False
            return True

        notional = float(last_px) * float(ct_val) * float(sz)
        req_margin = notional / lev

        # 留一点 buffer，避免手续费/浮动
        buffer_ratio = float((self.cfg.get("trade") or {}).get("margin_buffer_ratio", 0.95) or 0.95)

        if req_margin > avail * buffer_ratio:
            log.warning(
                "BLOCK ORDER: insufficient margin",
                extra={
                    "avail_usdt": avail,
                    "est_notional": notional,
                    "est_req_margin": req_margin,
                    "leverage": lev,
                    "buffer_ratio": buffer_ratio,
                },
            )
            return False

        return True

    # -----------------------------
    # Cleanup + utils
    # -----------------------------
    def _cleanup_pending(self, idem: str) -> None:
        try:
            self.store.del_kv("pending_current_idem")
        except Exception:
            pass
        try:
            self.store.del_kv(self._pending_key(idem))
        except Exception:
            pass
        try:
            self.store.del_kv(self._pending_ts_key(idem))
        except Exception:
            pass

    def _bar_close(self, bar: Dict[str, Any]) -> float:
        if not isinstance(bar, dict):
            return 0.0
        for k in ("close", "c", "last", "px"):
            if k in bar and bar[k] is not None:
                try:
                    return float(bar[k])
                except Exception:
                    continue
        return 0.0
