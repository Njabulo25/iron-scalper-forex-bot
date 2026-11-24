# fix_trader_v2.py
code = '''import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import *
from src.strategies.daily_breakout import DailyBreakout
from src.strategies.london_scalper import LondonScalper
from src.strategies.risk_manager import RiskManager

class LiveTrader:
    def __init__(self, web_state=None, socketio=None):
        self.web_state = web_state
        self.socketio = socketio
        self.risk_mgr = RiskManager(ACCOUNT_BALANCE, RISK_PER_TRADE, MAX_DAILY_RISK, MAX_DAILY_TRADES, MAX_CONCURRENT_TRADES)
        self._stop = False
        self.last_signal_update = 0
        
        if not mt5.initialize():
            raise Exception("MT5 not available")
        
        a = mt5.account_info()
        if a:
            self.risk_mgr.current_balance = a.balance
            self.risk_mgr.initial_balance = a.balance
        
        # Count existing positions from MT5
        positions = mt5.positions_get(symbol=BREAKOUT_SYMBOL)
        if positions:
            self.risk_mgr.open_positions = len(positions)
        
        print(f"MT5 Connected. Balance: ${self.risk_mgr.current_balance:,.2f}")
        print(f"Existing positions: {self.risk_mgr.open_positions}")

    def stop(self):
        self._stop = True

    def get_data(self, symbol, tf, bars=2000):
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.sort_values("time").reset_index(drop=True)

    def get_open_positions_count(self):
        positions = mt5.positions_get(symbol=BREAKOUT_SYMBOL)
        return len(positions) if positions else 0

    def place_trade(self, trade):
        symbol = BREAKOUT_SYMBOL
        tt = trade["type"]
        sl = trade["sl"]
        tp = trade["tp"]
        
        # Sync open positions with MT5
        actual_open = self.get_open_positions_count()
        self.risk_mgr.open_positions = actual_open
        
        ok, reason = self.risk_mgr.can_trade()
        if not ok:
            return
        
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return
        
        cp = tick.ask if tt == "BUY" else tick.bid
        lots = max(round(self.risk_mgr.calculate_position_size(cp, sl, symbol), 2), 0.01)
        
        # Check if similar position already exists
        existing = mt5.positions_get(symbol=symbol)
        if existing:
            for pos in existing:
                if pos.type == (mt5.POSITION_TYPE_BUY if tt == "BUY" else mt5.POSITION_TYPE_SELL):
                    if abs(pos.price_open - cp) < 5.0:  # Within $5 of entry
                        print(f"Skipped: Similar position already open at {pos.price_open:.2f}")
                        return
        
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": lots, "type": mt5.ORDER_TYPE_BUY if tt == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": cp, "sl": sl, "tp": tp, "deviation": 20, "magic": 123456,
            "comment": f"IronScalper_{trade['strategy']}",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC
        }
        
        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self.risk_mgr.register_trade()
            self.risk_mgr.open_positions = self.get_open_positions_count()
            print(f"TRADE: {tt} {lots} lots @ {cp:.2f} SL:{sl:.2f} TP:{tp:.2f}")
        else:
            print(f"Failed: {res.comment}")

    def run_web(self):
        print("Bot running...")
        self.last_signal_update = 0
        while not self._stop:
            try:
                df = self.get_data(BREAKOUT_SYMBOL, mt5.TIMEFRAME_M5, 2000)
                
                if df is not None:
                    # Generate upcoming signals
                    upcoming = []
                    
                    if BREAKOUT_ENABLED:
                        for t in DailyBreakout(df).generate():
                            if t["time"].date() == datetime.now().date():
                                upcoming.append({
                                    "strategy": "BREAKOUT", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M")
                                })
                    
                    if SCALPER_ENABLED:
                        for t in LondonScalper(df).generate():
                            diff = (datetime.now() - t["time"]).total_seconds()
                            if diff < 600:
                                upcoming.append({
                                    "strategy": "SCALPER", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M")
                                })
                    
                    # Keep only most recent 5
                    upcoming = upcoming[-5:]
                    
                    # Update web state
                    if self.web_state:
                        self.web_state["upcoming"] = upcoming
                        
                        # Get open positions from MT5
                        positions = mt5.positions_get(symbol=BREAKOUT_SYMBOL)
                        open_pos = []
                        if positions:
                            for p in positions:
                                pos_type = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                                open_pos.append({
                                    "type": pos_type,
                                    "entry": p.price_open,
                                    "sl": p.sl if p.sl > 0 else 0,
                                    "tp": p.tp if p.tp > 0 else 0,
                                    "profit": p.profit,
                                    "strategy": p.comment if p.comment else "Manual"
                                })
                        self.web_state["open_positions"] = open_pos
                        
                        # Update account info
                        tick = mt5.symbol_info_tick(BREAKOUT_SYMBOL)
                        if tick:
                            self.web_state["bid"] = tick.bid
                            self.web_state["ask"] = tick.ask
                        a = mt5.account_info()
                        if a:
                            self.web_state["balance"] = a.balance
                            self.web_state["daily_pnl"] = a.balance - self.risk_mgr.initial_balance
                        
                        self.web_state["daily_trades"] = self.risk_mgr.daily_trades
                        self.web_state["total_trades"] = len(self.web_state.get("trade_history", []))
                        
                        if self.socketio:
                            self.socketio.emit("state_update", self.web_state)
                    
                    # Execute trades
                    now = datetime.now()
                    
                    if BREAKOUT_ENABLED:
                        for t in DailyBreakout(df).generate():
                            if t["time"].date() == now.date():
                                time_diff = (now - t["time"]).total_seconds()
                                if time_diff < 300 and time_diff > 30:
                                    self.place_trade(t)
                    
                    if SCALPER_ENABLED:
                        for t in LondonScalper(df).generate():
                            diff = (now - t["time"]).total_seconds()
                            if 30 < diff < 300:
                                self.place_trade(t)
                
                time.sleep(10)
                
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(30)
        
        mt5.shutdown()
        print("Bot stopped.")
'''

with open('src/engine/live_trader.py', 'w') as f:
    f.write(code)
print("Trader V2 written - MT5 sync + upcoming signals!")