# fix_strategy.py
strategy_code = '''import pandas as pd
import numpy as np
from datetime import time

class GoldScalper:
    def __init__(self, df_15m, df_5m):
        self.df_15m = df_15m.copy()
        self.df_5m = df_5m.copy()
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
        self.df_15m["atr"] = self._calc_atr(self.df_15m, 14)
        self.df_5m["ema20"] = self.df_5m["close"].ewm(span=20, adjust=False).mean()
        self.df_5m["ema50"] = self.df_5m["close"].ewm(span=50, adjust=False).mean()
        self.df_5m["atr"] = self._calc_atr(self.df_5m, 14)
        self.df_5m["rsi7"] = self._calc_rsi(self.df_5m["close"], 7)
        self.df_5m["swing_low"] = self.df_5m["low"].rolling(5).min()
        self.df_5m["swing_high"] = self.df_5m["high"].rolling(5).max()
        self.df_5m["body"] = abs(self.df_5m["close"] - self.df_5m["open"])

    def is_london_session(self, candle_time):
        t = candle_time.time()
        return time(7, 0) <= t <= time(17, 0)

    def generate_signals(self):
        trades = []
        start_idx = 60
        for i in range(start_idx, len(self.df_5m)):
            row5 = self.df_5m.iloc[i]
            curr_time = row5["time"]
            if not self.is_london_session(curr_time):
                continue
            mask = self.df_15m["time"] <= curr_time
            if not mask.any():
                continue
            last_15m = self.df_15m[mask].iloc[-1]
            bull_trend = (last_15m["ema20"] > last_15m["ema50"]) and (last_15m["adx"] > 25)
            bear_trend = (last_15m["ema20"] < last_15m["ema50"]) and (last_15m["adx"] > 25)
            if not (bull_trend or bear_trend):
                continue
            if pd.isna(row5["ema20"]) or pd.isna(row5["ema50"]):
                continue
            trend_aligned = (bull_trend and row5["ema20"] > row5["ema50"]) or (bear_trend and row5["ema20"] < row5["ema50"])
            if not trend_aligned:
                continue
            ema20_5m = row5["ema20"]
            atr5m = row5["atr"]
            close5 = row5["close"]
            if pd.isna(ema20_5m) or pd.isna(atr5m):
                continue
            in_pullback = abs(close5 - ema20_5m) <= (0.5 * atr5m)
            if not in_pullback:
                continue
            rsi = row5["rsi7"]
            if pd.isna(rsi):
                continue
            prev_rsi = self.df_5m["rsi7"].iloc[i-1] if i > 0 else 50
            if bull_trend:
                if not (prev_rsi <= 50 and rsi > 50):
                    continue
            else:
                if not (prev_rsi >= 50 and rsi < 50):
                    continue
            if i < 2:
                continue
            prev_open = self.df_5m.iloc[i-1]["open"]
            prev_close = self.df_5m.iloc[i-1]["close"]
            curr_open = row5["open"]
            curr_close = row5["close"]
            body_size = row5["body"]
            if pd.isna(body_size) or body_size < (0.5 * atr5m):
                continue
            bullish_engulf = (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)
            bearish_engulf = (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)
            if bull_trend and bullish_engulf:
                entry = curr_close
                sl = row5["swing_low"] - 2.0 * atr5m
                tp = entry + 2.0 * (entry - sl)
                trades.append({"type": "BUY", "entry": entry, "sl": sl, "tp": tp, "time": curr_time, "trend": "BULLISH", "adx": last_15m["adx"], "body_size": body_size, "atr": atr5m})
            elif bear_trend and bearish_engulf:
                entry = curr_close
                sl = row5["swing_high"] + 2.0 * atr5m
                tp = entry - 2.0 * (sl - entry)
                trades.append({"type": "SELL", "entry": entry, "sl": sl, "tp": tp, "time": curr_time, "trend": "BEARISH", "adx": last_15m["adx"], "body_size": body_size, "atr": atr5m})
        return trades

if __name__ == "__main__":
    df_15m = pd.read_csv("data/raw/XAUUSD_15Min.csv", parse_dates=["time"])
    df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"])
    df_15m = df_15m.sort_values("time").reset_index(drop=True)
    df_5m = df_5m.sort_values("time").reset_index(drop=True)
    scalper = GoldScalper(df_15m, df_5m)
    trades = scalper.generate_signals()
    print("=" * 70)
    print("GOLD SCALPER V2 - SIGNAL TEST")
    print("=" * 70)
    print("Data Range:", df_5m["time"].min(), "to", df_5m["time"].max())
    print("15-min bars:", len(df_15m))
    print("5-min bars:", len(df_5m))
    print("Total signals:", len(trades))
    if trades:
        print("All signals:")
        print("-" * 70)
        for t in trades:
            print(t["time"], "|", t["type"], "| Entry:", round(t["entry"], 2), "| SL:", round(t["sl"], 2), "| TP:", round(t["tp"], 2), "| ADX:", round(t["adx"], 1), "| Body:", round(t["body_size"], 2), "ATR")
    else:
        print("No signals generated with V2 filters.")
'''

with open('src/strategies/gold_scalper.py', 'w', encoding='utf-8') as f:
    f.write(strategy_code)

print("DONE - gold_scalper.py updated!")