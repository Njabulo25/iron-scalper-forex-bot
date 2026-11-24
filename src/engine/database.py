"""SQLite persistence helpers for Iron Scalper.

The database is stored in the project's ``data`` directory and is retained
indefinitely. Call :func:`init_db` during application startup; the public
write and read helpers also initialize the schema defensively.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "data" / "iron_scalper.db"

# Databases whose schema has already been created in this process. The bot runs
# a 2 second loop, so re-running the schema and the position_id backfill on
# every query was wasted work on each pass.
_INITIALIZED = set()
_INIT_LOCK = threading.Lock()


def _connect():
    """Open a connection configured to return rows as dictionaries."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


class _DatabaseConnection:
    """Commit or roll back a transaction and always close its SQLite handle."""

    def __enter__(self):
        self.connection = _connect()
        return self.connection

    def __exit__(self, exception_type, exception_value, traceback):
        try:
            if exception_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


def _timestamp(value):
    """Convert datetime values to a stable SQLite timestamp representation."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _validated_limit(limit):
    """Return a safe positive SQL LIMIT value."""
    limit = int(limit)
    return max(limit, 0)


def _ensure_ready():
    """Create the schema once per process, not once per database call."""
    key = str(DATABASE_PATH)
    if key in _INITIALIZED:
        return
    with _INIT_LOCK:
        if key not in _INITIALIZED:
            init_db()


def init_db():
    """Create the trading database and all required tables if they are absent."""
    with _DatabaseConnection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket INTEGER NOT NULL UNIQUE,
                position_id INTEGER,
                symbol TEXT NOT NULL,
                trade_type TEXT NOT NULL CHECK (trade_type IN ('BUY', 'SELL')),
                strategy TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                lots REAL NOT NULL,
                profit_pips REAL,
                profit_usd REAL,
                open_time TEXT NOT NULL,
                close_time TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                signal_type TEXT NOT NULL CHECK (signal_type IN ('BUY', 'SELL')),
                entry_price REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                signal_time TEXT NOT NULL,
                executed INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                total_trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                total_pips REAL NOT NULL DEFAULT 0,
                total_profit_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pin TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status_open_time
                ON trades (status, open_time DESC);
            CREATE INDEX IF NOT EXISTS idx_signals_signal_time
                ON signals (signal_time DESC);
            """
        )

        # Databases created before position_id existed need the column added so
        # closing deals can be matched by MT5 position instead of order ticket.
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(trades)")
        }
        if "position_id" not in existing_columns:
            connection.execute("ALTER TABLE trades ADD COLUMN position_id INTEGER")
        connection.execute("UPDATE trades SET position_id = ticket WHERE position_id IS NULL")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades (position_id)"
        )

    _INITIALIZED.add(str(DATABASE_PATH))


def log_signal(symbol, strategy, signal_type, entry, sl, tp, signal_time):
    """Record a newly generated strategy signal as unexecuted."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO signals (
                symbol, strategy, signal_type, entry_price, sl, tp, signal_time, executed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (symbol, strategy, signal_type, entry, sl, tp, _timestamp(signal_time)),
        )
        return cursor.lastrowid


def log_trade_open(ticket, symbol, trade_type, strategy, entry, sl, tp, lots, open_time, position_id=None):
    """Record an MT5 trade after its opening order has been accepted."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO trades (
                ticket, position_id, symbol, trade_type, strategy,
                entry_price, sl, tp, lots, open_time, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            ON CONFLICT(ticket) DO NOTHING
            """,
            (
                ticket,
                position_id if position_id is not None else ticket,
                symbol, trade_type, strategy, entry, sl, tp, lots, _timestamp(open_time),
            ),
        )
        return cursor.lastrowid


def log_trade_close(ticket, exit_price, profit_pips, profit_usd, close_time):
    """Mark an existing MT5 trade as closed and save its realised result."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE trades
            SET exit_price = ?,
                profit_pips = ?,
                profit_usd = ?,
                close_time = ?,
                status = 'CLOSED'
            WHERE ticket = ?
            """,
            (exit_price, profit_pips, profit_usd, _timestamp(close_time), ticket),
        )
        return cursor.rowcount


def get_recent_trades(limit=100):
    """Return the most recently opened trades as dictionaries."""
    limit = _validated_limit(limit)
    if limit == 0:
        return []

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trades
            ORDER BY open_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_recent_signals(limit=100):
    """Return the most recently generated signals as dictionaries."""
    limit = _validated_limit(limit)
    if limit == 0:
        return []

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM signals
            ORDER BY signal_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_trade_by_ticket(ticket):
    """Return one trade by its MT5 order ticket, or ``None`` when absent."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            "SELECT * FROM trades WHERE ticket = ?",
            (ticket,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_open_trade_by_position(position_id):
    """Return the still-open trade belonging to an MT5 position ID, if any."""
    if not position_id:
        return None

    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE position_id = ? AND status = 'OPEN'
            ORDER BY id DESC
            LIMIT 1
            """,
            (position_id,),
        ).fetchone()
    return dict(row) if row is not None else None


_STATS_SQL = """
    SELECT
        COUNT(*) AS total_trades,
        COALESCE(SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END), 0) AS wins,
        COALESCE(SUM(CASE WHEN profit_pips <= 0 THEN 1 ELSE 0 END), 0) AS losses,
        COALESCE(SUM(profit_pips), 0) AS total_pips,
        COALESCE(SUM(profit_usd), 0) AS total_usd
    FROM trades
    WHERE status = 'CLOSED'
