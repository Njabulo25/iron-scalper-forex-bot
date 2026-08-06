# backtest_daily.py
import pandas as pd
import numpy as np
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parent / "src"))
from strategies.gold_scalper import GoldScalper

df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"])
df_5m = df_5m.sort_values("time").reset_index(drop=True)

scalper = GoldScalper(df_5m)
trades = scalper.generate_signals()

print("=" * 70)
print("DAILY BREAKOUT - BACKTEST")
print("=" * 70)

if not trades:
    print("No trades found")
    exit()

wins = 0
losses = 0
total_pips = 0
total_r = 0

for trade in trades:
    entry_time = trade["time"]
    entry_price = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    trade_type = trade["type"]
    asian_range = trade["asian_range"]
    
    entry_idx = df_5m[df_5m["time"] == entry_time].index[0]
    
    exit_price = None
    bars_held = 0
    
    for i in range(entry_idx + 1, len(df_5m)):
        bars_held += 1
        high = df_5m["high"].iloc[i]
        low = df_5m["low"].iloc[i]
        
        if trade_type == "BUY":
            if low <= sl:
                exit_price = sl
                break
            if high >= tp:
                exit_price = tp
                break
        else:
            if high >= sl:
                exit_price = sl
                break
            if low <= tp:
                exit_price = tp
                break
    
    if exit_price is None:
        exit_price = df_5m["close"].iloc[-1]
    
    if trade_type == "BUY":
        profit_pips = (exit_price - entry_price) * 100
    else:
        profit_pips = (entry_price - exit_price) * 100
    
    risk_pips = abs(entry_price - sl) * 100
    reward_pips = abs(tp - entry_price) * 100
    r_multiple = profit_pips / risk_pips if risk_pips > 0 else 0
    
    is_win = profit_pips > 0
    if is_win:
        wins += 1
    else:
        losses += 1
    
    total_pips += profit_pips
    total_r += r_multiple
    
    hours_held = bars_held * 5 / 60
    
    status = "WIN" if is_win else "LOSS"
    print(f"\n{status} | {trade['time']} | {trade['type']}")
    print(f"  Asian Range: {asian_range:.2f} | Entry: {entry_price:.2f} | Exit: {exit_price:.2f}")
    print(f"  SL: {sl:.2f} | TP: {tp:.2f}")
    print(f"  Pips: {profit_pips:+.1f} | R-Multiple: {r_multiple:+.2f}R")
    print(f"  Duration: {hours_held:.1f} hours ({bars_held} bars)")

win_rate = (wins / len(trades) * 100) if trades else 0
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Total Trades: {len(trades)}")
print(f"Wins: {wins} ({win_rate:.0f}%)")
print(f"Losses: {losses} ({100-win_rate:.0f}%)")
print(f"Total Pips: {total_pips:+.1f}")
print(f"Total R: {total_r:+.2f}R")
if trades:
    print(f"Avg Pips/Trade: {total_pips/len(trades):+.1f}")
    print(f"Avg R/Trade: {total_r/len(trades):+.2f}R")
print(f"Expectancy: {(win_rate/100 * 1.5) - ((100-win_rate)/100 * 1.0):.2f}R per trade")