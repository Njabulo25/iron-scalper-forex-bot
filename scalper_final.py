# scalper_final.py
new_code = '''import pandas as pd
import numpy as np
from datetime import time

class LondonScalper:
    def __init__(self, df_15m, df_5m, config):
        self.df_15m = df_15m.copy()
        self.df = df_5m.copy()
        self.config = config
        self._calc_indicators()

    def _calc_atr(self, df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
        return tr.rolling(period).mean()

    def _calc_rsi(self, df, period=7):
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = -delta.clip(upper=0).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _calc_adx(self, df, period=14):
        atr = self._calc_atr(df, period)
        up = df["high"] - df["high"].shift(1)
        down = df["low"].shift(1) - df["low"]
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        plus_di = 100 * (pd.Series(plus_dm).rolling(period).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm).rolling(period).mean() / atr)
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx.rolling(period).mean()

    def _calc_indicators(self):
        self.df_15m["ema20"] = self.df_15m["close"].ewm(span=20, adjust=False).mean()
        self.df_15m["ema50"] = self.df_15m["close"].ewm(span=50, adjust=False).mean()
        self.df_15m["adx"] = self._calc_adx(self.df_15m, 14)
        self.df["ema_fast"] = self.df["close"].ewm(span=20, adjust=False).mean()
        self.df["ema_slow"] = self.df["close"].ewm(span=50, adjust=False).mean()
        self.df["atr"] = self._calc_atr(self.df, 14)
        self.df["rsi"] = self._calc_rsi(self.df, 7)
        self.df["swing_low"] = self.df["low"].rolling(3).min()
        self.df["swing_high"] = self.df["high"].rolling(3).max()

    def generate_signals(self):
        trades = []
        last_trade_idx = -99
        
        for i in range(60, len(self.df)):
            row = self.df.iloc[i]
            curr_time = row["time"]
            
            t = curr_time.time()
            if not (self.config.SCALPER_SESSION_START <= t <= self.config.SCALPER_SESSION_END):
                continue
            
            if i - last_trade_idx < self.config.SCALPER_MIN_BAR_SPACING:
                continue
            
            mask = self.df_15m["time"] <= curr_time
            if not mask.any():
                continue
            last_15 = self.df_15m[mask].iloc[-1]
            
            bull_15 = (last_15["ema20"] > last_15["ema50"]) and (last_15["adx"] > 25)
            bear_15 = (last_15["ema20"] < last_15["ema50"]) and (last_15["adx"] > 25)
            
            if not (bull_15 or bear_15):
                continue
            
            atr = row["atr"]
            close = row["close"]
            ema = row["ema_fast"]
            
            if pd.isna(atr) or atr == 0 or pd.isna(ema):
                continue
            
            in_pullback = abs(close - ema) <= 0.8 * atr
            if not in_pullback:
                continue
            
            rsi = row["rsi"]
            if pd.isna(rsi):
                continue
            
            body = close - row["open"]
            prev_low = self.df["low"].iloc[i-1] if i > 0 else 0
            prev_high = self.df["high"].iloc[i-1] if i > 0 else 99999
            
            if bull_15 and body > 0 and rsi < 65 and close > prev_low:
                entry = close
                sl = row["swing_low"] - 2.0 * atr
                tp = entry + 2.5 * atr
                trades.append({
                    "type": "BUY", "strategy": "SCALPER",
                    "entry": entry, "sl": sl, "tp": tp,
                    "time": curr_time
                })
                last_trade_idx = i
            
            elif bear_15 and body < 0 and rsi > 35 and close < prev_high:
                entry = close
                sl = row["swing_high"] + 2.0 * atr
                tp = entry - 2.5 * atr
                trades.append({
                    "type": "SELL", "strategy": "SCALPER",
                    "entry": entry, "sl": sl, "tp": tp,
                    "time": curr_time
                })
                last_trade_idx = i
        
        return trades
'''

with open('src/strategies/london_scalper.py', 'w') as f:
    f.write(new_code)

print("Clean scalper written!")