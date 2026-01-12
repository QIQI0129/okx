from datetime import datetime
from zoneinfo import ZoneInfo

SG_TZ = ZoneInfo("Asia/Singapore")

class RiskManager:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg

    def _today_sg(self) -> str:
        return datetime.now(SG_TZ).date().isoformat()

    def ensure_daily_reset(self, portfolio):
        today = self._today_sg()
        last_date = self.store.get_kv("daily_base_date")

        if last_date != today:
            self.store.set_kv("daily_base_date", today)
            self.store.set_kv("daily_base_equity", str(portfolio.equity))
            self.store.del_kv("halted")

    def refresh_daily_baseline(self, portfolio):
        self.ensure_daily_reset(portfolio)
        base = self.store.get_kv_float("daily_base_equity")
        if base is None or base <= 0:
            self.store.set_kv("daily_base_equity", str(portfolio.equity))

    def is_halted(self, portfolio) -> bool:
        self.ensure_daily_reset(portfolio)

        if self.store.get_kv("halted") == "1":
            return True

        base = self.store.get_kv_float("daily_base_equity") or portfolio.equity
        dd = (base - portfolio.equity) / max(base, 1e-9)

        if dd >= float(self.cfg["risk"]["daily_loss_limit_pct"]):
            self.store.set_kv("halted", "1")
            return True

        return False
