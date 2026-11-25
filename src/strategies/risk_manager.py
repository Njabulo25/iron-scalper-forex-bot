# src/strategies/risk_manager.py
"""Position sizing and the trading brakes.

Deliberately pure: no MT5 calls, no database calls. Balance, open-position count
and broker symbol rules are passed in, so this module can be reasoned about and
tested without a terminal. Open-position count is *not* tracked here any more —
MT5 is the single source of truth for what is open.
"""

from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
import math


class RiskManager:
    def __init__(
        self, balance, risk_per_trade=0.01, max_daily_risk=0.03, max_trades=10,
        max_concurrent=3, max_consecutive_losses=3,
    ):
        self.initial_balance = balance
        self.current_balance = balance
        self.day_start_balance = balance
        self.risk_per_trade = risk_per_trade
        self.max_daily_risk = max_daily_risk
        self.max_trades = max_trades
        self.max_concurrent = max_concurrent
        self.max_consecutive_losses = max_consecutive_losses

        self.today = date.today()
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.open_positions = 0
        self.consecutive_losses = 0

    def _reset_daily(self, today=None):
        today = today or date.today()
        if isinstance(today, datetime):
            today = today.date()
        if today != self.today:
            self.today = today
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            # The daily loss limit is measured against the balance the day began with.
            self.day_start_balance = self.current_balance

    def loss_limit(self):
        return self.max_daily_risk * (self.day_start_balance or self.initial_balance)

    def can_trade(self, current_balance=None, today=None):
        if current_balance is not None:
            self.current_balance = current_balance
        self._reset_daily(today)

        if self.daily_pnl <= -self.loss_limit():
            return False, "Daily loss limit hit"

        if self.daily_trades >= self.max_trades:
            return False, "Max daily trades reached"

        if self.open_positions >= self.max_concurrent:
            return False, "Max concurrent positions"

        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"{self.max_consecutive_losses} consecutive losses - stop trading"

        return True, "OK"

    def size_position(self, balance, entry_price, stop_loss, symbol_info, loss_per_lot):
        """Return lots within budget using MT5's account-currency loss estimate.

        Rounds **down** to the broker's volume step: never round a risk position
        up. Returns 0.0 when the broker minimum would exceed the risk budget or
        when the supplied loss estimate is invalid.
        """
        if symbol_info is None:
            return 0.0

        vol_min = float(getattr(symbol_info, "volume_min", 0) or 0)
        vol_max = float(getattr(symbol_info, "volume_max", 0) or 0)
        vol_step = float(getattr(symbol_info, "volume_step", 0) or 0)
        loss_per_lot = float(loss_per_lot or 0)

        if (
            not math.isfinite(loss_per_lot) or loss_per_lot <= 0
            or not math.isfinite(float(balance)) or balance <= 0
            or vol_min <= 0 or vol_step <= 0 or vol_max < vol_min
        ):
            return 0.0

        risk_amount = float(balance) * self.risk_per_trade
        raw_lots = risk_amount / loss_per_lot
        if raw_lots + 1e-12 < vol_min:
            return 0.0

        step = Decimal(str(vol_step))
        steps = (Decimal(str(raw_lots)) / step).to_integral_value(rounding=ROUND_DOWN)
        max_steps = (Decimal(str(vol_max)) / step).to_integral_value(rounding=ROUND_DOWN)
        lots = min(steps, max_steps) * step
        if lots < Decimal(str(vol_min)):
            return 0.0
        return float(lots)

    def register_open(self, today=None):
        """Count a filled entry against the daily trade budget."""
        self._reset_daily(today)
        self.daily_trades += 1

    def register_close(self, profit_loss, today=None):
        """Register realised P/L from a closed trade.

        This is what feeds the daily-loss limit and the consecutive-loss stop.
        """
        self._reset_daily(today)
        profit_loss = float(profit_loss)
        self.daily_pnl += profit_loss
        self.current_balance += profit_loss

        if profit_loss < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def get_status(self):
        self._reset_daily()
        return {
            "balance": self.current_balance,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "open_positions": self.open_positions,
            "consecutive_losses": self.consecutive_losses,
        }
