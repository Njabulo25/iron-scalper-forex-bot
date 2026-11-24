import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import *
from src.engine.database import (
    get_open_trade_by_position,
    get_recent_trades,
    get_trade_stats,
    init_db,
    log_signal,
    log_trade_close,
    log_trade_open,
)
from src.strategies.daily_breakout import DailyBreakout
from src.strategies.london_scalper import LondonScalper
from src.strategies.risk_manager import RiskManager

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "bot_state.json"


def _server_timestamp(epoch_seconds):
    """Convert an MT5 server epoch to the same naive frame as bar timestamps."""
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).replace(tzinfo=None)


class LiveTrader:
    # MT5 deal entry types that mean the deal closed (or reduced) a position.
    _CLOSING_ENTRIES = (
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
    )

    def __init__(self, web_state=None, socketio=None):
        self.web_state = web_state
        self.socketio = socketio
        self._stop = False
        self._executed_signals = set()
        self._logged_signal_keys = set()
        self._cached_signals = []
        self._last_bar_time = None

        self._load_state()

        self.risk_mgr = RiskManager(ACCOUNT_BALANCE, RISK_PER_TRADE, MAX_DAILY_RISK, MAX_DAILY_TRADES, MAX_CONCURRENT_TRADES)

        if not mt5.initialize():
            raise Exception("MT5 not available")

        try:
            init_db()
        except Exception:
            pass

        a = mt5.account_info()
        if a:
            self.risk_mgr.current_balance = a.balance
            self.risk_mgr.initial_balance = a.balance

        positions = mt5.positions_get(symbol=BREAKOUT_SYMBOL)
        if positions:
            self.risk_mgr.open_positions = len(positions)

        print(f"MT5 Connected. Balance: ${self.risk_mgr.current_balance:,.2f}")
        print(f"Existing positions: {self.risk_mgr.open_positions}")
        print(f"Executed signals loaded: {len(self._executed_signals)}")

    # ── State persistence ──────────────────────────────────────────

    def _load_state(self):
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            saved_date = data.get("date", "")
            self._executed_signals = set(data.get("executed_signals", []))
            if saved_date != str(datetime.now().date()):
                self._executed_signals = set()
        else:
            self._executed_signals = set()

    def _save_state(self):
        STATE_FILE.write_text(json.dumps({
            "date": str(datetime.now().date()),
            "executed_signals": list(self._executed_signals),
            "balance": self.risk_mgr.current_balance
        }, indent=2))

    # ── Clock helpers ──────────────────────────────────────────────

    def _server_now(self):
        """Broker clock, in the same frame as the bar timestamps.

        Comparing datetime.now() (this PC's clock) with MT5 bar times is what
        made signals look hours old or hours in the future, so orders either
        fired late or never fired at all.
        """
        tick = mt5.symbol_info_tick(BREAKOUT_SYMBOL)
        if tick is not None and getattr(tick, "time", 0):
            return _server_timestamp(tick.time)
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _display_offset(self):
        """Offset from broker time to the local clock, rounded to 15 minutes."""
        try:
            seconds = (datetime.now() - self._server_now()).total_seconds()
            return timedelta(minutes=round(seconds / 900) * 15)
        except Exception:
            return timedelta(hours=2)

    def _signal_age_seconds(self, trade, server_now=None):
        """How long ago the signal bar closed, measured on the broker clock."""
        server_now = server_now or self._server_now()
        return (server_now - trade["time"]).total_seconds()

    # ── MT5 helpers ────────────────────────────────────────────────

    def stop(self):
        self._stop = True
        self._save_state()

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

    def _signal_key(self, trade):
        return f"{trade['type']}_{trade['entry']:.1f}_{trade['time'].strftime('%Y%m%d_%H%M')}_{trade.get('strategy','?')}"

    def _check_spread(self, symbol):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, 0
        spread = (tick.ask - tick.bid) * 100
        max_spread = MAX_SPREAD_PIPS
        if spread > max_spread:
            print(f"Spread too high: {spread:.0f} pips (max {max_spread})")
            return False, spread
        return True, spread

    def _calculate_position_size(self, symbol, entry_price, stop_loss):
        """Risk-based lot size using MT5's real symbol rules."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.01

        contract_size = info.trade_contract_size
        vol_min = info.volume_min
        vol_max = info.volume_max
        vol_step = info.volume_step
        sl_distance = abs(entry_price - stop_loss)

        if contract_size <= 0 or vol_min <= 0 or vol_max < vol_min or vol_step <= 0 or sl_distance <= 0:
            return 0.01

        risk_amount = self.risk_mgr.current_balance * RISK_PER_TRADE
        raw = risk_amount / (sl_distance * contract_size)
        clamped = min(max(raw, vol_min), vol_max)

        # Normalize to broker's allowed volume step
        min_d = Decimal(str(vol_min))
        step_d = Decimal(str(vol_step))
        vol_d = Decimal(str(clamped))
        steps = ((vol_d - min_d) / step_d).to_integral_value(ROUND_HALF_UP)
        normalized = min(max(min_d + steps * step_d, min_d), Decimal(str(vol_max)))

        return float(normalized)

    # ── Trade execution ────────────────────────────────────────────

    def place_trade(self, trade):
        symbol = BREAKOUT_SYMBOL
        tt = trade["type"]
        sl = trade["sl"]
        tp = trade["tp"]

        key = self._signal_key(trade)
        if key in self._executed_signals:
            return

        spread_ok, spread = self._check_spread(symbol)
        if not spread_ok:
            return

        self.risk_mgr.open_positions = self.get_open_positions_count()

        ok, reason = self.risk_mgr.can_trade()
        if not ok:
            return

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return

        cp = tick.ask if tt == "BUY" else tick.bid

        # Never send a stop that price has already passed: the broker rejects it,
        # and a rejected order would leave the stats we now track incomplete.
        if (tt == "BUY" and cp <= sl) or (tt == "SELL" and cp >= sl):
            print(f"Skipped {tt}: price {cp:.2f} already through SL {sl:.2f}")
            return

        # Recalculate TP from live entry to preserve intended risk:reward
        if tt == "BUY":
            risk = cp - sl
            tp = cp + risk * 1.5 if risk > 0 else tp
        else:
            risk = sl - cp
            tp = cp - risk * 1.5 if risk > 0 else tp

        lots = self._calculate_position_size(symbol, cp, sl)

        # Skip duplicate same-direction positions within $5
        existing = mt5.positions_get(symbol=symbol)
        if existing:
            for pos in existing:
                is_same_type = (
                    pos.type == mt5.POSITION_TYPE_BUY and tt == "BUY"
                ) or (
                    pos.type == mt5.POSITION_TYPE_SELL and tt == "SELL"
                )
                if is_same_type and abs(pos.price_open - cp) < 5.0:
                    return

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if tt == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": cp,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 123456,
            "comment": f"IronScalper_{trade['strategy']}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self._executed_signals.add(key)
            self.risk_mgr.register_trade()
            self.risk_mgr.open_positions = self.get_open_positions_count()
            self._save_state()

            position_id = self._resolve_position_id(res)

            try:
                log_trade_open(
                    res.order, symbol, tt, trade["strategy"],
                    cp, sl, tp, lots,
                    self._server_now().strftime("%Y-%m-%d %H:%M:%S"),
                    position_id=position_id,
                )
            except Exception:
                pass

            print(
                f"TRADE: {tt} {lots} lots @ {cp:.2f} SL:{sl:.2f} TP:{tp:.2f} "
                f"Spread:{spread:.0f}pips Position:{position_id}"
            )
        else:
            print(f"Failed: {res.comment}")

    def _resolve_position_id(self, result):
        """Return the MT5 position ID created by a filled order."""
        deal_ticket = getattr(result, "deal", 0) or 0
        if deal_ticket:
            try:
                deals = mt5.history_deals_get(ticket=deal_ticket)
            except Exception:
                deals = None
            if deals:
                position_id = getattr(deals[0], "position_id", 0)
                if position_id:
                    return position_id
        return getattr(result, "order", 0) or getattr(result, "deal", 0)

    # ── Signal generation ──────────────────────────────────────────

    def _generate_signals(self, df):
        """Return signals, recomputed once per closed bar instead of every 5 minutes."""
        last_bar_time = df["time"].iloc[-1]
        if self._cached_signals and last_bar_time == self._last_bar_time:
            return self._cached_signals

        upcoming = []
        server_now = self._server_now()
        offset = self._display_offset()

        if BREAKOUT_ENABLED:
            for t in DailyBreakout(df).generate():
                if t["time"].date() == server_now.date():
                    t["time_str"] = (t["time"] + offset).strftime("%H:%M")
                    t["alert"] = f"Watch for {t['type']} at {t['time_str']} SAST"
                    upcoming.append(t)
                    self._log_signal_once(t, BREAKOUT_SYMBOL, "BREAKOUT")

        if SCALPER_ENABLED:
            for t in LondonScalper(df).generate():
                if self._signal_age_seconds(t, server_now) > -600:
                    t["time_str"] = (t["time"] + offset).strftime("%H:%M")
                    t["alert"] = f"Watch for {t['type']} at {t['time_str']} SAST"
                    upcoming.append(t)
                    self._log_signal_once(t, SCALPER_SYMBOL, "SCALPER")

        self._cached_signals = upcoming
        self._last_bar_time = last_bar_time
        return upcoming

    def _log_signal_once(self, trade, symbol, strategy):
        """Store a signal once; the old code re-inserted it on every cycle."""
        key = self._signal_key(trade)
        if key in self._logged_signal_keys:
            return
        self._logged_signal_keys.add(key)
        try:
            log_signal(
                symbol, strategy, trade["type"], trade["entry"], trade["sl"], trade["tp"],
                trade["time"].strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            pass

    # ── Stats ──────────────────────────────────────────────────────

    def _refresh_stats(self):
        """Publish real DB statistics so the dashboard stops showing zeros."""
        if not self.web_state:
            return
        try:
            stats = get_trade_stats()
            self.web_state["total_trades"] = stats["total_trades"]
            self.web_state["wins"] = stats["wins"]
            self.web_state["losses"] = stats["losses"]
            self.web_state["win_rate"] = stats["win_rate"]
            self.web_state["closed_pnl"] = stats["total_usd"]
            self.web_state["total_pips"] = stats["total_pips"]
        except Exception:
            pass

        try:
            history = []
            for trade in get_recent_trades(60):
                if trade["status"] != "CLOSED":
                    continue
                history.append({
                    "time": trade["close_time"] or trade["open_time"],
                    "type": trade["trade_type"],
                    "entry": trade["entry_price"],
                    "exit": trade["exit_price"] or 0,
                    "pips": trade["profit_pips"] or 0,
                    "win": (trade["profit_pips"] or 0) > 0,
                })
            self.web_state["trade_history"] = history
        except Exception:
            pass

    # ── Closed trade reconciliation ────────────────────────────────

    def _check_closed_positions(self):
        """Match closing deals to open trades by MT5 position ID.

        A closing deal's `order` is the exit order, not the entry order, so
        matching on it never found the trade and nothing was ever reconciled.
        """
        # Query a wide window on the broker clock so a closing deal is never
        # missed because of a local/server time offset.
        server_now = self._server_now()
        day_start = server_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        deals = mt5.history_deals_get(day_start, server_now + timedelta(days=1))
        if not deals:
            return

        for deal in deals:
            if getattr(deal, "entry", None) not in self._CLOSING_ENTRIES:
                continue

            position_id = getattr(deal, "position_id", 0)
            if not position_id:
                continue

            try:
                trade = get_open_trade_by_position(position_id)
            except Exception:
                continue

            if trade is None:
                continue

            close_price = getattr(deal, "price", None)
            close_ts = getattr(deal, "time", None)
            if close_price is None or close_ts is None:
                continue

            profit_usd = (
                (getattr(deal, "profit", 0.0) or 0.0)
                + (getattr(deal, "swap", 0.0) or 0.0)
                + (getattr(deal, "commission", 0.0) or 0.0)
            )

            if trade["trade_type"] == "BUY":
                profit_pips = (close_price - trade["entry_price"]) * 100
            else:
                profit_pips = (trade["entry_price"] - close_price) * 100

            close_time = _server_timestamp(close_ts).strftime("%Y-%m-%d %H:%M:%S")
            try:
                log_trade_close(trade["ticket"], close_price, profit_pips, profit_usd, close_time)
            except Exception:
                continue

            # Feed the realised result in so the daily-loss and consecutive-loss
            # brakes can actually trip.
            self.risk_mgr.register_close(profit_usd)
            self._save_state()
            self._refresh_stats()

            print(
                f"CLOSED #{trade['ticket']} {profit_usd:+.2f} USD "
                f"| day {self.risk_mgr.daily_pnl:+.2f} "
                f"| streak {self.risk_mgr.consecutive_losses}"
            )

    # ── Main loop ──────────────────────────────────────────────────

    def run_web(self):
        print("Bot running...")
        while not self._stop:
            try:
                self._check_closed_positions()
                df = self.get_data(BREAKOUT_SYMBOL, mt5.TIMEFRAME_M5, 2000)

                if df is not None:
                    signals = self._generate_signals(df)

                    if self.web_state:
                        self.web_state["upcoming"] = [
                            {
                                "strategy": s.get("strategy", "?"),
                                "type": s["type"],
                                "entry": round(s["entry"], 2),
                                "sl": round(s["sl"], 2),
                                "tp": round(s["tp"], 2),
                                "time": s.get("time_str", "--:--"),
                                "alert": s.get("alert", ""),
                            }
                            for s in signals[-5:]
                        ]

                        positions = mt5.positions_get(symbol=BREAKOUT_SYMBOL)
                        open_pos = []
                        if positions:
                            for p in positions:
                                pt = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                                open_pos.append({
                                    "type": pt,
                                    "entry": p.price_open,
                                    "sl": p.sl if p.sl > 0 else 0,
                                    "tp": p.tp if p.tp > 0 else 0,
                                    "profit": round(p.profit, 2),
                                    "strategy": p.comment if p.comment else "Manual",
                                })
                        self.web_state["open_positions"] = open_pos

                        tick = mt5.symbol_info_tick(BREAKOUT_SYMBOL)
                        if tick:
                            self.web_state["bid"] = tick.bid
                            self.web_state["ask"] = tick.ask

                        a = mt5.account_info()
                        if a:
                            self.web_state["balance"] = a.balance

                        self.web_state["daily_trades"] = self.risk_mgr.daily_trades
                        self.web_state["daily_pnl"] = round(self.risk_mgr.daily_pnl, 2)
                        self.web_state["consecutive_losses"] = self.risk_mgr.consecutive_losses
                        self._refresh_stats()

                        if self.socketio:
                            self.socketio.emit("state_update", self.web_state)

                    # Act on the signal the moment its bar closes: no 30s floor and
                    # no waiting for the next 10 second poll.
                    server_now = self._server_now()
                    for t in signals:
                        age = self._signal_age_seconds(t, server_now)
                        if 0 <= age <= MAX_SIGNAL_AGE_SECONDS:
                            self.place_trade(t)

                time.sleep(LOOP_SECONDS)

            except Exception as e:
                print(f"Error: {e}")
                self._save_state()
                time.sleep(30)

        self._save_state()
        mt5.shutdown()
        print("Bot stopped.")