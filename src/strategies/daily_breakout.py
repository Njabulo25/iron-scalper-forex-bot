import numpy as np
import pandas as pd

from config.settings import CONFIG


class DailyBreakout:
    """Asian-range breakout on 5-minute bars.

    Every number that shapes a signal comes from :class:`config.settings.BreakoutConfig`,
    so the tuned values reach the engine instead of being shadowed by literals.
    """

    def __init__(self, df_5m, config=None):
        self.config = config or CONFIG.breakout
        self.df = df_5m.copy()
        self._calc()

    def _calc(self):
        high, low, close = self.df["high"], self.df["low"], self.df["close"]
        tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
        self.df["atr"] = tr.rolling(self.config.atr_period).mean()

    def get_asian(self, day):
        """High and low of the Asian range for ``day``, or ``(None, None)``."""
        start = day.replace(
            hour=self.config.asian_start.hour,
            minute=self.config.asian_start.minute,
            second=0,
        )
        end = day.replace(
            hour=self.config.asian_end.hour,
            minute=self.config.asian_end.minute,
            second=0,
        )
        window = self.df[(self.df["time"] >= start) & (self.df["time"] < end)]
        if len(window) < self.config.min_asian_bars:
            return None, None
        return window["high"].max(), window["low"].min()

    def generate(self):
        config = self.config
        trades = []
        done = set()

        for i in range(config.atr_period + 16, len(self.df)):
            row = self.df.iloc[i]
            close_time = row["time"]
            day = close_time.replace(hour=0, minute=0, second=0)
            if day in done:
                continue

            in_session = config.session_start <= close_time.time() <= config.session_end
            if not in_session:
                continue

            asian_high, asian_low = self.get_asian(day)
            if asian_high is None:
                continue

            price_range = asian_high - asian_low
            atr = row["atr"]
            if pd.isna(atr) or atr == 0 or price_range < config.min_range:
                continue

            close = row["close"]
            stop_distance = price_range * config.sl_range_pct
            target_distance = stop_distance * config.tp_rr

            if close > asian_high and close - asian_high <= config.atr_multiplier * atr:
                trades.append({
                    "type": "BUY", "entry": close,
                    "sl": close - stop_distance, "tp": close + target_distance,
                    "time": close_time, "strategy": "BREAKOUT",
                })
                done.add(day)
            elif close < asian_low and asian_low - close <= config.atr_multiplier * atr:
                trades.append({
                    "type": "SELL", "entry": close,
                    "sl": close + stop_distance, "tp": close - target_distance,
                    "time": close_time, "strategy": "BREAKOUT",
                })
                done.add(day)

        return trades
