import MetaTrader5 as mt5
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
        if not mt5.initialize():
            raise Exception("MT5 not available")
        a = mt5.account_info()
        if a:
            self.risk_mgr.current_balance = a.balance
            self.risk_mgr.initial_balance = a.balance
        print(f"MT5 Connected. Balance: ${self.risk_mgr.current_balance:,.2f}")

    def stop(self):
        self._stop = True

    def get_data(self, symbol, tf, bars=2000):
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.sort_values("time").reset_index(drop=True)

    def place_trade(self, trade):
        symbol = BREAKOUT_SYMBOL
        tt = trade["type"]
        sl = trade["sl"]
        tp = trade["tp"]
        ok, reason = self.risk_mgr.can_trade()
        if not ok:
            print(f"Blocked: {reason}")
            return
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return
        cp = tick.ask if tt == "BUY" else tick.bid
        lots = max(round(self.risk_mgr.calculate_position_size(cp, sl, symbol), 2), 0.01)
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots, "type": mt5.ORDER_TYPE_BUY if tt == "BUY" else mt5.ORDER_TYPE_SELL, "price": cp, "sl": sl, "tp": tp, "deviation": 20, "magic": 123456, "comment": f"IronScalper_{trade['strategy']}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self.risk_mgr.register_trade()
            print(f"TRADE: {tt} {lots} lots @ {cp:.2f}")
        else:
            print(f"Failed: {res.comment}")

    def run_web(self):
        print("Bot running...")
        while not self._stop:
            try:
                df = self.get_data(BREAKOUT_SYMBOL, mt5.TIMEFRAME_M5, 2000)
                if df is not None and BREAKOUT_ENABLED:
                    for t in DailyBreakout(df).generate():
                        if t["time"].date() == datetime.now().date():
                            self.place_trade(t)
                if df is not None and SCALPER_ENABLED:
                    for t in LondonScalper(df).generate():
                        if (datetime.now() - t["time"]).total_seconds() < 300:
                            self.place_trade(t)
                if self.web_state and self.socketio:
                    tick = mt5.symbol_info_tick(BREAKOUT_SYMBOL)
                    if tick:
                        self.web_state["bid"] = tick.bid
                        self.web_state["ask"] = tick.ask
                    a = mt5.account_info()
                    if a:
                        self.web_state["balance"] = a.balance
                    self.web_state["status"] = "Running"
                    self.socketio.emit("state_update", self.web_state)
                time.sleep(10)
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(30)
        mt5.shutdown()
