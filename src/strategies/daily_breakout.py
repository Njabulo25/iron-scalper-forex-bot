import pandas as pd
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
