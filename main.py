# -*- coding: utf-8 -*-
"""
main.py - OKX Quant Pro (SIM stable)

特点：
- Public WS（business）订阅 candle1m；行情获取优先 WS，失败 fallback REST
- Private WS 登录后订阅 account/positions/orders，并打印“订单状态更新摘要”
- 主循环稳定容错：任何异常不会让程序退出（除非 KeyboardInterrupt）
- 下单/订单超时/部分成交：由 OrderManager.housekeep() 处理
- Portfolio 定期 refresh（权益/余额/仓位）
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import yaml

from utils.logger import get_logger
from data.store import SQLiteStore
from exchange.okx_rest import OKXRest
from exchange.okx_ws import OKXPublicWS
from exchange.okx_ws_private import OKXPrivateWS

from execution.portfolio import Portfolio
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from datetime import datetime
from zoneinfo import ZoneInfo
log = get_logger()


SG_TZ = ZoneInfo("Asia/Singapore")

def sg_day_key(ts: float) -> str:
    d = datetime.fromtimestamp(ts, SG_TZ).strftime("%Y-%m-%d")
    return d

def ensure_daily_baseline(store, equity: float) -> float:
    """
    确保当日基准存在：
      - 新的一天：重置 baseline = 当前 equity
      - 同一天：沿用已有 baseline
    """
    today = sg_day_key(time.time())
    k_day = "pnl:baseline_day"
    k_eq = "pnl:baseline_equity"

    last_day = store.get_kv(k_day) or ""
    baseline = safe_float(store.get_kv(k_eq) or 0.0, 0.0)

    if (not last_day) or (last_day != today) or baseline <= 0:
        baseline = float(equity or 0.0)
        store.set_kv(k_day, today)
        store.set_kv(k_eq, str(baseline))
        log.warning("DAILY BASELINE RESET", extra={"day": today, "baseline_equity": baseline})

    return baseline


# -----------------------------
# Helpers
# -----------------------------
def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def now_ts() -> float:
    return time.time()


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return int(default)


def bar_close(bar: Dict[str, Any]) -> float:
    if not isinstance(bar, dict):
        return 0.0
    for k in ("close", "c", "last", "px"):
        if k in bar and bar[k] is not None:
            try:
                return float(bar[k])
            except Exception:
                pass
    return 0.0


def bar_ts_ms(bar: Dict[str, Any]) -> int:
    """
    兼容你 WS / REST bar 的不同字段命名
    - 常见：ts（毫秒）、t（毫秒）、timestamp（毫秒）
    """
    if not isinstance(bar, dict):
        return 0
    for k in ("ts", "t", "timestamp", "time"):
        if k in bar and bar[k] is not None:
            try:
                v = int(float(bar[k]))
                # 如果是秒级，转毫秒（粗略判断）
                if v < 10_000_000_000:
                    v *= 1000
                return v
            except Exception:
                pass
    return 0


@dataclass
class Signal:
    action: str
    idempotency_key: str
    reason: str


def make_idem(action: str, candle_ts_ms: int, ema_fast: float, ema_slow: float) -> str:
    # idem 尽量短且稳定：动作 + K线时间 + 两条 EMA
    return f"SIG_{action}_{candle_ts_ms}_{ema_fast:.4f}_{ema_slow:.4f}"


# -----------------------------
# WS event handlers
# -----------------------------
def make_private_ws_handler(cfg: dict, store: SQLiteStore):
    inst_cfg = str(((cfg.get("trade") or {}).get("inst_id")) or "").strip()

    def _sf(x) -> float:
        try:
            if x is None:
                return 0.0
            if isinstance(x, str) and x.strip() == "":
                return 0.0
            return float(x)
        except Exception:
            return 0.0

    def on_private_event(msg: dict):
        try:
            arg0 = (msg.get("arg") or {})
            ch = str(arg0.get("channel") or "")

            # ---- orders：打印订单状态摘要 ----
            if ch == "orders":
                data = msg.get("data") or []
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    o = data[0]
                    log.info("WS ORDER UPDATE", extra={
                        "instId": o.get("instId"),
                        "clOrdId": o.get("clOrdId"),
                        "ordId": o.get("ordId"),
                        "state": o.get("state"),
                        "side": o.get("side"),
                        "posSide": o.get("posSide"),
                        "avgPx": o.get("avgPx"),
                        "accFillSz": o.get("accFillSz"),
                    })
                return

            # ---- positions：写入实时仓位收益 & 仓位数量 ----
            if ch == "positions":
                long_sz = short_sz = 0.0
                long_upl = short_upl = 0.0
                long_upl_ratio = short_upl_ratio = 0.0

                data = msg.get("data") or []
                if isinstance(data, list):
                    for p in data:
                        if not isinstance(p, dict):
                            continue

                        inst_id = str(p.get("instId") or "").strip()
                        if inst_cfg and inst_id and inst_id != inst_cfg:
                            continue

                        pos_side = str(p.get("posSide") or "").lower().strip()  # long/short
                        pos = _sf(p.get("pos"))
                        upl = _sf(p.get("upl"))
                        upl_ratio = _sf(p.get("uplRatio"))

                        if pos_side == "long":
                            long_sz += abs(pos)
                            long_upl += upl
                            long_upl_ratio = upl_ratio
                        elif pos_side == "short":
                            short_sz += abs(pos)
                            short_upl += upl
                            short_upl_ratio = upl_ratio

                # KV：给 Portfolio/main 使用
                store.set_kv("ws:pos_long", str(long_sz))
                store.set_kv("ws:pos_short", str(short_sz))
                store.set_kv("ws:upl_long", str(long_upl))
                store.set_kv("ws:upl_short", str(short_upl))
                store.set_kv("ws:upl_ratio_long", str(long_upl_ratio))
                store.set_kv("ws:upl_ratio_short", str(short_upl_ratio))

                # 兼容旧逻辑（单向）
                has_pos = (long_sz > 0) or (short_sz > 0)
                store.set_kv("ws:has_pos", "1" if has_pos else "0")
                if long_sz > 0 and short_sz == 0:
                    store.set_kv("ws:pos_side", "long")
                    store.set_kv("ws:pos_sz", str(long_sz))
                elif short_sz > 0 and long_sz == 0:
                    store.set_kv("ws:pos_side", "short")
                    store.set_kv("ws:pos_sz", str(short_sz))
                elif not has_pos:
                    store.set_kv("ws:pos_side", "")
                    store.set_kv("ws:pos_sz", "0")
                else:
                    # 同时存在多空：选仓位更大的一边
                    if long_sz >= short_sz:
                        store.set_kv("ws:pos_side", "long")
                        store.set_kv("ws:pos_sz", str(long_sz))
                    else:
                        store.set_kv("ws:pos_side", "short")
                        store.set_kv("ws:pos_sz", str(short_sz))

                # 用于判断 WS 快照是否新鲜
                store.set_kv("ws_private:uptime", str(time.time()))

                # 打印实时仓位收益（你要的“收益额/收益率”）
                if long_sz > 0:
                    log.info("POS PNL LONG", extra={
                        "pos": round(long_sz, 6),
                        "upl_usdt": round(long_upl, 4),
                        "upl_pct": round(long_upl_ratio * 100, 4),
                    })
                if short_sz > 0:
                    log.info("POS PNL SHORT", extra={
                        "pos": round(short_sz, 6),
                        "upl_usdt": round(short_upl, 4),
                        "upl_pct": round(short_upl_ratio * 100, 4),
                    })
                return

            # ---- account：可选打印 ----
            if ch == "account":
                data = msg.get("data") or []
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    a = data[0]
                    log.info("WS ACCOUNT", extra={"totalEq": a.get("totalEq"), "uTime": a.get("uTime")})
                return

            # ---- event error ----
            if str(msg.get("event") or "").lower() == "error":
                log.error("PRIVATE WS EVENT ERROR", extra={
                    "code": msg.get("code"),
                    "msg": msg.get("msg"),
                    "connId": msg.get("connId"),
                })

        except Exception as e:
            log.warning("PRIVATE WS HANDLER ERROR", extra={"err": str(e)})

    return on_private_event



# -----------------------------
# Bar source (WS first, REST fallback)
# -----------------------------
def get_latest_bar_and_ema(
    cfg: dict,
    ex: OKXRest,
    store: SQLiteStore,
) -> Tuple[Optional[Dict[str, Any]], float, float]:
    """
    返回：
      bar(dict or None), ema_fast(float), ema_slow(float)

    优先：
      1) 如果项目里 OKXPublicWS 已把 candle 写入 store（ws:candle:...），就直接读
      2) 否则调用 REST：ex.get_latest_bar_with_ema(...)
    """
    inst_id = str((cfg.get("trade") or {}).get("inst_id") or "").strip()
    bar_name = str((cfg.get("trade") or {}).get("bar") or "1m").strip()
    fast = safe_int((cfg.get("strategy") or {}).get("fast"), 9)
    slow = safe_int((cfg.get("strategy") or {}).get("slow"), 21)

    # 1) WS -> store（如果你 public ws 有写入）
    ws_key = f"ws:candle:{inst_id}:{bar_name}"
    try:
        raw = store.get_kv(ws_key)
        if raw:
            obj = json.loads(raw)
            # 你 store 里可能写的是 {"bar":{...}, "ema_fast":..., "ema_slow":...}
            if isinstance(obj, dict) and "bar" in obj:
                b = obj.get("bar") or {}
                ef = safe_float(obj.get("ema_fast"), 0.0)
                es = safe_float(obj.get("ema_slow"), 0.0)
                if isinstance(b, dict) and ef and es:
                    return b, ef, es
            # 或者直接写 bar list/ dict
            if isinstance(obj, dict):
                # 如果 obj 本身就是 bar
                b = obj
                # EMA 仍需 REST 计算（fallback）
                # 这里走 REST 以保证 EMA 有值
    except Exception:
        pass

    # 2) REST fallback（兼容返回 tuple 或 dict）
    ret = ex.get_latest_bar_with_ema(inst_id=inst_id, bar=bar_name, fast=fast, slow=slow)

    # 兼容你之前出现过的“返回 tuple”
    if isinstance(ret, tuple) and len(ret) >= 3:
        b, ef, es = ret[0], safe_float(ret[1]), safe_float(ret[2])
        return b if isinstance(b, dict) else None, ef, es

    # 兼容返回 dict
    if isinstance(ret, dict):
        b = ret.get("bar") if isinstance(ret.get("bar"), dict) else ret
        ef = safe_float(ret.get("ema_fast") or ret.get("fast"), 0.0)
        es = safe_float(ret.get("ema_slow") or ret.get("slow"), 0.0)
        return b if isinstance(b, dict) else None, ef, es

    return None, 0.0, 0.0


# -----------------------------
# Strategy: EMA cross -> Signal
# -----------------------------
def generate_signal_from_ema(
    store: SQLiteStore,
    inst_id: str,
    candle_ts_ms: int,
    ema_fast: float,
    ema_slow: float,
) -> Optional[Signal]:
    """
    产生信号：
      - fast 上穿 slow => OPEN_LONG
      - fast 下穿 slow => OPEN_SHORT

    用 store 保存上一根关系，避免每轮都发单。
    """
    if candle_ts_ms <= 0 or ema_fast <= 0 or ema_slow <= 0:
        return None

    prev_rel = store.get_kv(f"ema_rel:{inst_id}") or ""
    rel = "GT" if ema_fast > ema_slow else "LT"

    # 首次只记录，不发单（避免开机立刻下单）
    if not prev_rel:
        store.set_kv(f"ema_rel:{inst_id}", rel)
        store.set_kv(f"ema_last_ts:{inst_id}", str(candle_ts_ms))
        return None

    # 同一根K线不重复发信号
    last_ts = safe_int(store.get_kv(f"ema_last_ts:{inst_id}") or "0", 0)
    if candle_ts_ms == last_ts:
        return None

    store.set_kv(f"ema_last_ts:{inst_id}", str(candle_ts_ms))

    # 交叉判定
    if prev_rel == "LT" and rel == "GT":
        store.set_kv(f"ema_rel:{inst_id}", rel)
        idem = make_idem("LONG", candle_ts_ms, ema_fast, ema_slow)
        return Signal(action="OPEN_LONG", idempotency_key=idem, reason="EMA golden cross")

    if prev_rel == "GT" and rel == "LT":
        store.set_kv(f"ema_rel:{inst_id}", rel)
        idem = make_idem("SHORT", candle_ts_ms, ema_fast, ema_slow)
        return Signal(action="OPEN_SHORT", idempotency_key=idem, reason="EMA dead cross")

    # 无交叉，只更新关系
    store.set_kv(f"ema_rel:{inst_id}", rel)
    return None


# -----------------------------
# Main
# -----------------------------
def main():
    # ---- load config
    cfg_path = os.environ.get("OKX_CFG", "").strip() or "config.yaml"
    if len(sys.argv) >= 2:
        cfg_path = sys.argv[1].strip()

    cfg = load_yaml(cfg_path)

    env = cfg.get("env") or {}
    demo = bool(env.get("demo", True))

    inst_id = str((cfg.get("trade") or {}).get("inst_id") or "").strip()
    td_mode = str((cfg.get("account") or {}).get("td_mode") or "isolated").strip()
    leverage = safe_int((cfg.get("account") or {}).get("leverage"), 1)
    use_ws = bool(env.get("use_ws", True))
    use_private_ws = bool(env.get("use_private_ws", True))

    # ---- store
    # 你项目里如果 db_path 在 cfg 里有，优先用；否则默认 ./data/okx_quant.db
    store_cfg = cfg.get("store") or {}
    db_path = str(
        store_cfg.get("path")
        or store_cfg.get("db_path")
        or "data/okx_quant.db"
    ).strip()

    # 确保目录存在
    dir_ = os.path.dirname(db_path)
    if dir_:
        os.makedirs(dir_, exist_ok=True)

    store = SQLiteStore(path=db_path)

    # ---- exchange REST
    # 兼容 OKXRest(cfg) 或 OKXRest(cfg, store)
    try:
        ex = OKXRest(cfg, store)
    except TypeError:
        ex = OKXRest(cfg)

    # ---- bootstrap（如果存在就做）
    if hasattr(ex, "bootstrap"):
        try:
            ex.bootstrap()
        except Exception as e:
            log.warning("BOOTSTRAP FAILED", extra={"err": str(e)})

    # ---- portfolio（你的 Portfolio 已做参数顺序自适应的话，两种都可以）
    try:
        portfolio = Portfolio(cfg, ex, store)
    except TypeError:
        portfolio = Portfolio(ex, store, cfg)

    # ---- risk
    try:
        risk = RiskManager(cfg, store)
    except TypeError:
        risk = RiskManager(cfg)

    # ---- order manager
    om = OrderManager(ex=ex, store=store, cfg=cfg, portfolio=portfolio, risk=risk)

    # ---- WS
    pws: Optional[OKXPrivateWS] = None
    wss: Optional[OKXPublicWS] = None

    # Public WS（通常只用来拿 candle；你已经修到 /business 后不会 60018）
    if use_ws:
        try:
            wss = OKXPublicWS(cfg, store)
            wss.start()
        except Exception as e:
            log.warning("Public WS start failed", extra={"err": str(e)})
            wss = None

    # Private WS（用于订单/仓位/余额回报）
    if use_private_ws:
        try:
            on_private_event = make_private_ws_handler(cfg, store)
            pws = OKXPrivateWS(cfg, store, on_event=on_private_event)
            pws.start()
        except Exception as e:
            log.warning("Private WS start failed", extra={"err": str(e)})
            pws = None

    # ---- boot log
    log.info(
        "BOOT OKX Quant Pro",
        extra={
            "inst": inst_id,
            "demo": demo,
            "tdMode": td_mode,
            "lev": leverage,
            "use_ws": bool(wss),
            "use_private_ws": bool(pws),
            "order_timeout_sec": safe_int((cfg.get("trade") or {}).get("order_timeout_sec"), 60),
            "proxy": bool((cfg.get("proxy") or {}).get("enabled", False)),
        },
    )

    # ---- main loop
    last_pf_refresh = 0.0
    pf_refresh_sec = float((cfg.get("trade") or {}).get("portfolio_refresh_sec", 5) or 5)

    loop_sleep = float((cfg.get("trade") or {}).get("loop_sleep_sec", 1) or 1)

    try:
        while True:
            t0 = now_ts()

            # 1) portfolio refresh（节流）
            if (t0 - last_pf_refresh) >= pf_refresh_sec:
                try:
                    portfolio.refresh()
                    upl_long = store.get_kv_float("ws:upl_long") or 0.0
                    upl_short = store.get_kv_float("ws:upl_short") or 0.0
                    r_long = store.get_kv_float("ws:upl_ratio_long") or 0.0
                    r_short = store.get_kv_float("ws:upl_ratio_short") or 0.0

                    log.info("POS PNL SNAPSHOT", extra={
                        "11111收益额:upl_long_usdt": round(upl_long, 4),
                        "2222收益率:upl_long_pct": round(r_long * 100, 4),
                        "3333收益额:upl_short_usdt": round(upl_short, 4),
                        "4444收益率upl_short_pct": round(r_short * 100, 4),
                    })

                    # --- equity & pnl print ---
                    equity = float(getattr(portfolio, "equity", 0.0) or 0.0)
                    avail = float(getattr(portfolio, "avail_usdt", 0.0) or 0.0)
                    pos_long = float(getattr(portfolio, "pos_long", 0.0) or 0.0)
                    pos_short = float(getattr(portfolio, "pos_short", 0.0) or 0.0)

                    # 仓位浮动盈亏（如果 portfolio 暴露了 upl，就打印；没有也不影响）
                    upl = float(getattr(portfolio, "upl", 0.0) or 0.0)

                    baseline = ensure_daily_baseline(store, equity)
                    pnl = equity - baseline
                    pnl_pct = (pnl / baseline) if baseline > 0 else 0.0

                    log.info("PNL SNAPSHOT", extra={
                        "equity_usd": round(equity, 4),
                        "baseline_usd": round(baseline, 4),
                        "收益额:pnl_usd": round(pnl, 4),
                        "收益率:pnl_pct": round(pnl_pct * 100, 4),
                        "upl_usd": round(upl, 4),
                        "avail_usdt": round(avail, 4),
                        "pos_long": pos_long,
                        "pos_short": pos_short,
                    })
                except Exception as e:
                    log.warning("PORTFOLIO REFRESH FAILED", extra={"err": str(e)})
                last_pf_refresh = t0

            # 2) 订单 housekeeping（timeout/部分成交/挂TP-SL等）
            try:
                om.housekeep()
            except Exception as e:
                log.error("ORDER HOUSEKEEP ERROR", extra={"err": str(e)})

            # 3) 获取最新 bar + EMA（WS优先，REST fallback）
            try:
                bar, ema_fast, ema_slow = get_latest_bar_and_ema(cfg, ex, store)
            except Exception as e:
                log.error("BAR FETCH ERROR", extra={"err": str(e)})
                bar, ema_fast, ema_slow = None, 0.0, 0.0

            if bar:
                c = bar_close(bar)
                ts_ms = bar_ts_ms(bar)

                # 4) 策略产生信号
                sig = None
                try:
                    sig = generate_signal_from_ema(store, inst_id, ts_ms, ema_fast, ema_slow)
                except Exception as e:
                    log.error("SIGNAL GEN ERROR", extra={"err": str(e)})

                # 5) 信号驱动下单
                if sig:
                    try:
                        om.on_signal(sig, {"close": c, "ts": ts_ms})
                    except Exception as e:
                        # 主循环不能炸
                        log.error("MAIN LOOP ORDER ERROR", extra={"err": str(e)})

            # 6) 如果 Private WS disabled（比如 60032），打印一次提示（避免刷屏）
            try:
                if pws and getattr(pws, "disabled", False):
                    # 只提示一次
                    once_key = "warn:private_ws_disabled_once"
                    if store.get_kv(once_key) != "1":
                        store.set_kv(once_key, "1")
                        log.error("Private WS disabled - fallback to REST polling", extra={
                            "reason": getattr(pws, "last_error", ""),
                            "hint": "For demo trading on www.okx.com, use wspap demo WS + simulated API key.",
                        })
            except Exception:
                pass

            time.sleep(loop_sleep)

    except KeyboardInterrupt:
        log.warning("EXIT by KeyboardInterrupt", extra={})
    finally:
        try:
            if wss:
                wss.stop()
        except Exception:
            pass
        try:
            if pws:
                pws.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
