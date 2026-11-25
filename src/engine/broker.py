"""MetaTrader 5 gateway.

The only module that talks to the terminal. It owns connection health and
reconnection, platform identity, market data, position and order reads, stop and
volume validation, order submission and post-fill verification, so the trading
loop contains no MT5 plumbing at all.

Every public method is defensive: a terminal that has gone away or an API that
is missing returns ``None``/a failed result instead of raising into the loop.
That is what makes the loop able to keep running (and the dashboard able to say
"disconnected") while the terminal comes back.
"""

import logging
import math
from pathlib import Path
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

DONE = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
PLACED = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)

# The broker may answer "come back with a fresh price" — worth one retry.
REQUOTE_CODES = (
    getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
    getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
    getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
)
# Some brokers reject IOC outright; FOK is the other standard market fill.
INVALID_FILL_CODES = (getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030),)
FILLING_FOK = getattr(mt5, "ORDER_FILLING_FOK", 0)

POSITION_TYPE_BUY = getattr(mt5, "POSITION_TYPE_BUY", 0)
ORDER_TYPE_BUY = getattr(mt5, "ORDER_TYPE_BUY", 0)
ORDER_TYPE_SELL = getattr(mt5, "ORDER_TYPE_SELL", 1)

SYMBOL_TRADE_MODE_FULL = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
ACCOUNT_TRADE_MODE_REAL = getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
TRADE_MODE_TEXT = {
    0: "disabled",
    1: "close only",
    2: "long only",
    3: "short only",
    4: "full",
}

PENDING_ORDER_TEXT = {
    2: "BUY_LIMIT",
    3: "SELL_LIMIT",
    4: "BUY_STOP",
    5: "SELL_STOP",
    6: "BUY_STOP_LIMIT",
    7: "SELL_STOP_LIMIT",
}


def server_timestamp(epoch_seconds):
    """Convert an MT5 server epoch to the naive frame used by bar timestamps."""
    return datetime.fromtimestamp(int(epoch_seconds), timezone.utc).replace(tzinfo=None)


def _rounded(value, places=2):
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return value


class MarketSnapshot:
    """One consistent read of the account, the quote, positions and pending orders."""

    __slots__ = ("account", "tick", "positions", "orders", "connected", "feed_age", "error")

    def __init__(self, account, tick, positions, connected, feed_age=0.0, error=None, orders=None):
        self.account = account
        self.tick = tick
        self.positions = positions or []
        self.orders = orders or []
        self.connected = connected
        self.feed_age = feed_age
        self.error = error

    @property
    def healthy(self):
        return bool(self.connected and self.account is not None and self.tick is not None)

    def position_count(self, magic=None):
        if magic is None:
            return len(self.positions)
        return sum(1 for p in self.positions if getattr(p, "magic", 0) == magic)

    def foreign_positions(self, magic):
        """Positions on this symbol that this bot did not open."""
        return [p for p in self.positions if getattr(p, "magic", 0) != magic]


class OrderResult:
    """Normalised outcome of an order attempt."""

    __slots__ = ("ok", "retcode", "comment", "price", "order", "deal", "attempts")

    def __init__(self, ok, retcode, comment="", price=None, order=0, deal=0, attempts=1):
        self.ok = ok
        self.retcode = retcode
        self.comment = comment
        self.price = price
        self.order = order
        self.deal = deal
        self.attempts = attempts

    def __repr__(self):
        return (
            f"OrderResult(ok={self.ok}, retcode={self.retcode}, "
            f"comment={self.comment!r}, price={self.price}, attempts={self.attempts})"
        )


