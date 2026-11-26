import numpy as np
import pandas as pd

from config.settings import CONFIG


class LondonScalper:
    """London-session EMA pullback scalper on its configured bar timeframe.

    Every indicator period, band and ATR multiple comes from
    :class:`config.settings.ScalperConfig` rather than a literal in this file.
    """

    def __init__(self, df_5m, config=None):
        self.config = config or CONFIG.scalper
        self.df = df_5m.copy()
        self._calc()

    def _calc(self):
        config = self.config
        high, low, close = self.df["high"], self.df["low"], self.df["close"]

        tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
        self.df["atr"] = tr.rolling(config.atr_period).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(config.rsi_period).mean()
        loss = -delta.clip(upper=0).rolling(config.rsi_period).mean()
        self.df["rsi"] = 100 - (100 / (1 + gain / loss))

        self.df["ema_fast"] = close.ewm(span=config.ema_fast, adjust=False).mean()
        self.df["ema_slow"] = close.ewm(span=config.ema_slow, adjust=False).mean()

    def generate(self):
        config = self.config
        trades = []
        last_index = -10
        start = max(config.ema_slow, config.atr_period) + 10

        for i in range(start, len(self.df)):
            row = self.df.iloc[i]
            if not config.session_start <= row["time"].time() <= config.session_end:
                continue
            if i - last_index < config.min_bar_spacing:
                continue

            atr = row["atr"]
            close = row["close"]
            ema = row["ema_fast"]
            rsi = row["rsi"]
            if pd.isna(atr) or atr == 0 or pd.isna(ema) or pd.isna(rsi):
                continue
            if abs(close - ema) > config.pullback_atr * atr:
                continue

            body = close - row["open"]
            bullish = row["ema_fast"] > row["ema_slow"]
            bearish = row["ema_fast"] < row["ema_slow"]

            if bullish and body > 0 and config.rsi_long_min < rsi < config.rsi_long_max:
                trades.append({
                    "type": "BUY", "entry": close,
                    "sl": close - config.sl_atr * atr,
                    "tp": close + config.tp_atr * atr,
                    "time": row["time"], "strategy": "SCALPER",
                })
                last_index = i
            elif bearish and body < 0 and config.rsi_short_min < rsi < config.rsi_short_max:
                trades.append({
                    "type": "SELL", "entry": close,
                    "sl": close + config.sl_atr * atr,
                    "tp": close - config.tp_atr * atr,
                    "time": row["time"], "strategy": "SCALPER",
                })
                last_index = i

        return trades
