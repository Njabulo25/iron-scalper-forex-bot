# src/engine/live_trader.py
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import sys
from pathlib import Path
from datetime import datetime, time as dt_time

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
        self.open_positions = {}
        self.last_breakout_day = None
        self.last_scalper_time = None
        self._stop = False
        
        if not mt5.initialize():
            raise Exception("MT5 not available")
        
        account = mt5.account_info()
        if account:
            self.risk_mgr.current_balance = account.balance
            self.risk_mgr.initial_balance = account.balance
        
        print(f"MT5 Connected. Balance: ${self.risk_mgr.current_balance:,.2f}")
    
    def stop(self):
        self._stop = True
    
    def get_data(self, symbol, timeframe, bars=2000):
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
        if rates is None:
            return None
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df.sort_values('time').reset_index(drop=True)
    
    def update_web_state(self):
        if self.web_state is None or self.socketio is None:
            return
        
        tick = mt5.symbol_info_tick(BREAKOUT_SYMBOL)
        if tick:
            self.web_state["bid"] = tick.bid
            self.web_state["ask"] = tick.ask
            self.web_state["spread"] = (tick.ask - tick.bid) * 100
        
        account = mt5.account_info()
        if account:
            self.web_state["balance"] = account.balance
        
        status = self.risk_mgr.get_status()
        self.web_state["daily_pnl"] = status["daily_pnl"]
        self.web_state["daily_trades"] = status["daily_trades"]
        self.web_state["total_trades"] = len(self.web_state.get("trade_history", []))
        
        self.socketio.emit('state_update', self.web_state)
    
    def place_trade(self, trade):
        symbol = BREAKOUT_SYMBOL
        trade_type = trade["type"]
        sl = trade["sl"]
        tp = trade["tp"]
        
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            print(f"Trade blocked: {reason}")
            return None
        
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        
        current_price = tick.ask if trade_type == "BUY" else tick.bid
        lot_size = max(round(self.risk_mgr.calculate_position_size(current_price, sl, symbol), 2), 0.01)
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": mt5.ORDER_TYPE_BUY if trade_type == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": current_price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 123456,
            "comment": f"IronScalper_{trade['strategy']}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.risk_mgr.register_trade()
            
            trade_info = {
                "type": trade_type,
                "entry": current_price,
                "sl": sl,
                "tp": tp,
                "strategy": trade["strategy"],
                "time": datetime.now().strftime("%H:%M:%S")
            }
            
            if self.web_state is not None:
                self.web_state["open_positions"].append(trade_info)
            
            print(f"TRADE: {trade_type} {lot_size} lots @ {current_price:.2f}")
            return result.order
        
        print(f"Order failed: {result.comment}")
        return None
    
    def check_positions(self):
        positions = mt5.positions_get()
        closed = []
        
        for order_id, trade in list(self.open_positions.items()):
            if positions is None or not any(p.ticket == order_id for p in positions):
                closed.append(order_id)
                
                # Calculate P&L
                history = mt5.history_deals_get(position=order_id)
                if history and len(history) > 0:
                    profit = sum(d.profit for d in history)
                    self.risk_mgr.current_balance += profit
                    
                    if self.web_state is not None:
                        self.web_state["open_positions"] = [p for p in self.web_state["open_positions"] if p.get("ticket") != order_id]
                        self.web_state["trade_history"].append({
                            "time": datetime.now().strftime("%m/%d %H:%M"),
                            "type": trade.get("type", "?"),
                            "entry": trade.get("entry", 0),
                            "exit": trade.get("exit", trade.get("entry", 0)),
                            "pips": trade.get("pips", 0),
                            "win": profit > 0,
                            "strategy": trade.get("strategy", "?")
                        })
        
        for order_id in closed:
            del self.open_positions[order_id]
    
    def run_web(self):
        """Run for web dashboard."""
        print("Bot started. Trading...")
        
        while not self._stop:
            try:
                self.check_positions()
                
                df_5m = self.get_data(BREAKOUT_SYMBOL, mt5.TIMEFRAME_M5, 2000)
                if df_5m is not None:
                    if BREAKOUT_ENABLED:
                        breakout = DailyBreakout(df_5m)
                        for trade in breakout.generate():
                            if trade["time"].date() == datetime.now().date():
                                self.place_trade(trade)
                    
                    if SCALPER_ENABLED:
                        scalper = LondonScalper(df_5m)
                        for trade in scalper.generate():
                            diff = (datetime.now() - trade["time"]).total_seconds()
                            if diff < 300:
                                self.place_trade(trade)
                
                self.update_web_state()
                time.sleep(10)
                
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(30)
        
        mt5.shutdown()
        print("Bot stopped.")