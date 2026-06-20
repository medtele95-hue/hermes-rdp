# =====================================================================
#  risk_manager.py  —  position sizing + hard risk guards
#  No martingale, no grid, fixed-fractional only. DEMO-only enforced upstream.
# =====================================================================
import math
import config as cfg


class RiskManager:
    def __init__(self, balance):
        self.balance = float(balance)
        self.equity = float(balance)
        self.peak = float(balance)
        self.day = None
        self.day_start_equity = float(balance)
        self.consec_losses = 0
        self.open_trades = 0

    def _roll_day(self, now):
        d = now.date()
        if self.day != d:
            self.day = d
            self.day_start_equity = self.equity

    def can_open(self, now, is_demo):
        """Return (ok, blocked_reason). Order of hard guards matters."""
        self._roll_day(now)
        if cfg.DEMO_ONLY and not is_demo:
            return False, "NOT_DEMO"
        if self.open_trades >= cfg.MAX_OPEN_TRADES:
            return False, "MAX_OPEN_TRADES"
        if self.consec_losses >= cfg.MAX_CONSEC_LOSSES:
            return False, "MAX_CONSEC_LOSSES"
        daily_loss_pct = (self.day_start_equity - self.equity) / self.day_start_equity * 100 if self.day_start_equity > 0 else 0
        if daily_loss_pct >= cfg.MAX_DAILY_LOSS_PCT:
            return False, "DAILY_LOSS_LIMIT"
        dd_pct = (self.peak - self.equity) / self.peak * 100 if self.peak > 0 else 0
        if dd_pct >= cfg.MAX_DD_PCT:
            return False, "MAX_DRAWDOWN"
        return True, None

    def compute_lot(self, sl_distance, point, tick_value, tick_size, vol_min, vol_max, vol_step):
        """lot = risk_money / (sl_points * value_per_point). Returns 0.0 if min-lot would exceed risk."""
        if sl_distance <= 0 or point <= 0 or tick_value <= 0 or tick_size <= 0:
            return 0.0
        risk_money = self.balance * cfg.RISK_PCT / 100.0
        value_per_point = tick_value * (point / tick_size)     # account-ccy value of 1 point per 1.0 lot
        sl_points = sl_distance / point
        loss_per_lot = sl_points * value_per_point
        if loss_per_lot <= 0:
            return 0.0
        lot = risk_money / loss_per_lot
        lot = math.floor(lot / vol_step) * vol_step
        if lot < vol_min:        # min lot would risk MORE than allowed -> skip the trade
            return 0.0
        if lot > vol_max:
            lot = vol_max
        return round(lot, 2)

    def on_open(self):
        self.open_trades += 1

    def on_close(self, pnl_money):
        self.equity += pnl_money
        self.balance += pnl_money
        self.peak = max(self.peak, self.equity)
        self.open_trades = max(0, self.open_trades - 1)
        if pnl_money < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0

    def sync(self, equity, balance, open_trades):
        """Sync live state from MT5 account/positions each loop."""
        self.equity = float(equity)
        self.balance = float(balance)
        self.peak = max(self.peak, self.equity)
        self.open_trades = int(open_trades)
