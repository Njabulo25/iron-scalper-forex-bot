# build_final.py - Clean optimized strategies
import os
os.makedirs('src/strategies', exist_ok=True)

# ============================================================
# DAILY BREAKOUT - Optimized
# ============================================================
breakout_code = '''import pandas as pd
import numpy as np
from datetime import time

class DailyBreakout:
    def __init__(self, df_5m):
        self.df = df_5m.copy()
        self._calc()

    def _calc(self):
        high, low, close = self.df["high"], self.df["low"], self.df["close"]
        tr = np.maximum(high-low, np.maximum(abs(high-close.shift()), abs(low-close.shift())))
        self.df["atr"] = tr.rolling(14).mean()

    def get_asian(self, day):
        start = day.replace(hour=0, minute=0)
        end = day.replace(hour=7, minute=0)
        d = self.df[(self.df["time"] >= start) & (self.df["time"] < end)]
        if len(d) < 10:
            return None, None
        return d["high"].max(), d["low"].min()

    def generate(self):
        trades = []
        done = set()
        for i in range(30, len(self.df)):
            row = self.df.iloc[i]
            ct = row["time"]
            day = ct.replace(hour=0, minute=0, second=0)
            if day in done:
                continue
            t = ct.time()
            if not (time(7,0) <= t <= time(12,0)):
                continue
            ah, al = self.get_asian(day)
            if ah is None:
                continue
            rng = ah - al
            atr = row["atr"]
            if pd.isna(atr) or atr == 0 or rng < 0.15:
                continue
            c = row["close"]
            if c > ah and c - ah <= 1.5 * atr:
                sl = c - rng * 0.5
                tp = c + rng * 0.75
                trades.append({"type":"BUY","entry":c,"sl":sl,"tp":tp,"time":ct,"strategy":"BREAKOUT"})
                done.add(day)
            elif c < al and al - c <= 1.5 * atr:
                sl = c + rng * 0.5
                tp = c - rng * 0.75
                trades.append({"type":"SELL","entry":c,"sl":sl,"tp":tp,"time":ct,"strategy":"BREAKOUT"})
                done.add(day)
        return trades
'''

# ============================================================
# LONDON SCALPER - Optimized  
# ============================================================
scalper_code = '''import pandas as pd
import numpy as np
from datetime import time

class LondonScalper:
    def __init__(self, df_5m):
        self.df = df_5m.copy()
        self._calc()

    def _calc(self):
        high, low, close = self.df["high"], self.df["low"], self.df["close"]
        tr = np.maximum(high-low, np.maximum(abs(high-close.shift()), abs(low-close.shift())))
        self.df["atr"] = tr.rolling(14).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(7).mean()
        loss = -delta.clip(upper=0).rolling(7).mean()
        self.df["rsi"] = 100 - (100/(1+gain/loss))
        self.df["ema20"] = close.ewm(span=20, adjust=False).mean()
        self.df["ema50"] = close.ewm(span=50, adjust=False).mean()

    def generate(self):
        trades = []
        last_idx = -10
        for i in range(60, len(self.df)):
            row = self.df.iloc[i]
            t = row["time"].time()
            if not (time(8,0) <= t <= time(16,0)):
                continue
            if i - last_idx < 5:
                continue
            atr = row["atr"]
            c = row["close"]
            ema = row["ema20"]
            rsi = row["rsi"]
            if pd.isna(atr) or atr == 0 or pd.isna(ema) or pd.isna(rsi):
                continue
            if abs(c - ema) > 0.8 * atr:
                continue
            body = c - row["open"]
            bull = row["ema20"] > row["ema50"]
            bear = row["ema20"] < row["ema50"]
            if bull and body > 0 and rsi > 40 and rsi < 70:
                sl = c - 1.5 * atr
                tp = c + 2.5 * atr
                trades.append({"type":"BUY","entry":c,"sl":sl,"tp":tp,"time":row["time"],"strategy":"SCALPER"})
                last_idx = i
            elif bear and body < 0 and rsi < 60 and rsi > 30:
                sl = c + 1.5 * atr
                tp = c - 2.5 * atr
                trades.append({"type":"SELL","entry":c,"sl":sl,"tp":tp,"time":row["time"],"strategy":"SCALPER"})
                last_idx = i
        return trades
'''

with open('src/strategies/daily_breakout.py', 'w') as f:
    f.write(breakout_code)
with open('src/strategies/london_scalper.py', 'w') as f:
    f.write(scalper_code)

print("Clean strategies written!")