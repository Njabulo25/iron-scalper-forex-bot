"""Live trading loop for Iron Scalper.

Layers, and who is allowed to do what:

    strategies/          pure signal generation, no I/O
    strategies/risk_*    pure sizing and brakes, no I/O
    engine/broker        all MetaTrader 5 I/O: connection, data, orders, reconciliation reads
    engine/database      all SQLite I/O
    this module          orchestration only: read -> signal -> risk -> execute -> reconcile

The loop is the only place these are wired together. It publishes one
consistent state snapshot per pass to the dashboard, it keeps running when MT5
disappears instead of dying on an exception, and it periodically reconciles MT5
against the database so a position opened or closed outside the bot is noticed
within one sync interval.
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import CONFIG
from src.engine.broker import Broker, server_timestamp
from src.engine.database import (
    get_dashboard_snapshot,
    get_day_state,
    get_open_trade_by_position,
    get_open_trades,
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

COMMENT_PREFIX = "IronScalper_"

_LOG_CONFIGURED = False


def _utc_now():
    """This PC's clock in UTC, used only before the broker clock is available."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _configure_logging():
    """Send the pipeline's log to a rotating file as well as the console."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return

    logger = logging.getLogger("iron")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
        )
        Path("logs").mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(
            "logs/trader.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    _LOG_CONFIGURED = True


class LiveTrader:
    # MT5 deal entry types that mean the deal closed (or reduced) a position.
    _CLOSING_ENTRIES = (
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
    )

    def __init__(self, web_state=None, socketio=None, state_lock=None, config=None):
        _configure_logging()
        self.log = logging.getLogger("iron.trader")

        self.config = config or CONFIG
        self.web_state = web_state
        self.socketio = socketio
        self._state_lock = state_lock or threading.Lock()
        self._stop = False

        self._executed_signals = set()
        self._cached_signals = []
        self._last_bar_time = None
        self._last_history_scan = 0.0
        self._last_position_sync = 0.0
        self._last_block_log = {}
        self._was_connected = None
        self._seen_reconnects = 0
        self._readiness = {
            "ok": False, "issues": ["not checked yet"],
            "terminal": {}, "account": {}, "symbol": {},
        }

        self._load_state()

        risk = self.config.risk
        self.risk_mgr = RiskManager(
            risk.starting_balance, risk.risk_per_trade, risk.max_daily_loss_pct,
            risk.max_daily_trades, risk.max_open_positions, risk.max_consecutive_losses,
        )

        session = self.config.mt5
        self.broker = Broker(
            symbol=session.symbol,
            magic=session.magic,
            deviation=session.deviation_points,
            retry_attempts=session.retry_attempts,
            reconnect_seconds=session.reconnect_seconds,
            max_backoff_seconds=session.max_backoff_seconds,
            terminal_path=session.terminal_path,
            expected_login=session.expected_login,
            expected_server=session.expected_server,
            require_algo_trading=session.require_algo_trading,
            logger=logging.getLogger("iron.broker"),
        )

        if not self.broker.connect():
            raise RuntimeError(
                f"MT5 not available: {self.broker.last_error or 'connection failed'}"
            )

        try:
            init_db()
        except Exception:
            self.log.exception("Schema initialisation failed; continuing without it")

        snapshot = self.broker.poll()
        account = snapshot.account
        if account is not None:
            self.risk_mgr.current_balance = account.balance
            self.risk_mgr.initial_balance = account.balance

        self._restore_day_state(account)
        self.risk_mgr.open_positions = snapshot.position_count()

        if self.web_state is not None:
            self.web_state["starting_balance"] = self.risk_mgr.day_start_balance

        self._readiness = self.broker.readiness()
        self._publish_config()
        self._sync_positions(snapshot, force=True)
        self._publish_connection(snapshot)

        identity = self._readiness.get("account") or {}
        terminal = self._readiness.get("terminal") or {}
        self.log.info(
            "MT5 ready: account %s on %s | terminal %s | balance %.2f %s | "
            "positions %d | restored signals %d",
            identity.get("login", "?"), identity.get("server", "?"),
            terminal.get("name", "?"), self.risk_mgr.current_balance,
            identity.get("currency", ""), self.risk_mgr.open_positions,
            len(self._executed_signals),
        )
        for issue in self._readiness.get("issues") or []:
            self.log.warning("MT5 is not ready to trade: %s", issue)
        if self.config.ml.enabled:
            # No model exists yet, so the threshold cannot filter anything.
            self.log.warning(
                "ml.enabled is on but no model is implemented; the confidence "
                "gate is not applied"
            )

    # ── State persistence ──────────────────────────────────────────

    def _load_state(self):
        """Load dedupe keys and the saved day state; never fail on a bad file."""
        self._saved_state = {}
        self._executed_signals = set()
        if not STATE_FILE.exists():
            return

        try:
            data = json.loads(STATE_FILE.read_text())
        except Exception:
            # A half-written state file must not stop the bot from starting.
            self.log.warning("State file unreadable; starting with a clean slate")
            return

        self._saved_state = data
        saved_date = data.get("date", "")
        self._executed_signals = set(data.get("executed_signals", []))
        if saved_date not in {str(_utc_now().date()), str(datetime.now().date())}:
            self._executed_signals = set()

    def _save_state(self):
        """Write the state atomically so a crash cannot truncate it."""
        payload = json.dumps({
            "date": str(self._today()),
            "executed_signals": sorted(self._executed_signals),
            "balance": self.risk_mgr.current_balance,
            # Persisted so a mid-day restart cannot reset the daily-loss stop.
            "day_start_balance": self.risk_mgr.day_start_balance,
            "daily_pnl": self.risk_mgr.daily_pnl,
            "daily_trades": self.risk_mgr.daily_trades,
            "consecutive_losses": self.risk_mgr.consecutive_losses,
        }, indent=2)

        temporary = STATE_FILE.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload)
            temporary.replace(STATE_FILE)
        except Exception as error:
            self.log.error("Could not save state: %s", error)

    # ── Clock helpers ──────────────────────────────────────────────

    def _server_now(self):
        """Broker clock, in the same frame as the bar timestamps.

        Comparing this PC's clock with MT5 bar times is what once made signals
        look hours old, so every age and timestamp now uses this one clock.
        """
        return self.broker.server_now()

    def _today(self):
        """Trading day on the broker clock."""
        try:
            return self._server_now().date()
        except Exception:
            return datetime.now().date()

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

    def _restore_day_state(self, account):
        """Rebuild today's risk state so restarting mid-day keeps the brakes armed."""
        today = self._today()
        saved = self._saved_state or {}
        saved_today = saved.get("date") in {str(today), str(datetime.now().date())}
        balance = account.balance if account else self.risk_mgr.current_balance

        try:
            day = get_day_state(today)
        except Exception:
            self.log.exception("Could not read today's realised P/L")
            day = {"realised_usd": 0.0, "trades": 0, "trailing_losses": 0}

        day_pnl = day["realised_usd"]
        day_trades = day["trades"]
        trailing = day["trailing_losses"]

        if not day_trades and saved_today:
            # Nothing closed today in the database: fall back to the saved state.
            day_pnl = float(saved.get("daily_pnl") or 0.0)
            day_trades = int(saved.get("daily_trades") or 0)
            trailing = int(saved.get("consecutive_losses") or 0)

        self.risk_mgr.daily_pnl = day_pnl
        self.risk_mgr.daily_trades = day_trades
        self.risk_mgr.consecutive_losses = trailing
        self.risk_mgr.current_balance = balance

        saved_start = saved.get("day_start_balance") if saved_today else None
        self.risk_mgr.day_start_balance = (
            float(saved_start) if saved_start else balance - day_pnl
        )

        if day_pnl or trailing:
            self.log.info(
                "Restored day state: P/L %+.2f from %d trades, streak %d, day start %.2f",
                day_pnl, day_trades, trailing, self.risk_mgr.day_start_balance,
            )

    # ── Position and pending-order reconciliation ──────────────────

    def _position_strategy(self, position):
        """Which strategy owns a position, judged by the comment MT5 stored."""
        comment = (getattr(position, "comment", "") or "").strip()
        if comment.startswith(COMMENT_PREFIX):
            return comment[len(COMMENT_PREFIX):] or "UNKNOWN"
        return "MANUAL"

    def _sync_positions(self, snapshot, force=False):
        """Reconcile MT5 positions against the database, and refresh readiness.

        Three drifts are possible and each is handled once per sync interval:

        * a row still marked OPEN whose position is gone (closed externally),
        * a position carrying this bot's magic with no row (filled while the
          process was down, or stored before a crash),
        * a position with someone else's magic (opened by hand) - reported to
          the dashboard and counted as exposure, never written into our trades.
        """
        now = time.monotonic()
        interval = self.config.execution.position_sync_seconds
        if not force and now - self._last_position_sync < interval:
            return
        self._last_position_sync = now
        self._readiness = self.broker.readiness()

        try:
            known = {
                trade["position_id"]: trade
                for trade in get_open_trades()
                if trade.get("position_id")
            }
        except Exception:
            self.log.exception("Position sync could not read open trades")
            return

        live = {}
        for position in snapshot.positions:
            if getattr(position, "magic", 0) != self.config.mt5.magic:
                continue
            live[getattr(position, "ticket", 0)] = position

        for position_id, trade in known.items():
            if position_id in live:
                continue
            deals = self.broker.deals_for_position(position_id)
            closing = [
                deal for deal in deals
                if getattr(deal, "entry", None) in self._CLOSING_ENTRIES
            ]
            if not closing:
                self.log.warning(
                    "Trade #%s is OPEN in the database but not in MT5, with no "
                    "closing deal found - left untouched", trade["ticket"],
                )
                continue

            net = sum(
                (getattr(deal, "profit", 0.0) or 0.0)
                + (getattr(deal, "swap", 0.0) or 0.0)
                + (getattr(deal, "commission", 0.0) or 0.0)
                for deal in closing
            )
            volume = sum(getattr(deal, "volume", 0) or 0.0 for deal in closing)
            if volume:
                price = sum(
                    (getattr(deal, "price", 0) or 0) * (getattr(deal, "volume", 0) or 0)
                    for deal in closing
                ) / volume
            else:
                price = getattr(closing[-1], "price", None)
            if price is None:
                continue

            close_time = server_timestamp(
                max(getattr(deal, "time", 0) or 0 for deal in closing)
            ).strftime("%Y-%m-%d %H:%M:%S")
            pips = (
                (price - trade["entry_price"]) * 100
                if trade["trade_type"] == "BUY"
                else (trade["entry_price"] - price) * 100
            )
            try:
                log_trade_close(trade["ticket"], price, pips, net, close_time)
                self.risk_mgr.register_close(net)
                self._save_state()
                self.log.info(
                    "Closed outside the loop: trade #%s settled at %.2f (%+.2f USD)",
                    trade["ticket"], price, net,
                )
            except Exception:
                self.log.exception("Could not settle trade #%s", trade["ticket"])

        for position_id, position in live.items():
            if position_id in known:
                continue
            strategy = self._position_strategy(position)
            try:
                log_trade_open(
                    position_id, self.config.mt5.symbol, self.broker.position_side(position),
                    strategy, getattr(position, "price_open", 0),
                    getattr(position, "sl", 0) or 0, getattr(position, "tp", 0) or 0,
                    getattr(position, "volume", 0),
                    server_timestamp(getattr(position, "time", 0) or 0).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    position_id=position_id,
                )
                self.log.warning(
                    "Adopted untracked position %s (%s) into the database",
                    position_id, strategy,
                )
            except Exception:
                self.log.exception("Could not adopt position %s", position_id)

        stray_orders = [
            order for order in snapshot.orders
            if getattr(order, "magic", 0) == self.config.mt5.magic
        ]
        if stray_orders:
            self.log.warning(
                "%d pending order(s) carry this bot's magic; the bot never places "
                "pending orders, so they are external: %s",
                len(stray_orders),
                ", ".join(str(getattr(order, "ticket", "?")) for order in stray_orders),
            )

    # ── Market data ────────────────────────────────────────────────

    def get_data(self, symbol=None, timeframe=None, bars=2000):
        """Return recent bars as a DataFrame, or ``None`` if the feed is down."""
        symbol = symbol or self.config.mt5.symbol
        timeframe = (
            self.broker.timeframe(self.config.scalper.timeframe)
            if timeframe is None else timeframe
        )
        rates = self.broker.rates(timeframe, bars)
        if rates is None:
            return None
        try:
            frame = pd.DataFrame(rates)
            frame["time"] = pd.to_datetime(frame["time"], unit="s")
            return frame.sort_values("time").reset_index(drop=True)
        except Exception:
            self.log.exception("Could not build the bar frame")
            return None

    def _signal_key(self, trade):
        """Identity of a signal: strategy, side and the bar that produced it."""
        bar = trade["time"].strftime("%Y%m%d_%H%M")
        return f"{trade.get('strategy', '?')}|{trade['type']}|{bar}"

    def _spread_ok(self, tick):
        limit = self.config.execution.max_spread_points
        spread = (tick.ask - tick.bid) * 100
        if spread > limit:
            self.log.info("Spread too wide: %.0f points (max %s)", spread, limit)
            return False, spread
        return True, spread

    def _block(self, reason, message):
        """Log a refusal once a minute instead of on every 2-second pass."""
        now = time.monotonic()
        if now - self._last_block_log.get(reason, 0.0) < 60:
            return
        self._last_block_log[reason] = now
        self.log.info(message)

    def _entry_permission(self, snapshot):
        """Whether the account, terminal and symbol currently allow a new entry."""
        issues = self._readiness.get("issues") or []
        if issues:
            return False, "; ".join(issues)

        stale = self.config.mt5.feed_stale_seconds
        if snapshot.feed_age > stale:
            return False, f"quote feed is {snapshot.feed_age:.0f}s old (max {stale:.0f}s)"
        return True, ""

    # ── Trade execution ────────────────────────────────────────────

    def _duplicate_nearby(self, positions, side, entry):
        guard = self.config.risk.same_direction_guard_usd
        for position in positions:
            if self.broker.position_side(position) != side:
                continue
            opened_at = getattr(position, "price_open", 0) or 0
            if abs(opened_at - entry) < guard:
                self.log.info(
                    "Skipped %s: a similar position is already open at %.2f",
                    side, opened_at,
                )
                return True
        return False

    def place_trade(self, trade):
        """Validate and submit one signal. Every exit path is logged."""
        if self._stop:
            return None

        key = self._signal_key(trade)
        if key in self._executed_signals:
            return None

        snapshot = self.broker.poll()
        if not snapshot.healthy:
            self._block("connection", "Entry skipped: MT5 is unavailable")
            return None

        permitted, why = self._entry_permission(snapshot)
        if not permitted:
            self._block("permission", f"Entry skipped: {why}")
            return None

        spread_ok, spread = self._spread_ok(snapshot.tick)
        if not spread_ok:
            return None

        # Balance and open positions come from MT5 on every entry, so a deposit
        # or a close the reconciliation has not seen cannot stale the sizing.
        self.risk_mgr.current_balance = snapshot.account.balance
        self.risk_mgr.open_positions = snapshot.position_count()
        allowed, reason = self.risk_mgr.can_trade()
        if not allowed:
            self._block(reason, f"Entry skipped: {reason}")
            return None

        side = trade["type"]
        entry = snapshot.tick.ask if side == "BUY" else snapshot.tick.bid
        stop_loss = trade["sl"]
        take_profit = trade.get("tp")

        # The strategy authored this take-profit; it is not recomputed here, so
        # live geometry matches the levels the strategy was measured with.
        symbol_info = self.broker.symbol_info()
        valid, explanation = self.broker.validate_stops(
            side, entry, stop_loss, take_profit, info=symbol_info
        )
        if not valid:
            self.log.warning(
                "Skipped %s %s: %s (entry %.2f sl %.2f tp %s)",
                trade.get("strategy", "?"), side, explanation, entry, stop_loss, take_profit,
            )
            return None

        if self._duplicate_nearby(snapshot.positions, side, entry):
            return None

        lots = self.risk_mgr.size_position(
            snapshot.account.balance, entry, stop_loss, symbol_info
        )
        sized_ok, sized_why = self.broker.validate_volume(lots, info=symbol_info)
        if not sized_ok:
            self.log.error("Skipped %s: %s", side, sized_why)
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.mt5.symbol,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": entry,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": self.config.mt5.deviation_points,
            "magic": self.config.mt5.magic,
            "comment": f"{COMMENT_PREFIX}{trade.get('strategy', '?')}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self.broker.send_order(request, side)
        if not result.ok:
            self.log.error(
                "Order not filled for %s: retcode=%s %s",
                key, result.retcode, result.comment,
            )
            return result

        fill_price = result.price or entry
        self._executed_signals.add(key)
        self._save_state()
        self.risk_mgr.register_open()

        position_id = self.broker.resolve_position_id(result)
        position = self.broker.position_by_ticket(position_id)

        honoured, notes = self.broker.verify_fill(position, lots, stop_loss, take_profit)
        if not honoured:
            self.log.warning(
                "Position %s does not match the request: %s", position_id, "; ".join(notes)
            )
        fill_lots = float(getattr(position, "volume", 0) or lots) if position is not None else lots
        fill_sl = (
            float(getattr(position, "sl", 0) or 0) or stop_loss
        ) if position is not None else stop_loss
        fill_tp = (
            float(getattr(position, "tp", 0) or 0) or take_profit
        ) if position is not None else take_profit

        try:
            log_trade_open(
                result.order, self.config.mt5.symbol, side, trade.get("strategy", "?"),
                fill_price, fill_sl, fill_tp, fill_lots,
                self._server_now().strftime("%Y-%m-%d %H:%M:%S"),
                position_id=position_id,
            )
        except Exception:
            # The position is real in MT5 but has no row. Startup recovery will
            # adopt it; log loudly so it is never invisible.
            self.log.exception(
                "Order %s filled but could not be stored (position %s)",
                result.order, position_id,
            )

        self.log.info(
            "OPEN %s %s %.2f lots @ %.2f sl %.2f tp %s | spread %.0f p | position %s | attempt %d",
            trade.get("strategy", "?"), side, fill_lots, fill_price, fill_sl,
            fill_tp, spread, position_id, result.attempts,
        )
        return result

    # ── Signal generation ──────────────────────────────────────────

    def _generate_signals(self, df):
        """Return signals, recomputed once per closed bar instead of every pass."""
        last_bar_time = df["time"].iloc[-1]
        if self._cached_signals and last_bar_time == self._last_bar_time:
            return self._cached_signals

        upcoming = []
        server_now = self._server_now()
        offset = self._display_offset()

        if self.config.breakout.enabled:
            strategy = DailyBreakout(df, self.config.breakout)
            for trade in strategy.generate():
                if trade["time"].date() == server_now.date():
                    trade["time_str"] = (trade["time"] + offset).strftime("%H:%M")
                    trade["alert"] = f"Watch for {trade['type']} at {trade['time_str']} SAST"
                    upcoming.append(trade)
                    self._store_signal(trade, self.config.breakout.symbol, "BREAKOUT")

        if self.config.scalper.enabled:
            strategy = LondonScalper(df, self.config.scalper)
            for trade in strategy.generate():
                if self._signal_age_seconds(trade, server_now) > -600:
                    trade["time_str"] = (trade["time"] + offset).strftime("%H:%M")
                    trade["alert"] = f"Watch for {trade['type']} at {trade['time_str']} SAST"
                    upcoming.append(trade)
                    self._store_signal(trade, self.config.scalper.symbol, "SCALPER")

        self._cached_signals = upcoming
        self._last_bar_time = last_bar_time
        return upcoming

    def _store_signal(self, trade, symbol, strategy):
        """Persist a signal; SQLite's unique index decides whether it is new."""
        try:
            inserted = log_signal(
                symbol, strategy, trade["type"], trade["entry"], trade["sl"], trade["tp"],
                trade["time"].strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            self.log.exception("Could not store signal")
            return
        if inserted:
            self.log.info(
                "SIGNAL %s %s @ %.2f (sl %.2f tp %.2f)",
                strategy, trade["type"], trade["entry"], trade["sl"], trade["tp"],
            )

    # ── Closed trade reconciliation ────────────────────────────────

    def _check_closed_positions(self, force=False):
        """Settle closed trades from MT5's deal history, one position at a time.

        Deals are grouped by ``position_id`` so a partial close, an INOUT or a
        close-by settles the trade once with its full result, instead of the
        first deal closing the row and the rest being dropped.
        """
        interval = self.config.execution.history_scan_seconds
        if not force and time.monotonic() - self._last_history_scan < interval:
            return

        server_now = self._server_now()
        day_start = server_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        deals = self.broker.deals(day_start, server_now + timedelta(days=1))
        self._last_history_scan = time.monotonic()
        if not deals:
            return

        closed = {}
        for deal in deals:
            if getattr(deal, "entry", None) not in self._CLOSING_ENTRIES:
                continue
            position_id = getattr(deal, "position_id", 0)
            if not position_id:
                continue

            group = closed.setdefault(position_id, {
                "net": 0.0, "notional": 0.0, "volume": 0.0,
                "last_price": None, "last_time": 0,
            })
            group["net"] += (
                (getattr(deal, "profit", 0.0) or 0.0)
                + (getattr(deal, "swap", 0.0) or 0.0)
                + (getattr(deal, "commission", 0.0) or 0.0)
            )
            price = getattr(deal, "price", None)
            if price is not None:
                volume = getattr(deal, "volume", 0) or 0.0
                group["notional"] += price * volume
                group["volume"] += volume
                group["last_price"] = price
            group["last_time"] = max(group["last_time"], getattr(deal, "time", 0) or 0)

        for position_id, group in closed.items():
            try:
                trade = get_open_trade_by_position(position_id)
            except Exception:
                self.log.exception("Could not look up position %s", position_id)
                continue
            if trade is None:
                continue

            close_price = (
                group["notional"] / group["volume"]
                if group["volume"] else group["last_price"]
            )
            if close_price is None or not group["last_time"]:
                continue

            if trade["trade_type"] == "BUY":
                profit_pips = (close_price - trade["entry_price"]) * 100
            else:
                profit_pips = (trade["entry_price"] - close_price) * 100

            close_time = server_timestamp(group["last_time"]).strftime("%Y-%m-%d %H:%M:%S")
            try:
                log_trade_close(
                    trade["ticket"], close_price, profit_pips, group["net"], close_time
                )
            except Exception:
                self.log.exception("Could not close trade #%s", trade["ticket"])
                continue

            self.risk_mgr.register_close(group["net"])
            self._save_state()
            self.log.info(
                "CLOSE #%s %s %+.2f USD | day %+.2f | streak %d",
                trade["ticket"], trade["trade_type"], group["net"],
                self.risk_mgr.daily_pnl, self.risk_mgr.consecutive_losses,
            )

    # ── Dashboard publication ──────────────────────────────────────

    def _publish_config(self):
        """Publish the exact configuration this engine was built with."""
        if self.web_state is None:
            return
        with self._state_lock:
            self.web_state["config"] = self.config.snapshot()

    def _publish_connection(self, snapshot):
        if self.web_state is None:
            return

        if snapshot.connected and self._was_connected is False:
            self.log.info("MT5 connection restored")
        elif not snapshot.connected and self._was_connected:
            self.log.warning("MT5 connection lost: %s", snapshot.error or "unknown")
        self._was_connected = snapshot.connected

        feed_age = round(snapshot.feed_age, 1)
        payload = {
            "mt5_connected": bool(snapshot.connected),
            "mt5_reconnects": self.broker.reconnects,
            "mt5_error": snapshot.error or "",
            "feed_age_seconds": feed_age,
            "feed_stale": feed_age > self.config.mt5.feed_stale_seconds,
            "mt5_ready": bool(self._readiness.get("ok")),
            "mt5_issues": list(self._readiness.get("issues") or []),
            "mt5_terminal": self._readiness.get("terminal") or {},
            "mt5_account": self._readiness.get("account") or {},
            "mt5_symbol": self._readiness.get("symbol") or {},
            "pending_orders": [
                self.broker.order_summary(order) for order in snapshot.orders
            ],
            "manual_positions": len(
                snapshot.foreign_positions(self.config.mt5.magic)
            ),
        }
        with self._state_lock:
            self.web_state.update(payload)

    def _publish_market(self, snapshot, signals):
        if self.web_state is None:
            return

        positions = [
            {
                "ticket": getattr(position, "ticket", 0),
                "type": self.broker.position_side(position),
                "entry": getattr(position, "price_open", 0) or 0,
                "sl": getattr(position, "sl", 0) or 0,
                "tp": getattr(position, "tp", 0) or 0,
                "volume": getattr(position, "volume", 0) or 0,
                "profit": round(getattr(position, "profit", 0) or 0, 2),
                "strategy": self._position_strategy(position),
            }
            for position in snapshot.positions
        ]

        payload = {
            "open_positions": positions,
            "bid": snapshot.tick.bid,
            "ask": snapshot.tick.ask,
            "balance": snapshot.account.balance,
            "equity": getattr(snapshot.account, "equity", snapshot.account.balance),
            "daily_trades": self.risk_mgr.daily_trades,
            "daily_pnl": round(self.risk_mgr.daily_pnl, 2),
            "consecutive_losses": self.risk_mgr.consecutive_losses,
            "upcoming": [
                {
                    "strategy": trade.get("strategy", "?"),
                    "type": trade["type"],
                    "entry": round(trade["entry"], 2),
                    "sl": round(trade["sl"], 2),
                    "tp": round(trade["tp"], 2),
                    "time": trade.get("time_str", "--:--"),
                    "alert": trade.get("alert", ""),
                }
                for trade in signals[-5:]
            ],
        }
        with self._state_lock:
            self.web_state.update(payload)

    def _refresh_stats(self):
        """Publish realised stats and risk headroom; called once per pass."""
        if self.web_state is None:
            return

        try:
            snapshot = get_dashboard_snapshot(60)
        except Exception:
            self.log.exception("Could not build the stats snapshot")
            snapshot = {"stats": {}, "history": []}

        stats = snapshot["stats"]
        risk = self.risk_mgr
        budget = risk.loss_limit()
        used = max(-risk.daily_pnl, 0.0)
        allowed, reason = risk.can_trade()

        payload = {
            "total_trades": stats.get("total_trades", 0),
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "win_rate": stats.get("win_rate", 0.0),
            "closed_pnl": stats.get("total_usd", 0.0),
            "total_pips": stats.get("total_pips", 0.0),
            "trade_history": snapshot["history"],
            "risk_budget": round(budget, 2),
            "risk_used_pct": round((used / budget * 100) if budget else 0.0, 1),
            "risk_reason": reason,
            "trading_allowed": allowed,
            "day_start_balance": round(risk.day_start_balance, 2),
            "starting_balance": round(risk.day_start_balance, 2),
        }
        with self._state_lock:
            self.web_state.update(payload)

    def _emit(self):
        """One socket publish per pass, from a consistent copy of the state."""
        if self.web_state is None or self.socketio is None:
            return
        with self._state_lock:
            payload = dict(self.web_state)
        try:
            self.socketio.emit("state_update", payload)
        except Exception:
            self.log.exception("Could not publish state to the dashboard")

    # ── Main loop ──────────────────────────────────────────────────

    def run_web(self):
        session = self.config.mt5
        self.log.info(
            "Trading loop started (symbol %s, %s bars, %.1fs interval)",
            session.symbol, self.config.scalper.timeframe,
            self.config.execution.loop_seconds,
        )

        while not self._stop:
            try:
                snapshot = self.broker.poll()

                # A restore is a new session: reconcile immediately instead of
                # waiting for the next scheduled sync.
                if self.broker.reconnects != self._seen_reconnects:
                    self._seen_reconnects = self.broker.reconnects
                    self._sync_positions(snapshot, force=True)

                self._publish_connection(snapshot)

                if not snapshot.healthy:
                    self._block("loop-connection", "Waiting for MT5 to come back")
                    self._refresh_stats()
                    self._emit()
                    time.sleep(self.config.execution.loop_seconds)
                    continue

                self._sync_positions(snapshot)
                self._publish_connection(snapshot)
                self._check_closed_positions()

                frame = self.get_data(session.symbol, bars=2000)
                signals = self._generate_signals(frame) if frame is not None else []

                if frame is not None:
                    self._publish_market(snapshot, signals)

                # Stats come from the database, so a data outage must not
                # freeze them: refresh exactly once per pass, either way.
                self._refresh_stats()
                self._emit()

                server_now = self._server_now()
                for trade in signals:
                    age = self._signal_age_seconds(trade, server_now)
                    if 0 <= age <= self.config.execution.max_signal_age_seconds:
                        self.place_trade(trade)

                time.sleep(self.config.execution.loop_seconds)

            except Exception:
                self.log.exception("Loop pass failed; continuing")
                self._save_state()
                time.sleep(30)

        self._save_state()
        self.broker.disconnect()
        self.log.info("Trading loop stopped")

    def stop(self):
        self._stop = True
        self._save_state()
