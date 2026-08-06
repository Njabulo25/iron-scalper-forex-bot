# backtest_combined.py
import pandas as pd
import numpy as np
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import *
from src.strategies.gold_scalper import GoldScalper as DailyBreakout
from src.strategies.london_scalper import LondonScalper
from src.strategies.risk_manager import RiskManager

df_15m = pd.read_csv("data/raw/XAUUSD_15Min.csv", parse_dates=["time"])
df_15m = df_15m.sort_values("time").reset_index(drop=True)
df_5m = pd.read_csv("data/raw/XAUUSD_M5.csv", parse_dates=["time"])
df_5m = df_5m.sort_values("time").reset_index(drop=True)

all_trades = []

if BREAKOUT_ENABLED:
    breakout = DailyBreakout(df_5m)
    breakout_trades = breakout.generate_signals()
    for t in breakout_trades:
        t["strategy"] = "BREAKOUT"
    all_trades.extend(breakout_trades)

if SCALPER_ENABLED:
    scalper = LondonScalper(df_15m, df_5m, sys.modules[__name__])
    scalper_trades = scalper.generate_signals()
    all_trades.extend(scalper_trades)

all_trades.sort(key=lambda x: x["time"])

risk_mgr = RiskManager(ACCOUNT_BALANCE, RISK_PER_TRADE, MAX_DAILY_RISK, MAX_DAILY_TRADES, MAX_CONCURRENT_TRADES)
results = []
balance_curve = [ACCOUNT_BALANCE]

for trade in all_trades:
    can_trade, reason = risk_mgr.can_trade()
    if not can_trade:
        continue
    
    entry_time = trade["time"]
    entry_price = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    trade_type = trade["type"]
    
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
        pips = (exit_price - entry_price) * 100
    else:
        pips = (entry_price - exit_price) * 100
    
    risk_pips = abs(entry_price - sl) * 100
    position_size = risk_mgr.calculate_position_size(entry_price, sl)
    dollar_pnl = pips * position_size * 0.01
    
    risk_mgr.register_trade(dollar_pnl)
    balance_curve.append(risk_mgr.current_balance)
    
    results.append({
        **trade,
        "exit": exit_price,
        "pips": pips,
        "bars": bars_held,
        "pnl": dollar_pnl,
        "win": pips > 0
    })

print("=" * 80)
print("IRON SCALPER - COMBINED BACKTEST")
print("=" * 80)

wins = sum(1 for r in results if r["win"])
losses = len(results) - wins
total_pips = sum(r["pips"] for r in results)
total_pnl = sum(r["pnl"] for r in results)

for r in results:
    status = "WIN" if r["win"] else "LOSS"
    print(f"\n{status} | {r['time']} | {r['strategy']:9s} | {r['type']:4s}")
    print(f"  Entry: {r['entry']:.2f} | Exit: {r['exit']:.2f} | Pips: {r['pips']:+.0f} | Bars: {r['bars']}")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Total Trades: {len(results)}")
print(f"  Breakout: {sum(1 for r in results if r['strategy']=='BREAKOUT')}")
print(f"  Scalper:  {sum(1 for r in results if r['strategy']=='SCALPER')}")
print(f"Wins: {wins} ({wins/len(results)*100:.0f}%)" if results else "N/A")
print(f"Losses: {losses}")
print(f"Total Pips: {total_pips:+.0f}")
print(f"Total P&L: ${total_pnl:+.2f}")
print(f"Final Balance: ${balance_curve[-1]:,.2f}")
print(f"Return: {(balance_curve[-1]/ACCOUNT_BALANCE-1)*100:+.1f}%")
if results:
    print(f"Avg Pips/Trade: {total_pips/len(results):+.0f}")