# src/strategies/risk_manager.py
"""Position sizing and the trading brakes.

Deliberately pure: no MT5 calls, no database calls. Balance, open-position count
and broker symbol rules are passed in, so this module can be reasoned about and
tested without a terminal. Open-position count is *not* tracked here any more —
MT5 is the single source of truth for what is open.
"""

from datetime import date
from decimal import ROUND_DOWN, Decimal


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

    def _reset_daily(self):
        today = date.today()
        if today != self.today:
            self.today = today
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            # The daily loss limit is measured against the balance the day began with.
            self.day_start_balance = self.current_balance

    def loss_limit(self):
        return self.max_daily_risk * (self.day_start_balance or self.initial_balance)

    def can_trade(self, current_balance=None):
        self._reset_daily()

        if current_balance is not None:
            self.current_balance = current_balance

        if self.daily_pnl <= -self.loss_limit():
            return False, "Daily loss limit hit"

        if self.daily_trades >= self.max_trades:
            return False, "Max daily trades reached"

        if self.open_positions >= self.max_concurrent:
            return False, "Max concurrent positions"

        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"{self.max_consecutive_losses} consecutive losses - stop trading"

        return True, "OK"

    def size_position(self, balance, entry_price, stop_loss, symbol_info):
        """Lots that risk ``risk_per_trade`` of ``balance``, within broker limits.

        Rounds **down** to the broker's volume step: never round a risk position
        up. Returns 0.0 when the inputs cannot produce a valid size.
        """
        if symbol_info is None:
            return 0.0

        contract = float(getattr(symbol_info, "trade_contract_size", 0) or 0)
        vol_min = float(getattr(symbol_info, "volume_min", 0) or 0)
        vol_max = float(getattr(symbol_info, "volume_max", 0) or 0)
        vol_step = float(getattr(symbol_info, "volume_step", 0) or 0)
        distance = abs(entry_price - stop_loss)

        if contract <= 0 or vol_min <= 0 or vol_step <= 0 or vol_max < vol_min or distance <= 0:
            return 0.0

        risk_amount = max(balance, 0.0) * self.risk_per_trade
        raw_lots = risk_amount / (distance * contract)

        step = Decimal(str(vol_step))
        steps = (Decimal(str(raw_lots)) / step).to_integral_value(rounding=ROUND_DOWN)
        lots = steps * step
        lots = min(lots, Decimal(str(vol_max)))
        lots = max(lots, Decimal(str(vol_min)))
        return float(lots)

    def register_open(self):
        """Count a filled entry against the daily trade budget."""
        self.daily_trades += 1

    def register_close(self, profit_loss):
        """Register realised P/L from a closed trade.

        This is what feeds the daily-loss limit and the consecutive-loss stop.
        """
        self._reset_daily()
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
