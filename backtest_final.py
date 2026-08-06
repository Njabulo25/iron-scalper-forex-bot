# backtest_final.py
import pandas as pd
import numpy as np
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from config.settings import *

from src.strategies.daily_breakout import DailyBreakout
from src.strategies.london_scalper import LondonScalper

d5 = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"]).sort_values("time").reset_index(drop=True)

all_trades = []

if BREAKOUT_ENABLED:
    bo = DailyBreakout(d5)
    for t in bo.generate():
        all_trades.append(t)

if SCALPER_ENABLED:
    sc = LondonScalper(d5)
    for t in sc.generate():
        all_trades.append(t)

all_trades.sort(key=lambda x: x["time"])

# Backtest
wins = 0
losses = 0
total_pips = 0

for t in all_trades:
    idx = d5[d5["time"] == t["time"]].index[0]
    exit_price = None
    bars = 0
    
    for i in range(idx+1, len(d5)):
        bars += 1
        h = d5["high"].iloc[i]
        l = d5["low"].iloc[i]
        
        if t["type"] == "BUY":
            if l <= t["sl"]:
                exit_price = t["sl"]
                break
            if h >= t["tp"]:
                exit_price = t["tp"]
                break
        else:
            if h >= t["sl"]:
                exit_price = t["sl"]
                break
            if l <= t["tp"]:
                exit_price = t["tp"]
                break
    
    if exit_price is None:
        exit_price = d5["close"].iloc[-1]
    
    pips = (exit_price - t["entry"]) * 100 if t["type"] == "BUY" else (t["entry"] - exit_price) * 100
    
    if pips > 0:
        wins += 1
    else:
        losses += 1
    total_pips += pips

total = len(all_trades)
print("=" * 60)
print("FINAL BACKTEST - 9 MONTHS")
print("=" * 60)
print(f"Trades: {total}")
print(f"  Breakout: {sum(1 for t in all_trades if t['strategy']=='BREAKOUT')}")
print(f"  Scalper:  {sum(1 for t in all_trades if t['strategy']=='SCALPER')}")
print(f"Wins: {wins} ({wins/total*100:.0f}%)" if total else "N/A")
print(f"Losses: {losses}")
print(f"Total Pips: {total_pips:+.0f}")
print(f"Avg Pips: {total_pips/total:+.0f}" if total else "")