class Broker:
    """MT5 session, platform identity, market data and order execution."""

    def __init__(
        self,
        symbol,
        magic,
        deviation=20,
        retry_attempts=2,
        reconnect_seconds=5,
        max_backoff_seconds=60,
        terminal_path="",
        expected_login=0,
        expected_server="",
        require_algo_trading=True,
        allow_live_trading=False,
        logger=None,
    ):
        self.symbol = symbol
        self.magic = magic
        self.deviation = deviation
        self.retry_attempts = max(int(retry_attempts), 1)
        self.reconnect_seconds = float(reconnect_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.terminal_path = terminal_path or ""
        self.expected_login = int(expected_login or 0)
        self.expected_server = expected_server or ""
        self.require_algo_trading = bool(require_algo_trading)
        self.allow_live_trading = bool(allow_live_trading)
        self._session_login = None
        self._session_server = None

        self.connected = False
        self.reconnects = 0
        self.reconnect_attempts = 0
        self.last_error = None
        self.last_ok = 0.0
        self._backoff = self.reconnect_seconds
        self._last_attempt = -1e9
        self._last_tick_time = None
        self._last_tick_change = None
        self.log = logger or logging.getLogger("iron.broker")

    # ── low-level ──────────────────────────────────────────────────

    def _call(self, name, *args, **kwargs):
        """Call an essential MT5 function, converting any failure into ``None``."""
        function = getattr(mt5, name, None)
        if function is None:
            self.last_error = f"{name} unavailable"
            return None
        try:
            return function(*args, **kwargs)
        except Exception as error:  # the terminal can vanish mid-call
            self.last_error = f"{name}: {error}"
            self.log.warning("MT5 %s failed: %s", name, error)
            return None

    def _optional(self, name, *args, **kwargs):
        """Call an advisory MT5 function; never records an error."""
        function = getattr(mt5, name, None)
        if function is None:
            return None
        try:
            return function(*args, **kwargs)
        except Exception as error:
            self.log.debug("MT5 %s unavailable: %s", name, error)
            return None

    # ── connection and identity ────────────────────────────────────

    def _identity_error(self, account, terminal):
        """Why this terminal must not be traded, or an empty string."""
        login = getattr(account, "login", None)
        server = getattr(account, "server", "") or ""

        if self.expected_login and login != self.expected_login:
            return (
                f"terminal is signed into account {login}, expected {self.expected_login}"
            )
        if self.expected_server and self.expected_server.lower() not in server.lower():
            return (
                f"terminal server {server!r} does not match expected "
                f"{self.expected_server!r}"
            )
        if self._session_login is not None and login != self._session_login:
            return f"terminal account changed during this run (now {login}, started on {self._session_login})"
        if (
            self._session_server is not None
            and server.lower() != self._session_server.lower()
        ):
            return "terminal broker server changed during this run"
        if (
            getattr(account, "trade_mode", None) == ACCOUNT_TRADE_MODE_REAL
            and not self.allow_live_trading
        ):
            return (
                "live-account trading is disabled; explicitly set "
                "IRON_SCALPER_ALLOW_LIVE_TRADING=1 after reviewing the risk settings"
            )
        if terminal is not None:
            if getattr(terminal, "connected", True) is False:
                return "terminal is not connected to its trade server"
            if self.require_algo_trading and getattr(terminal, "trade_allowed", True) is False:
                return (
                    "algorithmic trading is disabled in MetaTrader 5 "
                    "(enable the Algo Trading button and restart the bot)"
                )
            if self.require_algo_trading and getattr(terminal, "tradeapi_disabled", False):
                return (
                    "trading through the external Python API is disabled in MetaTrader 5 "
                    "(Tools > Options > Expert Advisors: clear 'Disable automated trading "
                    "via external Python API')"
                )
            if self.require_algo_trading and getattr(terminal, "tradeapi_disabled", False):
                return (
                    "trading through the external Python API is disabled in MetaTrader 5 "
                    "(Tools > Options > Expert Advisors: clear 'Disable automated trading "
                    "via external Python API')"
                )
        return ""

    def connect(self):
        """Initialise the terminal and verify it is the account we expect."""
        self._last_attempt = time.monotonic()
        if self.terminal_path and not Path(self.terminal_path).is_file():
            self.connected = False
            self.last_error = (
                f"Configured MetaTrader 5 terminal was not found: {self.terminal_path}. "
                "Set mt5.terminal_path to the full path of terminal64.exe."
            )
            return False

        kwargs = {"path": self.terminal_path} if self.terminal_path else {}
        if self.expected_login:
            kwargs["login"] = self.expected_login
        if self.expected_server:
            kwargs["server"] = self.expected_server
        initialize_exception = None
        try:
            initialized = bool(mt5.initialize(**kwargs))
        except Exception as error:
            initialize_exception = error
            initialized = False

        if not initialized:
            self.connected = False
            if initialize_exception is not None:
                self.last_error = f"MT5 initialize() raised an error: {initialize_exception}"
            else:
                self.last_error = self._initialization_error()
            return False

        account = self._call("account_info")
        if account is None:
            self.connected = False
            self.last_error = "account_info() unavailable"
            self.disconnect()
            return False

        terminal = self._optional("terminal_info")
        mismatch = self._identity_error(account, terminal)
        if mismatch:
            self.connected = False
            self.last_error = mismatch
            self.log.error("Refusing this MT5 session: %s", mismatch)
            self.disconnect()
            return False

        select = getattr(mt5, "symbol_select", None)
        if select is not None:
            self._call("symbol_select", self.symbol, True)

        if self._call("symbol_info", self.symbol) is None:
            self.connected = False
            self.last_error = f"symbol {self.symbol} is not available on this account"
            self.disconnect()
            return False

        if self._session_login is None:
            self._session_login = getattr(account, "login", None)
            self._session_server = getattr(account, "server", "") or ""
        self.connected = True
        self.last_ok = time.monotonic()
        self.last_error = None
        self._backoff = self.reconnect_seconds
        return True

    def _initialization_error(self):
        """Return MT5's diagnostic and next step for a failed initialize call."""
        try:
            error = mt5.last_error()
        except Exception as exc:
            return f"MT5 initialize() failed; last_error() was unavailable: {exc}"

        if isinstance(error, (tuple, list)) and len(error) >= 2:
            code, description = error[0], str(error[1])
        else:
            code, description = None, str(error or "no MT5 error details")

        diagnostic = f"MT5 initialize() failed ({code}: {description})"
        if code == -6:
            return (
                diagnostic + ". Verify the trading account number and exact broker server "
                "in MetaTrader 5 (these are different from an MQL5 Community login). If "
                "needed, set IRON_SCALPER_MT5_LOGIN and IRON_SCALPER_MT5_SERVER to select "
                "the saved account. Also confirm the terminal is signed in, and clear "
                "'Disable automated trading via external Python API' under Tools > Options "
                "> Expert Advisors. That setting allows Python to place orders. For a "
                "non-standard installation, set IRON_SCALPER_MT5_PATH to terminal64.exe."
            )
        if code == -10003:
            return (
                diagnostic + ". MT5 could not establish its local IPC connection. Open "
                "the intended terminal once, then retry; check mt5.terminal_path if its "
                "installation is non-standard."
            )
        if code == -10005:
            return (
                diagnostic + ". The terminal did not respond in time. Open MetaTrader 5 "
                "and retry after it finishes loading."
            )
        return diagnostic

    def disconnect(self):
        try:
            mt5.shutdown()
        except Exception:
            pass
        self.connected = False

    def _reconnect(self):
        """One reconnect attempt, rate-limited by an exponential backoff."""
        now = time.monotonic()
        if now - self._last_attempt < self._backoff:
            return False

        self.reconnect_attempts += 1
        self.log.warning(
            "MT5 disconnected (%s); reconnect attempt %d",
            self.last_error or "unknown", self.reconnect_attempts,
        )
        self.disconnect()
        if self.connect():
            self.reconnects += 1
            self.log.info("MT5 reconnected after %d attempt(s)", self.reconnect_attempts)
            return True

        self._backoff = min(self._backoff * 2, self.max_backoff_seconds)
        self.log.error("MT5 reconnect failed; next attempt in %.0fs", self._backoff)
        return False

    def poll(self):
        """Return one consistent account/quote/positions/orders snapshot.

        Reconnects when the terminal is unreachable, so the caller only has to
        check ``snapshot.healthy``.
        """
        self.last_error = None
        if not self.connected:
            self._reconnect()

        account = self._call("account_info")
        tick = self._call("symbol_info_tick", self.symbol)

        if account is not None:
            identity_problem = self._identity_error(account, self._optional("terminal_info"))
            if identity_problem:
                self.connected = False
                self.last_error = identity_problem
                account = None

        if account is None or tick is None:
            missing = [
                name for name, value in (("account", account), ("quote", tick))
                if value is None
            ]
            # Keep a more specific error from _call if it recorded one.
            self.last_error = self.last_error or f"no {' and no '.join(missing)} data"
            self.connected = False
            self._reconnect()
            if self.connected:
                account = self._call("account_info")
                tick = self._call("symbol_info_tick", self.symbol)

        healthy = account is not None and tick is not None
        error = None
        if healthy:
            self.connected = True
            self.last_ok = time.monotonic()
            self.last_error = None
        else:
            error = self.last_error

        if tick is not None:
            tick_time = getattr(tick, "time", 0) or 0
            if tick_time != self._last_tick_time:
                self._last_tick_time = tick_time
                self._last_tick_change = time.monotonic()

        feed_age = (
            time.monotonic() - self._last_tick_change if self._last_tick_change else 0.0
        )

        return MarketSnapshot(
            account=account if healthy else None,
            tick=tick,
            positions=self._optional("positions_get", symbol=self.symbol) or [],
            connected=self.connected,
            feed_age=feed_age,
            error=error,
            orders=self._optional("orders_get", symbol=self.symbol) or [],
        )

    def server_now(self):
        """Broker clock, in the same frame as the bar timestamps."""
        tick = self._call("symbol_info_tick", self.symbol)
        if tick is not None and getattr(tick, "time", 0):
            return server_timestamp(tick.time)
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def readiness(self):
        """Whether a new order may be sent right now, with the evidence for it.

        Returns ``{"ok", "issues", "terminal", "account", "symbol"}``. The
        summaries are the plain values the dashboard displays, so a refusal on
        the dashboard shows the same account and symbol rules the gate applied.
        """
        issues = []
        terminal = self._optional("terminal_info")
        account = self._optional("account_info")
        info = self._optional("symbol_info", self.symbol)
        tick = self._optional("symbol_info_tick", self.symbol)

        if not self.connected:
            issues.append(self.last_error or "terminal is not connected")
        if terminal is not None:
            if getattr(terminal, "connected", True) is False:
                issues.append("terminal lost its trade-server connection")
            if self.require_algo_trading and getattr(terminal, "trade_allowed", True) is False:
                issues.append("algorithmic trading is disabled in the terminal")
            if self.require_algo_trading and getattr(terminal, "tradeapi_disabled", False):
                issues.append("trading through the external Python API is disabled in the terminal")
        else:
            issues.append("terminal information is unavailable")
        if account is None:
            issues.append("account information is unavailable")
        else:
            identity_problem = self._identity_error(account, terminal)
            if identity_problem and identity_problem not in issues:
                issues.append(identity_problem)
            if getattr(account, "trade_allowed", True) is False:
                issues.append("this account is not permitted to trade")

        if info is None:
            issues.append(f"symbol {self.symbol} is unavailable")
        else:
            if getattr(info, "visible", True) is False:
                issues.append(f"symbol {self.symbol} is not visible in Market Watch")
            mode = getattr(info, "trade_mode", None)
            if mode is not None and mode != SYMBOL_TRADE_MODE_FULL:
                issues.append(
                    f"symbol {self.symbol} is {TRADE_MODE_TEXT.get(mode, mode)}"
                )
            if float(getattr(info, "volume_min", 0) or 0) <= 0:
                issues.append(f"symbol {self.symbol} reports no valid minimum volume")

        if tick is None:
            issues.append("no quote is available for the symbol")

        return {
            "ok": not issues,
            "issues": issues,
            "terminal": self.terminal_summary(terminal),
            "account": self.account_summary(account),
            "symbol": self.symbol_summary(info),
        }

    # ── plain summaries for the dashboard ──────────────────────────

    def terminal_summary(self, terminal=None):
        if terminal is None:
            terminal = self._optional("terminal_info")
        if terminal is None:
            return {}
        return {
            "name": getattr(terminal, "name", "") or "",
            "company": getattr(terminal, "company", "") or "",
            "path": getattr(terminal, "path", "") or "",
            "build": getattr(terminal, "build", None),
            "connected": bool(getattr(terminal, "connected", True)),
            "trade_allowed": bool(getattr(terminal, "trade_allowed", True)),
        }

    def account_summary(self, account=None):
        if account is None:
            account = self._optional("account_info")
        if account is None:
            return {}
        return {
            "login": getattr(account, "login", None),
            "server": getattr(account, "server", "") or "",
            "company": getattr(account, "company", "") or "",
            "currency": getattr(account, "currency", "") or "",
            "leverage": getattr(account, "leverage", None),
            "balance": _rounded(getattr(account, "balance", None)),
            "equity": _rounded(getattr(account, "equity", None)),
            "margin_free": _rounded(getattr(account, "margin_free", None)),
            "margin": _rounded(getattr(account, "margin", None)),
            "margin_level": _rounded(getattr(account, "margin_level", None), 1),
            "trade_allowed": bool(getattr(account, "trade_allowed", True)),
            "trade_expert": bool(getattr(account, "trade_expert", True)),
            "trade_mode": getattr(account, "trade_mode", None),
        }

    def symbol_summary(self, info=None):
        if info is None:
            info = self._optional("symbol_info", self.symbol)
        if info is None:
            return {}
        mode = getattr(info, "trade_mode", None)
        return {
            "name": getattr(info, "name", self.symbol) or self.symbol,
            "digits": getattr(info, "digits", None),
            "point": getattr(info, "point", None),
            "contract_size": getattr(info, "trade_contract_size", None),
            "volume_min": getattr(info, "volume_min", None),
            "volume_max": getattr(info, "volume_max", None),
            "volume_step": getattr(info, "volume_step", None),
            "stops_level": getattr(info, "trade_stops_level", None),
            "trade_mode": mode,
            "trade_mode_text": TRADE_MODE_TEXT.get(mode, "unknown"),
            "visible": bool(getattr(info, "visible", True)),
        }

    def order_summary(self, order):
        kind = getattr(order, "type", None)
        return {
            "ticket": getattr(order, "ticket", 0),
            "type": PENDING_ORDER_TEXT.get(kind, str(kind)),
            "symbol": getattr(order, "symbol", self.symbol),
            "volume": _rounded(getattr(order, "volume_current", 0) or 0),
            "price": _rounded(getattr(order, "price_open", 0) or 0),
            "magic": getattr(order, "magic", 0),
        }

    # ── market data ────────────────────────────────────────────────

    def rates(self, timeframe, bars):
        return self._call("copy_rates_from_pos", self.symbol, timeframe, 0, bars)

    def symbol_info(self):
        return self._call("symbol_info", self.symbol)

    def loss_per_lot(self, side, entry, stop_loss):
        """Estimate stop-loss exposure for one lot in the account currency.

        MT5 performs the symbol and deposit-currency conversion. Contract-size
        multiplication alone is wrong for accounts whose deposit currency
        differs from the symbol's quote currency.
        """
        calculator = getattr(mt5, "order_calc_profit", None)
        if calculator is None or side not in {"BUY", "SELL"}:
            return None
        action = ORDER_TYPE_BUY if side == "BUY" else ORDER_TYPE_SELL
        result = self._call(
            "order_calc_profit", action, self.symbol, 1.0,
            float(entry), float(stop_loss),
        )
        try:
            loss = -float(result)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(loss) or loss <= 0:
            return None
        return loss

    def tick(self):
        return self._call("symbol_info_tick", self.symbol)

    def positions(self):
        return self._optional("positions_get", symbol=self.symbol) or []

    def position_by_ticket(self, ticket):
        for position in self.positions():
            if getattr(position, "ticket", 0) == ticket:
                return position
        return None

    def orders(self):
        return self._optional("orders_get", symbol=self.symbol) or []

    def deals(self, start, end):
        return self._call("history_deals_get", start, end) or []

    def deals_for_position(self, position_id):
        return self._call("history_deals_get", position=position_id) or []

    def timeframe(self, name="M5"):
        """Resolve a configured timeframe name such as ``"M5"``."""
        return getattr(mt5, f"TIMEFRAME_{name}", getattr(mt5, "TIMEFRAME_M5", 5))

    # ── order safety ───────────────────────────────────────────────

    def validate_stops(self, side, entry, sl, tp, info=None):
        """Reject geometry the broker would reject, before it is sent."""
        if sl is None or entry is None:
            return False, "missing stop"

        if side == "BUY":
            if sl >= entry:
                return False, "stop loss is not below entry"
            if tp is not None and tp <= entry:
                return False, "take profit is not above entry"
        else:
            if sl <= entry:
                return False, "stop loss is not above entry"
            if tp is not None and tp >= entry:
                return False, "take profit is not below entry"

        info = info if info is not None else self.symbol_info()
        if info is None:
            return True, ""

        point = float(getattr(info, "point", 0) or 0)
        level = float(getattr(info, "trade_stops_level", 0) or 0)
        minimum = point * level
        if minimum > 0:
            if abs(entry - sl) < minimum:
                return False, f"stop closer than the broker minimum ({level:.0f} points)"
            if tp is not None and abs(tp - entry) < minimum:
                return False, f"target closer than the broker minimum ({level:.0f} points)"
        return True, ""

    def validate_volume(self, lots, info=None):
        """Reject a volume the broker would reject, before it is sent."""
        info = info if info is not None else self.symbol_info()
        if info is None:
            return True, ""
        minimum = float(getattr(info, "volume_min", 0) or 0)
        maximum = float(getattr(info, "volume_max", 0) or 0)
        step = float(getattr(info, "volume_step", 0) or 0)
        lots = float(lots)
        if lots <= 0:
            return False, "volume is zero"
        if minimum and lots < minimum - 1e-9:
            return False, f"volume {lots:g} is below the symbol minimum {minimum:g}"
        if maximum and lots > maximum + 1e-9:
            return False, f"volume {lots:g} is above the symbol maximum {maximum:g}"
        if step > 0:
            steps = round(lots / step)
            if abs(steps * step - lots) > 1e-9:
                return False, f"volume {lots:g} is not a multiple of the symbol step {step:g}"
        return True, ""

    def _price_tolerance(self):
        info = self._optional("symbol_info", self.symbol)
        point = float(getattr(info, "point", 0) or 0) if info is not None else 0.0
        return max(point * 2, 1e-9)

    def verify_fill(self, position, lots, sl, tp):
        """Compare the position MT5 actually holds against what was requested.

        Returns ``(ok, notes)``. A broker that silently drops or rounds a stop,
        or fills a different volume, is reported instead of being stored as if
        the request had been honoured.
        """
        if position is None:
            return True, []

        tolerance = self._price_tolerance()
        notes = []

        volume = float(getattr(position, "volume", lots) or lots)
        if abs(volume - float(lots)) > 1e-6:
            notes.append(f"volume {volume:g} instead of {float(lots):g}")

        current_sl = float(getattr(position, "sl", 0) or 0)
        if sl and abs(current_sl - float(sl)) > tolerance:
            notes.append(f"stop loss {current_sl or 'none'} instead of {float(sl):g}")

        current_tp = float(getattr(position, "tp", 0) or 0)
        if tp and abs(current_tp - float(tp)) > tolerance:
            notes.append(f"take profit {current_tp or 'none'} instead of {float(tp):g}")

        return (not notes, notes)

    def _position_tickets(self):
        return {getattr(p, "ticket", 0) for p in self.positions()}

    def _recovered_fill(self, before, request, attempts, note):
        """Resolve an unknown outcome by looking for a position MT5 now holds.

        A terminal that dies between sending and answering may still have
        filled the order. Reading positions before retrying is what stops a
        retry from opening a second position.
        """
        candidates = self._position_tickets() - before
        for position in self.positions():
            ticket = getattr(position, "ticket", 0)
            if ticket not in candidates:
                continue
            if getattr(position, "symbol", self.symbol) != request.get("symbol", self.symbol):
                continue
            if getattr(position, "magic", None) != request.get("magic"):
                continue
            if getattr(position, "type", None) != request.get("type"):
                continue
            self.log.warning(
                "Order outcome was unknown (%s); MT5 now holds position %s", note, ticket
            )
            return OrderResult(
                True, None, f"{note} (position recovered)", request.get("price"),
                ticket, 0, attempts,
            )
        return None

    def send_order(self, request, side):
        """Submit a market order, retrying once on a requote or fill-mode rejection.

        Never sends a second position when the first attempt's outcome is
        unknown: an exception or an empty answer is resolved by reading MT5.
        Returns an :class:`OrderResult`; never raises.
        """
        attempts = 0
        pending = dict(request)
        before = self._position_tickets()

        while attempts < self.retry_attempts:
            attempts += 1

            checker = getattr(mt5, "order_check", None)
            if checker is None:
                return OrderResult(False, None, "order_check unavailable", attempts=attempts)
            try:
                check = checker(pending)
            except Exception as error:
                return OrderResult(False, None, f"order_check failed: {error}", attempts=attempts)
            if check is None:
                return OrderResult(False, None, "order_check returned no result", attempts=attempts)
            if getattr(check, "retcode", None) != 0:
                return OrderResult(
                    False, getattr(check, "retcode", None),
                    f"order_check: {getattr(check, 'comment', '')}", attempts=attempts,
                )

            try:
                result = mt5.order_send(pending)
            except Exception as error:
                self.last_error = f"order_send: {error}"
                self.log.error("order_send raised: %s", error)
                recovered = self._recovered_fill(before, pending, attempts, str(error))
                if recovered is not None:
                    return recovered
                return OrderResult(False, None, str(error), attempts=attempts)

            if result is None:
                self.last_error = "order_send() returned None"
                self.log.error("order_send() returned None")
                recovered = self._recovered_fill(
                    before, pending, attempts, "order_send returned None"
                )
                if recovered is not None:
                    return recovered
                return OrderResult(False, None, "order_send returned None", attempts=attempts)

            code = getattr(result, "retcode", None)
            if code in (DONE, PLACED):
                price = getattr(result, "price", 0) or pending.get("price")
                return OrderResult(
                    True, code, getattr(result, "comment", ""), price,
                    getattr(result, "order", 0), getattr(result, "deal", 0), attempts,
                )

            if code in REQUOTE_CODES and attempts < self.retry_attempts:
                tick = self.tick()
                if tick is not None:
                    pending["price"] = tick.ask if side == "BUY" else tick.bid
                self.log.warning("Requote (%s); retrying at %s", code, pending.get("price"))
                continue

            if code in INVALID_FILL_CODES and attempts < self.retry_attempts:
                pending["type_filling"] = FILLING_FOK
                self.log.warning("Fill mode rejected (%s); retrying with FOK", code)
                continue

            self.last_error = f"order rejected: {getattr(result, 'comment', code)}"
            self.log.error(
                "Order rejected retcode=%s comment=%s", code, getattr(result, "comment", "")
            )
            return OrderResult(
                False, code, getattr(result, "comment", ""), attempts=attempts
            )

        return OrderResult(False, None, "retries exhausted", attempts=attempts)

    def resolve_position_id(self, result):
        """Return the MT5 position ID created by a filled order."""
        deal_ticket = getattr(result, "deal", 0) or 0
        if deal_ticket:
            deals = self._call("history_deals_get", ticket=deal_ticket)
            if deals:
                position_id = getattr(deals[0], "position_id", 0)
                if position_id:
                    return position_id
        return getattr(result, "order", 0) or getattr(result, "deal", 0)

    def position_side(self, position):
        return "BUY" if getattr(position, "type", None) == POSITION_TYPE_BUY else "SELL"
