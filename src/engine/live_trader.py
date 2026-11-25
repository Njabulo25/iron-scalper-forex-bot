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
    get_prediction,
    get_signal,
    init_db,
    link_prediction_to_trade,
    log_execution,
    log_prediction,
    log_risk_event,
    log_signal,
    log_system_event,
    log_trade_close,
    log_trade_open,
    mark_signal_traded,
    record_trading_settings,
    refresh_strategy_performance,
    resolve_prediction,
    set_active_model,
)
from src.ml.features import Bars, FEATURE_NAMES, SIGNAL_FEATURES, build_features
from src.ml.gate import MLGate, Prediction
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

        # ML gate: loaded only when configured. When enabled, a model that
        # cannot be loaded blocks entries instead of silently reverting to
        # ungated trading - a configuration that says "filter" must not trade
        # unfiltered because of a missing file.
        self.gate = None
        self._ml_blocked = False
        self._ml_logged = set()
        self._ml_seen = 0
        self._ml_skipped = 0
        self._ml_last = None
        self._feature_bar = None
        self._feature_positions = {}
        self._ml_status = {
            "enabled": bool(self.config.ml.enabled),
            "active": False,
            "threshold": self.config.ml.confidence_min,
            "reason": "disabled in config/settings.py",
            "model": {},
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
        self._record_startup(snapshot)
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
        self._load_ml()

    # ── Machine-learning gate ──────────────────────────────────────

    def _load_ml(self):
        """Load the configured model; fail closed when it is missing."""
        if not self.config.ml.enabled:
            self._publish_ml()
            return

        path = Path(self.config.ml.model_path)
        if not path.exists():
            self._ml_blocked = True
            self._ml_status.update({
                "active": False,
                "reason": f"model {path} not found - train it with train_ml.py",
            })
            self.log.error(
                "ml.enabled is on but %s does not exist; entries are blocked "
                "until a model is trained (train_ml.py --train)", path,
            )
            self._publish_ml()
            return

        try:
            self.gate = MLGate.load(path, threshold=self.config.ml.confidence_min)
        except Exception as error:
            self._ml_blocked = True
            self._ml_status.update({
                "active": False, "reason": f"model could not be loaded: {error}",
            })
            self.log.exception("ml.enabled is on but the model cannot be loaded")
            self._publish_ml()
            return

        missing = set(FEATURE_NAMES) | set(SIGNAL_FEATURES)
        missing -= set(self.gate.model.feature_names)
        if missing:
            self._ml_blocked = True
            self._ml_status.update({
                "active": False,
                "reason": "model is missing features: " + ", ".join(sorted(missing)),
            })
            self.log.error("ML model feature mismatch: missing %s", sorted(missing))
            self._publish_ml()
            return

        self._ml_status.update({
            "active": True,
            "reason": "",
            "model": self.gate.summary(),
        })
        self._db_safe("activate model", set_active_model, self.gate.version)
        self._db_safe(
            "model load event", log_system_event,
            f"ML gate active with {self.gate.version}",
            level="info", category="ml",
            detail={"threshold": self.gate.threshold},
        )
        self.log.info(
            "ML gate active: %s at threshold %.2f (%d features)",
            self.gate.version, self.gate.threshold,
            len(self.gate.model.feature_names),
        )
        self._publish_ml()

    def _db_safe(self, action, function, *args, **kwargs):
        """Write to the database without letting a locked file stop trading."""
        try:
            return function(*args, **kwargs)
        except Exception:
            self.log.exception("Database write failed: %s", action)
            return None

    def _record_startup(self, snapshot):
        """Record the configuration this run was built with, and the start event.

        ``trading_settings`` holds immutable snapshots, never the live values: a
        trade can always be traced back to the settings the engine started with.
        """
        self._db_safe(
            "trading settings", record_trading_settings, self.config.snapshot(),
            source="config/settings.py",
            note=f"engine start {self._today()}",
        )
        identity = self._readiness.get("account") or {}
        self._db_safe(
            "startup event", log_system_event,
            "Trading engine started",
            level="info", category="lifecycle",
            detail={
                "symbol": self.config.mt5.symbol,
                "login": identity.get("login"),
                "server": identity.get("server"),
                "balance": getattr(snapshot.account, "balance", None),
                "ml_enabled": self.config.ml.enabled,
            },
        )

    def _publish_ml(self):
        if self.web_state is None:
            return
        payload = dict(self._ml_status)
        payload.update({
            "ml_blocked": self._ml_blocked,
            "ml_seen": self._ml_seen,
            "ml_skipped": self._ml_skipped,
            "ml_last": self._ml_last or {},
        })
        with self._state_lock:
            self.web_state["ml"] = payload

    def _feature_row(self, trade, frame):
        """Features at the bar that produced ``trade``, or ``None``.

        The row is built from the same DataFrame the strategy read, keyed by
        that bar's own timestamp, so a signal a few bars old is scored with its
        own features rather than with the newest bar's.
        """
        if frame is None or not len(frame):
            return None

        token = frame["time"].iloc[-1]
        if token != self._feature_bar:
            self._feature_bar = token
            try:
                bars = Bars.from_columns(frame, symbol=self.config.mt5.symbol)
                self._feature_values = build_features(bars, self.config)
                self._feature_positions = {
                    value: index for index, value in enumerate(frame["time"])
                }
            except Exception:
                self.log.exception("Could not build ML features")
                self._feature_values = None
                self._feature_positions = {}

        if not getattr(self, "_feature_values", None):
            return None

        index = self._feature_positions.get(trade["time"])
        if index is None:
            return None

        row = {
            name: float(self._feature_values[name][index]) for name in FEATURE_NAMES
        }
        row["strategy_scalper"] = 1.0 if trade.get("strategy") == "SCALPER" else 0.0
        row["side_buy"] = 1.0 if trade["type"] == "BUY" else 0.0
        return row

    def _ml_decision(self, trade, frame):
        """Ask the gate about one signal. Returns ``(allowed, prediction)``.

        This function only produces a decision. No order is sent here and the
        gate holds no broker reference; an allowed signal continues into
        ``place_trade`` where the spread, risk and volume checks still apply.
        """
        if self._ml_blocked:
            return False, Prediction(
                False, 0.0, self.config.ml.confidence_min,
                self._ml_status.get("reason") or "ML gate unavailable", "none",
            )
        if self.gate is None:
            return True, None

        row = self._feature_row(trade, frame)
        if row is None:
            prediction = Prediction(
                False, 0.0, self.config.ml.confidence_min,
                "no feature row for this signal bar", self.gate.version,
            )
        else:
            prediction = self.gate.decide(row)

        key = self._signal_key(trade)
        self._ml_seen += 1
        if key not in self._ml_logged:
            self._ml_logged.add(key)
            try:
                log_prediction(
                    key, prediction.model_version, trade.get("strategy", "?"),
                    self.config.mt5.symbol, trade["type"],
                    trade["time"].strftime("%Y-%m-%d %H:%M:%S"),
                    prediction.probability, prediction.threshold,
                    "taken" if prediction.taken else "skipped",
                    features=row,
                )
            except Exception:
                self.log.exception("Could not log the ML prediction")

        if not prediction.taken:
            self._ml_skipped += 1
            self.log.info(
                "ML gate skipped %s %s: %s", trade.get("strategy", "?"),
                trade["type"], prediction.reason,
            )

        self._ml_last = {
            "strategy": trade.get("strategy", "?"),
            "type": trade["type"],
            "probability": prediction.probability,
            "threshold": prediction.threshold,
            "taken": prediction.taken,
            "time": trade["time"].strftime("%Y-%m-%d %H:%M"),
        }
        self._publish_ml()
        return prediction.taken, prediction

    def _r_multiple(self, trade, exit_price):
        """Realised result in units of the trade's own initial risk."""
        entry = float(trade["entry_price"])
        stop = float(trade["sl"] or 0)
        if trade["trade_type"] == "BUY":
            risk = entry - stop
            move = exit_price - entry
        else:
            risk = stop - entry
            move = entry - exit_price
        if risk <= 0:
            return 0.0
        return move / risk

    def _settle_prediction(self, trade, exit_price, net):
        """Attach a closed trade's realised R to the prediction that allowed it."""
        try:
            multiple = self._r_multiple(trade, exit_price)
            resolve_prediction(
                trade["ticket"], multiple, net, "WIN" if net >= 0 else "LOSS"
            )
        except Exception:
            self.log.exception("Could not settle the ML prediction for %s", trade["ticket"])

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
                log_trade_close(
                    trade["ticket"], price, pips, net, close_time,
                    r_multiple=self._r_multiple(trade, price),
                    close_reason="external",
                )
                self._settle_prediction(trade, price, net)
                refresh_strategy_performance(close_time[:10])
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
            self._db_safe(
                "permission event", log_risk_event,
                "entry_not_permitted", why, severity="warning",
                strategy=trade.get("strategy"), blocked=True,
            )
            return None

        spread_ok, spread = self._spread_ok(snapshot.tick)
        if not spread_ok:
            self._db_safe(
                "spread event", log_risk_event,
                "spread_limit",
                f"spread {spread:.0f} points above the "
                f"{self.config.execution.max_spread_points:.0f} point limit",
                severity="info", strategy=trade.get("strategy"),
                detail={"spread_points": round(spread, 1)}, blocked=True,
            )
            return None

        # Balance and open positions come from MT5 on every entry, so a deposit
        # or a close the reconciliation has not seen cannot stale the sizing.
        self.risk_mgr.current_balance = snapshot.account.balance
        self.risk_mgr.open_positions = snapshot.position_count()
        allowed, reason = self.risk_mgr.can_trade()
        if not allowed:
            self._block(reason, f"Entry skipped: {reason}")
            self._db_safe(
                "risk event", log_risk_event,
                "risk_limit", reason, severity="warning",
                strategy=trade.get("strategy"), blocked=True,
                detail={
                    "daily_pnl": round(self.risk_mgr.daily_pnl, 2),
                    "daily_trades": self.risk_mgr.daily_trades,
                    "open_positions": self.risk_mgr.open_positions,
                    "consecutive_losses": self.risk_mgr.consecutive_losses,
                },
            )
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
            self._db_safe(
                "invalid stop event", log_risk_event,
                "invalid_order", explanation, severity="warning",
                strategy=trade.get("strategy"), blocked=True,
                detail={"entry": entry, "sl": stop_loss, "tp": take_profit},
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
            self._db_safe(
                "invalid volume event", log_risk_event,
                "invalid_volume", sized_why, severity="warning",
                strategy=trade.get("strategy"), blocked=True,
                detail={"lots": lots},
            )
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

        started = time.monotonic()
        result = self.broker.send_order(request, side)
        latency_ms = int((time.monotonic() - started) * 1000)
        if not result.ok:
            self.log.error(
                "Order not filled for %s: retcode=%s %s",
                key, result.retcode, result.comment,
            )
            self._db_safe(
                "rejected order event", log_system_event,
                f"order rejected: {result.comment or result.retcode}",
                level="error", category="order",
                detail={"signal": key, "retcode": result.retcode,
                        "attempts": result.attempts, "latency_ms": latency_ms},
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
            trade_row_id = log_trade_open(
                result.order, self.config.mt5.symbol, side, trade.get("strategy", "?"),
                fill_price, fill_sl, fill_tp, fill_lots,
                self._server_now().strftime("%Y-%m-%d %H:%M:%S"),
                position_id=position_id, magic=self.config.mt5.magic,
            )
            # The execution row is what lets a fill be audited later: what was
            # requested, what came back, how long the terminal took.
            if trade_row_id:
                log_execution(
                    trade_row_id, position_id=position_id, order_ticket=result.order,
                    deal_ticket=result.deal, requested_price=entry,
                    filled_price=fill_price, requested_volume=lots,
                    filled_volume=fill_lots, spread_points=spread,
                    retcode=result.retcode, attempts=result.attempts,
                    latency_ms=latency_ms, comment=result.comment,
                    executed_at=self._server_now(),
                )
            # The gate's decision now has a real fill behind it, so monitoring
            # can join the prediction to the trade's eventual result.
            link_prediction_to_trade(key, trade_row_id)
            signal_symbol = (
                self.config.breakout.symbol
                if trade.get("strategy") == "BREAKOUT" else self.config.scalper.symbol
            )
            signal = get_signal(signal_symbol, trade.get("strategy", "?"), side, trade["time"])
            if signal is not None:
                prediction = get_prediction(key)
                mark_signal_traded(
                    signal["id"], trade_row_id, prediction["id"] if prediction else None
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
                timeframe=self.config.scalper.timeframe,
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
                    trade["ticket"], close_price, profit_pips, group["net"], close_time,
                    r_multiple=self._r_multiple(trade, close_price),
                    close_reason="tp/sl",
                )
                self._settle_prediction(trade, close_price, group["net"])
                refresh_strategy_performance(close_time[:10])
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
            self._db_safe(
                "reconnect event", log_system_event, "MT5 connection restored",
                level="info", category="connection",
                detail={"reconnects": self.broker.reconnects},
            )
        elif not snapshot.connected and self._was_connected:
            self.log.warning("MT5 connection lost: %s", snapshot.error or "unknown")
            self._db_safe(
                "disconnect event", log_system_event, "MT5 connection lost",
                level="warning", category="connection",
                detail={"error": snapshot.error or "unknown"},
            )
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
                    if not 0 <= age <= self.config.execution.max_signal_age_seconds:
                        continue
                    allowed, _ = self._ml_decision(trade, frame)
                    if not allowed:
                        continue
                    # The gate can only remove signals. Execution still goes
                    # through place_trade -> spread -> risk -> broker.
                    self.place_trade(trade)

                time.sleep(self.config.execution.loop_seconds)

            except Exception as error:
                self.log.exception("Loop pass failed; continuing")
                self._db_safe(
                    "loop error event", log_system_event,
                    f"loop pass failed: {error}", level="error", category="loop",
                )
                self._save_state()
                time.sleep(30)

        self._save_state()
        self.broker.disconnect()
        self.log.info("Trading loop stopped")

    def stop(self):
        self._stop = True
        self._save_state()