"""


def _stats_from_row(row):
    """Shape one aggregate row into the stats dictionary."""
    total_trades = row["total_trades"] or 0
    wins = row["wins"] or 0
    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": row["losses"] or 0,
        "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
        "total_pips": round(row["total_pips"] or 0, 1),
        "total_usd": round(row["total_usd"] or 0, 2),
    }


def get_dashboard_snapshot(limit=60):
    """Return realised stats and recent closed trades in a single connection."""
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        stats_row = connection.execute(_STATS_SQL).fetchone()
        rows = connection.execute(
            """
            SELECT trade_type, entry_price, exit_price, profit_pips, profit_usd,
                   open_time, close_time, strategy
            FROM trades
            WHERE status = 'CLOSED'
            ORDER BY close_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    history = [
        {
            "time": row["close_time"] or row["open_time"],
            "type": row["trade_type"],
            "strategy": row["strategy"],
            "entry": row["entry_price"],
            "exit": row["exit_price"] or 0,
            "pips": row["profit_pips"] or 0,
            "usd": row["profit_usd"] or 0,
            "win": (row["profit_pips"] or 0) > 0,
        }
        for row in rows
    ]
    return {"stats": _stats_from_row(stats_row), "history": history}


def get_trade_stats():
    """Return realised performance across every closed trade."""
    return get_dashboard_snapshot(0)["stats"]


def get_day_state(day=None):
    """Return one trading day's realised result and its trailing loss streak.

    ``day`` is the broker-clock day (the clock the stored timestamps use).
    """
    if day is None:
        day = datetime.now().date()

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT profit_usd
            FROM trades
            WHERE status = 'CLOSED' AND date(close_time) = ?
            ORDER BY close_time DESC, id DESC
            """,
            (str(day),),
        ).fetchall()

    realised_usd = sum(row["profit_usd"] or 0.0 for row in rows)
    trailing_losses = 0
    for row in rows:
        if (row["profit_usd"] or 0.0) < 0:
            trailing_losses += 1
        else:
            break

    return {
        "realised_usd": round(realised_usd, 2),
        "trades": len(rows),
        "trailing_losses": trailing_losses,
    }


def get_win_rate():
    """Return aggregate closed-trade performance based on realised pips."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN profit_pips <= 0 THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(profit_pips), 0) AS total_pips
            FROM trades
            WHERE status = 'CLOSED'
            """
        ).fetchone()

    wins = row["wins"]
    losses = row["losses"]
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades else 0.0
    return {
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pips": row["total_pips"],
    }


def add_user(username, pin, role):
    """Create a user and return ``True`` when the username was available."""
    if role not in {"admin", "viewer"}:
        raise ValueError("role must be 'admin' or 'viewer'")

    _ensure_ready()
    try:
        with _DatabaseConnection() as connection:
            connection.execute(
                "INSERT INTO users (username, pin, role) VALUES (?, ?, ?)",
                (username, pin, role),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def verify_user(username, pin):
    """Return whether the supplied username and PIN match a stored user."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            "SELECT 1 FROM users WHERE username = ? AND pin = ?",
            (username, pin),
        ).fetchone()
    return row is not None
