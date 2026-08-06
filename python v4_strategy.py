# v4_strategy.py - Final version with clustering and strength filters
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
        self.df_15m["adx_rising"] = self.df_15m["adx"] > self.df_15m["adx"].shift(5)
        self.df_5m["ema20"] = self.df_5m["close"].ewm(span=20, adjust=False).mean()
        self.df_5m["atr"] = self._calc_atr(self.df_5m, 14)
        self.df_5m["rsi7"] = self._calc_rsi(self.df_5m["close"], 7)
        self.df_5m["swing_low"] = self.df_5m["low"].rolling(5).min()
        self.df_5m["swing_high"] = self.df_5m["high"].rolling(5).max()
        self.df_5m["vol_sma20"] = self.df_5m["tick_volume"].rolling(20).mean()

    def is_london_session(self, candle_time):
        t = candle_time.time()
        return time(7, 0) <= t <= time(17, 0)

    def generate_signals(self):
        trades = []
        last_trade_idx = -99
        min_bar_spacing = 5
        
        for i in range(60, len(self.df_5m)):
            row5 = self.df_5m.iloc[i]
            curr_time = row5["time"]
            
            if not self.is_london_session(curr_time):
                continue
            
            # Bar spacing filter
            if i - last_trade_idx < min_bar_spacing:
                continue
            
            mask = self.df_15m["time"] <= curr_time
            if not mask.any():
                continue
            last_15m = self.df_15m[mask].iloc[-1]
            last_15m_idx = self.df_15m[mask].index[-1]
            
            # Trend with rising ADX
            bull_trend = (last_15m["ema20"] > last_15m["ema50"]) and (last_15m["adx"] > 20)
            bear_trend = (last_15m["ema20"] < last_15m["ema50"]) and (last_15m["adx"] > 20)
            
            if not (bull_trend or bear_trend):
                continue
            
            # ADX must be rising (trend strengthening, not weakening)
            if not last_15m["adx_rising"]:
                continue
            
            atr5m = row5["atr"]
            close5 = row5["close"]
            ema20_5m = row5["ema20"]
            
            if pd.isna(ema20_5m) or pd.isna(atr5m):
                continue
            
            # Pullback to EMA20
            in_pullback = abs(close5 - ema20_5m) <= (0.8 * atr5m)
            if not in_pullback:
                continue
            
            # Momentum candle
            if row5["close"] <= row5["open"]:
                continue
            
            # Volume above average
            vol_ok = row5["tick_volume"] > row5["vol_sma20"] * 0.8
            if not vol_ok:
                continue
            
            rsi = row5["rsi7"]
            if pd.isna(rsi):
                continue
            if bull_trend and rsi < 40:
                continue
            if bear_trend and rsi > 60:
                continue
            
            # All filters passed
            if bull_trend:
                entry = row5["close"]
                sl = row5["swing_low"] - 1.5 * atr5m
                tp = entry + 2.0 * (entry - sl)
                trades.append({"type": "BUY", "entry": entry, "sl": sl, "tp": tp, "time": curr_time, "trend": "BULLISH"})
                last_trade_idx = i
            
            elif bear_trend:
                entry = row5["close"]
                sl = row5["swing_high"] + 1.5 * atr5m
                tp = entry - 2.0 * (sl - entry)
                trades.append({"type": "SELL", "entry": entry, "sl": sl, "tp": tp, "time": curr_time, "trend": "BEARISH"})
                last_trade_idx = i
        
        return trades

if __name__ == "__main__":
    df_15m = pd.read_csv("data/raw/XAUUSD_15Min.csv", parse_dates=["time"])
    df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"])
    df_15m = df_15m.sort_values("time").reset_index(drop=True)
    df_5m = df_5m.sort_values("time").reset_index(drop=True)
    scalper = GoldScalper(df_15m, df_5m)
    trades = scalper.generate_signals()
    print("=" * 70)
    print("GOLD SCALPER V4")
    print("=" * 70)
    print("Signals:", len(trades))
    wins = sum(1 for t in trades if t["type"] == "BUY")
    print(f"Buys: {wins}, Sells: {len(trades)-wins}")
'''

with open('src/strategies/gold_scalper.py', 'w', encoding='utf-8') as f:
    f.write(strategy_code)

print("V4 written! Run python src/strategies/gold_scalper.py")