# backtest_quick.py
import pandas as pd
import numpy as np
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parent / "src"))
from strategies.gold_scalper import GoldScalper

# Load and prepare data
df_15m = pd.read_csv("data/raw/XAUUSD_15Min.csv", parse_dates=['time'])
df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=['time'])
df_15m = df_15m.sort_values('time').reset_index(drop=True)
df_5m = df_5m.sort_values('time').reset_index(drop=True)

# Generate signals (this also calculates all indicators on both timeframes)
scalper = GoldScalper(df_15m, df_5m)
trades = scalper.generate_signals()

print("=" * 70)
print("🔍 BACKTEST RESULTS")
print("=" * 70)

if not trades:
    print("\n❌ No trades to analyze")
    print("\n📊 Data diagnostics:")
    print(f"   15m bars: {len(df_15m)} ({df_15m['time'].min()} to {df_15m['time'].max()})")
    print(f"   5m bars: {len(df_5m)} ({df_5m['time'].min()} to {df_5m['time'].max()})")
    
    # Check how many 5m bars are in session
    session_count = 0
    for t in df_5m['time']:
        hour = t.hour
        if 8 <= hour < 16:
            session_count += 1
    print(f"   5m bars in London session (8-16 GMT): {session_count}")
    exit()

# Use the indicators already calculated in the scalper object
df_15m_ind = scalper.df_15m
df_5m_ind = scalper.df_5m

wins = 0
losses = 0
total_profit_pips = 0
total_bars_held = 0

for trade in trades:
    entry_time = trade['time']
    entry_price = trade['entry']
    sl = trade['sl']
    tp = trade['tp']
    trade_type = trade['type']
    
    # Find entry index
    entry_idx = df_5m_ind[df_5m_ind['time'] == entry_time].index[0]
    
    # Get trend context at entry
    mask = df_15m_ind['time'] <= entry_time
    last_15 = df_15m_ind[mask].iloc[-1]
    
    # Simulate trade
    exit_price = None
    exit_time = None
    bars_held = 0
    
    for i in range(entry_idx + 1, len(df_5m_ind)):
        bars_held += 1
        high = df_5m_ind['high'].iloc[i]
        low = df_5m_ind['low'].iloc[i]
        current_time = df_5m_ind['time'].iloc[i]
        
        if trade_type == 'BUY':
            if low <= sl:
                exit_price = sl
                exit_time = current_time
                break
            if high >= tp:
                exit_price = tp
                exit_time = current_time
                break
        else:  # SELL
            if high >= sl:
                exit_price = sl
                exit_time = current_time
                break
            if low <= tp:
                exit_price = tp
                exit_time = current_time
                break
    
    if exit_price is None:
        exit_price = df_5m_ind['close'].iloc[-1]
        exit_time = df_5m_ind['time'].iloc[-1]
    
    # Calculate profit
    if trade_type == 'BUY':
        profit_pips = (exit_price - entry_price) * 100
    else:
        profit_pips = (entry_price - exit_price) * 100
    
    risk_pips = abs(entry_price - sl) * 100
    reward_pips = abs(tp - entry_price) * 100
    is_win = profit_pips > 0
    
    if is_win:
        wins += 1
    else:
        losses += 1
    
    total_profit_pips += profit_pips
    total_bars_held += bars_held
    
    # Display trade details
    status = "✅ WIN" if is_win else "❌ LOSS"
    print(f"\n{status} | {trade['time']} | {trade['type']}")
    print(f"  📊 Trend: EMA20={last_15['ema20']:.2f} EMA50={last_15['ema50']:.2f} ADX={last_15['adx']:.1f}")
    print(f"  💰 Entry: {entry_price:.2f} -> Exit: {exit_price:.2f}")
    print(f"  📈 SL: {sl:.2f} | TP: {tp:.2f}")
    print(f"  💎 Pips: {profit_pips:+.1f} | Risk: {risk_pips:.1f} | Reward: {reward_pips:.1f}")
    print(f"  ⏱️  Bars held: {bars_held} | Exit time: {exit_time}")
    
    # Show what happened at exit
    exit_idx = df_5m_ind[df_5m_ind['time'] == exit_time].index[0]
    exit_row = df_5m_ind.iloc[exit_idx]
    print(f"  📋 Exit bar: O={exit_row['open']:.2f} H={exit_row['high']:.2f} L={exit_row['low']:.2f} C={exit_row['close']:.2f}")

# Summary
print("\n" + "=" * 70)
print("📊 SUMMARY")
print("=" * 70)
print(f"Total Trades: {len(trades)}")
if len(trades) > 0:
    print(f"Wins: {wins} ({wins/len(trades)*100:.0f}%)")
    print(f"Losses: {losses} ({losses/len(trades)*100:.0f}%)")
    print(f"Total Pips: {total_profit_pips:+.1f}")
    print(f"Avg Pips/Trade: {total_profit_pips/len(trades):+.1f}")
    print(f"Avg Bars Held: {total_bars_held/len(trades):.1f}")
    print(f"Risk:Reward Target: 1:2.0")
    print(f"Win Rate Needed for Breakeven: 33%")
    print(f"Actual Win Rate: {wins/len(trades)*100:.0f}%")

# Analysis
print("\n" + "=" * 70)
print("🔬 ANALYSIS")
print("=" * 70)
if wins == 0 and len(trades) > 0:
    print("⚠️  All trades hit stop loss. Possible issues:")
    print("   1. SL too tight (1.5 ATR) - getting stopped by noise")
    print("   2. Entry timing off - entering before pullback completes")
    print("   3. Trend may have already been weakening")
    print("   4. Need to check if engulfing was valid (not just a small candle)")
    print("\n💡 Suggestions:")
    print("   - Widen SL to 2.0 ATR below swing low")
    print("   - Add filter: engulfing candle must be > 0.5 ATR in size")
    print("   - Wait for RSI to cross back above 50 after pullback (not just neutral)")
    print("   - Add a 5-min trend filter (5m EMA20 > EMA50 for longs)")