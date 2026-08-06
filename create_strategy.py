# create_strategy.py - Run this once to create the strategy file

strategy_code = r'''
import pandas as pd
import numpy as np
from datetime import time

class GoldScalper:
    def __init__(self, df_15m, df_5m):
        self.df_15m = df_15m.copy()
        self.df_5m = df_5m.copy()
        self._calc_indicators()

    def _calc_atr(self, df, period=14):
        high, low, close = df['high'], df['low'], df['close']
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
        up = df['high'] - df['high'].shift(1)
        down = df['low'].shift(1) - df['low']
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        plus_di = 100 * (pd.Series(plus_dm).rolling(period).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm).rolling(period).mean() / atr)
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx.rolling(period).mean()

    def _calc_indicators(self):
        self.df_15m['ema20'] = self.df_15m['close'].ewm(span=20, adjust=False).mean()
        self.df_15m['ema50'] = self.df_15m['close'].ewm(span=50, adjust=False).mean()
        self.df_15m['adx'] = self._calc_adx(self.df_15m, 14)
        self.df_5m['ema20'] = self.df_5m['close'].ewm(span=20, adjust=False).mean()
        self.df_5m['ema50'] = self.df_5m['close'].ewm(span=50, adjust=False).mean()
        self.df_5m['atr'] = self._calc_atr(self.df_5m, 14)
        self.df_5m['rsi7'] = self._calc_rsi(self.df_5m['close'], 7)
        self.df_5m['swing_low'] = self.df_5m['low'].rolling(5).min()
        self.df_5m['swing_high'] = self.df_5m['high'].rolling(5).max()

    def is_london_session(self, candle_time):
        t = candle_time.time()
        return time(8, 0) <= t <= time(16, 0)

    def generate_signals(self):
        trades = []
        start_idx = 50
        for i in range(start_idx, len(self.df_5m)):
            row5 = self.df_5m.iloc[i]
            curr_time = row5['time']
            if not self.is_london_session(curr_time):
                continue
            mask = self.df_15m['time'] <= curr_time
            if not mask.any():
                continue
            last_15m = self.df_15m[mask].iloc[-1]
            bull_trend = (last_15m['ema20'] > last_15m['ema50']) and (last_15m['adx'] > 20)
            bear_trend = (last_15m['ema20'] < last_15m['ema50']) and (last_15m['adx'] > 20)
            if not (bull_trend or bear_trend):
                continue
            ema20_5m = row5['ema20']
            atr5m = row5['atr']
            close5 = row5['close']
            if pd.isna(ema20_5m) or pd.isna(atr5m):
                continue
            in_pullback = abs(close5 - ema20_5m) <= (0.3 * atr5m)
            if not in_pullback:
                continue
            rsi = row5['rsi7']
            if pd.isna(rsi):
                continue
            if not (45 <= rsi <= 55):
                continue
            if i < 2:
                continue
            prev_open = self.df_5m.iloc[i-1]['open']
            prev_close = self.df_5m.iloc[i-1]['close']
            curr_open = row5['open']
            curr_close = row5['close']
            bullish_engulf = (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)
            bearish_engulf = (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)
            if bull_trend and bullish_engulf:
                entry = curr_close
                sl = row5['swing_low'] - 0.5 * atr5m
                tp = entry + 2.0 * (entry - sl)
                trades.append({'type': 'BUY', 'entry': entry, 'sl': sl, 'tp': tp, 'time': curr_time, 'trend': 'BULLISH'})
            elif bear_trend and bearish_engulf:
                entry = curr_close
                sl = row5['swing_high'] + 0.5 * atr5m
                tp = entry - 2.0 * (sl - entry)
                trades.append({'type': 'SELL', 'entry': entry, 'sl': sl, 'tp': tp, 'time': curr_time, 'trend': 'BEARISH'})
        return trades

if __name__ == '__main__':
    df_15m = pd.read_csv('data/raw/XAUUSD_15Min.csv', parse_dates=['time'])
    df_5m = pd.read_csv('data/raw/XAUUSD_M5.csv', parse_dates=['time'])
    df_15m = df_15m.sort_values('time').reset_index(drop=True)
    df_5m = df_5m.sort_values('time').reset_index(drop=True)
    scalper = GoldScalper(df_15m, df_5m)
    trades = scalper.generate_signals()
    print('=' * 70)
    print('GOLD SCALPER - SIGNAL TEST')
    print('=' * 70)
    print(f'Data Range: {df_5m["time"].min()} to {df_5m["time"].max()}')
    print(f'15-min bars: {len(df_15m)}')
    print(f'5-min bars: {len(df_5m)}')
    print(f'Total signals: {len(trades)}')
    if trades:
        print('Last 5 signals:')
        print('-' * 70)
        for t in trades[-5:]:
            print(f"  {t['time']} | {t['type']:4s} | Entry: {t['entry']:.2f} | SL: {t['sl']:.2f} | TP: {t['tp']:.2f} | {t['trend']}")
    else:
        print('No signals generated. Try adjusting parameters.')
'''

with open('src/strategies/gold_scalper.py', 'w') as f:
    f.write(strategy_code)

print('gold_scalper.py created successfully!')