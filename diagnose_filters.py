# diagnose_filters.py - Shows which filters are blocking signals
import pandas as pd
import numpy as np
from datetime import time

# Load data
df_15m = pd.read_csv("data/raw/XAUUSD_15Min.csv", parse_dates=['time'])
df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=['time'])
df_15m = df_15m.sort_values('time').reset_index(drop=True)
df_5m = df_5m.sort_values('time').reset_index(drop=True)

# Calculate indicators (simplified for speed)
for df in [df_15m, df_5m]:
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

# Simple ADX on 15m
high, low, close = df_15m['high'], df_15m['low'], df_15m['close']
tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
atr14 = tr.rolling(14).mean()
up = high - high.shift(1)
down = low.shift(1) - low
plus_dm = np.where((up > down) & (up > 0), up, 0)
minus_dm = np.where((down > up) & (down > 0), down, 0)
plus_di = 100 * (pd.Series(plus_dm).rolling(14).mean() / atr14)
minus_di = 100 * (pd.Series(minus_dm).rolling(14).mean() / atr14)
dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
df_15m['adx'] = dx.rolling(14).mean()

# 5-min indicators
df_5m['atr'] = tr.rolling(14).mean()[:len(df_5m)]  # approximate
delta = df_5m['close'].diff()
gain = delta.clip(lower=0).rolling(7).mean()
loss = -delta.clip(upper=0).rolling(7).mean()
rs = gain / loss
df_5m['rsi7'] = 100 - (100 / (1 + rs))
df_5m['body'] = abs(df_5m['close'] - df_5m['open'])

# Count how many bars pass each filter
total_bars = len(df_5m)
start_idx = 60

session_count = 0
trend_count = 0
aligned_count = 0
pullback_count = 0
rsi_count = 0
body_count = 0
engulf_count = 0

for i in range(start_idx, len(df_5m)):
    row5 = df_5m.iloc[i]
    curr_time = row5['time']
    
    # Filter 1: Session
    t = curr_time.time()
    in_session = time(7, 0) <= t <= time(17, 0)
    if not in_session:
        continue
    session_count += 1
    
    # Filter 2: 15m Trend
    mask = df_15m['time'] <= curr_time
    if not mask.any():
        continue
    last_15m = df_15m[mask].iloc[-1]
    bull_trend = (last_15m['ema20'] > last_15m['ema50']) and (last_15m['adx'] > 25)
    bear_trend = (last_15m['ema20'] < last_15m['ema50']) and (last_15m['adx'] > 25)
    if not (bull_trend or bear_trend):
        continue
    trend_count += 1
    
    # Filter 3: 5m Trend Alignment
    if pd.isna(row5['ema20']) or pd.isna(row5['ema50']):
        continue
    aligned = (bull_trend and row5['ema20'] > row5['ema50']) or (bear_trend and row5['ema20'] < row5['ema50'])
    if not aligned:
        continue
    aligned_count += 1
    
    # Filter 4: Pullback to EMA20
    if pd.isna(row5['atr']):
        continue
    in_pullback = abs(row5['close'] - row5['ema20']) <= (0.5 * row5['atr'])
    if not in_pullback:
        continue
    pullback_count += 1
    
    # Filter 5: RSI Momentum
    rsi = row5['rsi7']
    if pd.isna(rsi):
        continue
    prev_rsi = df_5m['rsi7'].iloc[i-1] if i > 0 else 50
    if bull_trend:
        rsi_ok = (prev_rsi <= 50 and rsi > 50)
    else:
        rsi_ok = (prev_rsi >= 50 and rsi < 50)
    if not rsi_ok:
        continue
    rsi_count += 1
    
    # Filter 6: Body Size
    body_size = row5['body']
    if pd.isna(body_size) or body_size < (0.5 * row5['atr']):
        continue
    body_count += 1
    
    # Filter 7: Engulfing
    if i < 2:
        continue
    prev_open = df_5m.iloc[i-1]['open']
    prev_close = df_5m.iloc[i-1]['close']
    curr_open = row5['open']
    curr_close = row5['close']
    
    bullish_engulf = (prev_close < prev_open) and (curr_close > curr_open) and \
                     (curr_close > prev_open) and (curr_open < prev_close)
    bearish_engulf = (prev_close > prev_open) and (curr_close < curr_open) and \
                     (curr_close < prev_open) and (curr_open > prev_close)
    
    if bull_trend and bullish_engulf:
        engulf_count += 1
    elif bear_trend and bearish_engulf:
        engulf_count += 1

# Display results
print("=" * 70)
print("FILTER DIAGNOSIS - Where bars get eliminated")
print("=" * 70)
print(f"Total 5-min bars: {total_bars}")
print(f"Bars analyzed (after warmup): {total_bars - start_idx}")
print()
print(f"{'Filter':<30} {'Passed':>8} {'% of Previous':>15}")
print("-" * 55)
print(f"{'1. Session (7-17 GMT)':<30} {session_count:>8} {session_count/(total_bars-start_idx)*100:>14.1f}%")
print(f"{'2. 15m Trend (ADX>25)':<30} {trend_count:>8} {trend_count/max(session_count,1)*100:>14.1f}%")
print(f"{'3. 5m Trend Aligned':<30} {aligned_count:>8} {aligned_count/max(trend_count,1)*100:>14.1f}%")
print(f"{'4. Pullback to EMA20':<30} {pullback_count:>8} {pullback_count/max(aligned_count,1)*100:>14.1f}%")
print(f"{'5. RSI Momentum':<30} {rsi_count:>8} {rsi_count/max(pullback_count,1)*100:>14.1f}%")
print(f"{'6. Body > 0.5 ATR':<30} {body_count:>8} {body_count/max(rsi_count,1)*100:>14.1f}%")
print(f"{'7. Engulfing Pattern':<30} {engulf_count:>8} {engulf_count/max(body_count,1)*100:>14.1f}%")

print("\n" + "=" * 70)
print("RECOMMENDATION")
print("=" * 70)
if session_count < 100:
    print("- Session filter too restrictive? Check timezone of data")
if trend_count < 50:
    print("- ADX>25 too strict, try ADX>20")
if pullback_count < 20:
    print("- Pullback distance (0.5 ATR) too tight, try 0.8 ATR")
if rsi_count < 10:
    print("- RSI cross filter too strict, go back to neutral zone (40-60)")
if engulf_count == 0:
    print("- Engulfing patterns are rare in 5-min data. Try:")
    print("  1. Remove engulfing requirement")
    print("  2. Use simple close > open (bullish candle) instead")
    print("  3. Enter on break of previous candle high/low")