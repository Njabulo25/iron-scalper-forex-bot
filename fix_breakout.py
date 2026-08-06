# fix_breakout.py
strategy_code = '''import pandas as pd
import numpy as np
from datetime import time

class GoldScalper:
    def __init__(self, df_5m):
        self.df = df_5m.copy()
        self._calc_indicators()

    def _calc_atr(self, df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
        return tr.rolling(period).mean()

    def _calc_rsi(self, series, period):
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = -delta.clip(upper=0).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _calc_indicators(self):
        self.df["atr5"] = self._calc_atr(self.df, 14)
        self.df["rsi7"] = self._calc_rsi(self.df["close"], 7)

    def get_asian_range(self, day_start):
        asian_start = day_start.replace(hour=0, minute=0, second=0)
        asian_end = day_start.replace(hour=7, minute=0, second=0)
        mask = (self.df["time"] >= asian_start) & (self.df["time"] < asian_end)
        asian_data = self.df[mask]
        if len(asian_data) < 10:
            return None, None
        return asian_data["high"].max(), asian_data["low"].min()

    def generate_signals(self):
        trades = []
        days_processed = set()
        
        for i in range(20, len(self.df)):
            row = self.df.iloc[i]
            curr_time = row["time"]
            day_start = curr_time.replace(hour=0, minute=0, second=0)
            
            if day_start in days_processed:
                continue
            
            t = curr_time.time()
            if not (time(7, 0) <= t <= time(10, 0)):
                continue
            
            result = self.get_asian_range(day_start)
            if result is None:
                continue
            asian_high, asian_low = result
            asian_range = asian_high - asian_low
            
            atr5 = row["atr5"]
            if pd.isna(atr5) or atr5 == 0:
                continue
            
            close = row["close"]
            rsi = row["rsi7"]
            if pd.isna(rsi):
                continue
            
            # Minimum range: at least 20 pips (0.20 in XAUUSD price)
            if asian_range < 0.20:
                continue
            
            # Bullish breakout
            if close > asian_high:
                if close - asian_high > 1.5 * atr5:
                    continue
                if rsi < 50:
                    continue
                if row["close"] <= row["open"]:
                    continue
                
                entry = close
                sl = asian_low
                risk = entry - sl
                tp = entry + 1.5 * risk
                
                trades.append({
                    "type": "BUY", "entry": entry, "sl": sl, "tp": tp,
                    "time": curr_time, "asian_high": asian_high, "asian_low": asian_low
                })
                days_processed.add(day_start)
            
            # Bearish breakout
            elif close < asian_low:
                if asian_low - close > 1.5 * atr5:
                    continue
                if rsi > 50:
                    continue
                if row["close"] >= row["open"]:
                    continue
                
                entry = close
                sl = asian_high
                risk = sl - entry
                tp = entry - 1.5 * risk
                
                trades.append({
                    "type": "SELL", "entry": entry, "sl": sl, "tp": tp,
                    "time": curr_time, "asian_high": asian_high, "asian_low": asian_low
                })
                days_processed.add(day_start)
        
        return trades

if __name__ == "__main__":
    df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"])
    df_5m = df_5m.sort_values("time").reset_index(drop=True)
    scalper = GoldScalper(df_5m)
    trades = scalper.generate_signals()
    print("=" * 70)
    print("LONDON BREAKOUT V2")
    print("=" * 70)
    print("Signals:", len(trades))
    for t in trades:
        print(f"  {t['time']} | {t['type']} | Entry: {t['entry']:.2f} | SL: {t['sl']:.2f} | TP: {t['tp']:.2f}")
'''

with open('src/strategies/gold_scalper.py', 'w', encoding='utf-8') as f:
    f.write(strategy_code)

print("Fixed breakout strategy written!")