from dataclasses import dataclass

@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    idempotency_key: str

class EMACrossStrategy:
    def __init__(self, cfg):
        self.prev_diff = None

    def on_bar(self, bar, portfolio):
        diff = bar["ema_fast"] - bar["ema_slow"]

        if self.prev_diff is None:
            self.prev_diff = diff
            return None

        base = f"{bar['ts']}_{bar['ema_fast']:.4f}_{bar['ema_slow']:.4f}"
        sig = None

        if self.prev_diff <= 0 and diff > 0:
            sig = Signal("OPEN_LONG", "EMA golden cross", "SIG_LONG_" + base)
        elif self.prev_diff >= 0 and diff < 0:
            sig = Signal("OPEN_SHORT", "EMA dead cross", "SIG_SHORT_" + base)

        self.prev_diff = diff
        return sig
