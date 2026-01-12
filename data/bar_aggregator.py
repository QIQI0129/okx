import threading
from collections import deque
from typing import Optional, Dict, Any

class EMARolling:
    def __init__(self, period: int):
        self.period = period
        self.k = 2 / (period + 1)
        self.value: Optional[float] = None

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = price
        else:
            self.value = self.value + self.k * (price - self.value)
        return self.value

class BarAggregator:
    def __init__(self, fast: int, slow: int, max_bars: int = 200):
        self.fast = fast
        self.slow = slow
        self._ema_fast = EMARolling(fast)
        self._ema_slow = EMARolling(slow)

        self._bars = deque(maxlen=max_bars)
        self._latest_bar: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def on_candle(self, candle_row):
        ts = int(candle_row[0])
        o = float(candle_row[1])
        h = float(candle_row[2])
        l = float(candle_row[3])
        c = float(candle_row[4])

        ef = float(self._ema_fast.update(c))
        es = float(self._ema_slow.update(c))

        bar = {
            "ts": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "ema_fast": ef,
            "ema_slow": es,
        }

        with self._lock:
            self._bars.append(bar)
            self._latest_bar = bar

    def latest_bar(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest_bar) if self._latest_bar else None
