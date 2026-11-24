# src/strategies/risk_manager.py
from datetime import datetime, date


class RiskManager:
    def __init__(self, balance, risk_per_trade=0.01, max_daily_risk=0.03, max_trades=10, max_concurrent=3):
        self.initial_balance = balance
        self.current_balance = balance
        self.day_start_balance = balance
        self.risk_per_trade = risk_per_trade
        self.max_daily_risk = max_daily_risk
        self.max_trades = max_trades
        self.max_concurrent = max_concurrent

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

    def can_trade(self, current_balance=None):
        self._reset_daily()

        if current_balance:
            self.current_balance = current_balance

        # Daily loss limit
        loss_limit = self.max_daily_risk * (self.day_start_balance or self.initial_balance)
        if self.daily_pnl <= -loss_limit:
            return False, "Daily loss limit hit"

        # Max daily trades
        if self.daily_trades >= self.max_trades:
            return False, "Max daily trades reached"

        # Max concurrent positions
        if self.open_positions >= self.max_concurrent:
            return False, "Max concurrent positions"

        # Stop after 3 consecutive losses
        if self.consecutive_losses >= 3:
            return False, "3 consecutive losses - stop trading"

        return True, "OK"

    def calculate_position_size(self, entry_price, sl_price, symbol="XAUUSD"):
        risk_amount = self.current_balance * self.risk_per_trade
        sl_distance = abs(entry_price - sl_price)

        if sl_distance == 0:
            return 0.0

        # XAUUSD: 1 lot = 100 ounces, $1 move = $100 per lot
        contract_value = 100.0  # $100 per $1 move per lot
        position_size = risk_amount / (sl_distance * contract_value)

        return round(position_size, 2)

    def register_trade(self, profit_loss=None):
        """Register a newly opened trade."""
        self.daily_trades += 1
        self.open_positions += 1

        if profit_loss is not None:
            self.daily_pnl += profit_loss
            self.current_balance += profit_loss
            self.open_positions -= 1

            if profit_loss < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

    def register_close(self, profit_loss):
        """Register realised P/L from a closed trade.

        This is what feeds the daily-loss limit and the consecutive-loss stop.
        """
        self._reset_daily()
        profit_loss = float(profit_loss)
        self.daily_pnl += profit_loss
        self.current_balance += profit_loss
        self.open_positions = max(self.open_positions - 1, 0)

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
            "consecutive_losses": self.consecutive_losses
        }
