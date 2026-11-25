"""SQLite persistence for Iron Scalper.

The database is the historical record of the account: every signal, trade,
execution, risk decision, model version and operational event. It is retained
indefinitely, and it is the only place the dashboard and the engine agree on
what happened.

Three rules shape this module:

1. **Versioned migrations, never destructive.** ``schema_migrations`` records
   what has been applied. Each migration is idempotent (``IF NOT EXISTS``,
   guarded ``ALTER TABLE``), so it is safe on a fresh file, on the 2026-08
   schema and on any database that has already been migrated. Nothing drops a
   table or a row.
2. **Integrity is enforced by the database.** Connections enable
   ``foreign_keys``, ``busy_timeout`` and WAL. Every new table declares its
   keys and its checks; duplicates are prevented by unique indexes rather than
   by hoping the caller checked first.
3. **One timestamp convention.** Trading timestamps are the broker clock, in
   ``YYYY-MM-DD HH:MM:SS`` (the frame every MT5 value is converted to). Rows
   the process writes about itself (``created_at``) are UTC. Both are
   documented per table and normalised on the way in.

Schema as of version 1:

    trades              one row per MT5 position this bot opened (or adopted)
    trade_executions    the MT5 order that produced each trade, request vs fill
    signals             one row per generated signal (trading signals)
    ml_predictions      one row per gated signal: probability, decision, outcome
    market_data         collected OHLCV bars, unique per symbol/timeframe/bar
    trading_settings    immutable snapshots of the config an engine started with
    strategy_performance daily per-strategy rollup, rebuilt from trades
    backtest_results    one row per recorded backtest run
    model_versions      every trained model, with its out-of-sample record
    model_training_runs the run that produced a model version
    risk_events         risk decisions and violations (why a trade was refused)
    system_events       connection, order and operational events
    daily_stats         legacy, unused: superseded by strategy_performance
    users               dashboard accounts
"""

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "data" / "iron_scalper.db"

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 1

# Databases whose schema has already been checked in this process. The bot runs
# a 2 second loop, so re-running migrations on every query was wasted work.
_INITIALIZED = set()
_INIT_LOCK = threading.Lock()

log = logging.getLogger("iron.database")


# ── connection ─────────────────────────────────────────────────────


def _connect():
    """Open a connection with foreign keys, a busy timeout and WAL."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
    # The engine thread writes while Flask reads; without these two the
    # dashboard could turn a trade write into "database is locked".
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # Another process is mid-checkpoint; the database still works.
        pass
    connection.execute("PRAGMA synchronous = NORMAL")
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
    """Normalise a timestamp to the database's ``YYYY-MM-DD HH:MM:SS`` frame."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime(TS_FORMAT)
    text = str(value).strip()
    for pattern in (TS_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).strftime(TS_FORMAT)
        except ValueError:
            continue
    return text


def _now():
    """UTC now, for rows this module stamps about itself."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def _json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _validated_limit(limit):
    """Return a safe positive SQL LIMIT value."""
    limit = int(limit)
    return max(limit, 0)


def _ensure_ready():
    """Apply migrations once per process, not once per database call."""
    key = str(DATABASE_PATH)
    if key in _INITIALIZED:
        return
    with _INIT_LOCK:
        if key not in _INITIALIZED:
            init_db()


# ── migrations ─────────────────────────────────────────────────────


def _has_table(connection, name):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _add_column(connection, table, column, definition):
    """Add a column only when it is absent (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    if not _has_table(connection, table):
        return False
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def _migration_001_baseline(connection):
    """Create the full v1 schema and upgrade the original five-table design.

    On a new database this creates everything. On an existing one the
    ``CREATE TABLE IF NOT EXISTS`` statements are no-ops and the column
    additions bring ``trades``, ``signals`` and ``ml_predictions`` up to the
    schema the rest of the code assumes. No row is deleted or rewritten.
    """
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
            lots REAL NOT NULL CHECK (lots > 0),
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

        CREATE TABLE IF NOT EXISTS ml_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            model_version TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            signal_time TEXT NOT NULL,
            probability REAL NOT NULL,
            threshold REAL NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('taken', 'skipped')),
            features TEXT,
            trade_ticket INTEGER,
            outcome_r REAL,
            outcome_usd REAL,
            outcome_result TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS market_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            open REAL NOT NULL CHECK (open > 0),
            high REAL NOT NULL CHECK (high > 0),
            low REAL NOT NULL CHECK (low > 0),
            close REAL NOT NULL CHECK (close > 0),
            tick_volume INTEGER NOT NULL DEFAULT 0,
            real_volume INTEGER NOT NULL DEFAULT 0,
            spread REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'mt5',
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (high >= low),
            UNIQUE (symbol, timeframe, bar_time)
        );

        CREATE TABLE IF NOT EXISTS trade_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL REFERENCES trades (id) ON DELETE RESTRICT,
            position_id INTEGER,
            order_ticket INTEGER,
            deal_ticket INTEGER,
            requested_price REAL,
            filled_price REAL,
            requested_volume REAL,
            filled_volume REAL,
            spread_points REAL,
            retcode INTEGER,
            attempts INTEGER NOT NULL DEFAULT 1,
            latency_ms INTEGER,
            comment TEXT,
            executed_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS trading_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            effective_from TEXT NOT NULL,
            source TEXT NOT NULL,
            checksum TEXT NOT NULL,
            config TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (checksum, effective_from)
        );

        CREATE TABLE IF NOT EXISTS strategy_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            day TEXT NOT NULL,
            trades INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            gross_profit_usd REAL NOT NULL DEFAULT 0,
            net_profit_usd REAL NOT NULL DEFAULT 0,
            total_r REAL NOT NULL DEFAULT 0,
            avg_r REAL NOT NULL DEFAULT 0,
            win_rate REAL NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (strategy, day)
        );

        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_label TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            data_start TEXT,
            data_end TEXT,
            parameters TEXT,
            trades INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            win_rate REAL NOT NULL DEFAULT 0,
            expectancy_r REAL NOT NULL DEFAULT 0,
            total_r REAL NOT NULL DEFAULT 0,
            max_drawdown_r REAL NOT NULL DEFAULT 0,
            profit_usd REAL,
            model_version_id INTEGER REFERENCES model_versions (id),
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS model_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            family TEXT NOT NULL,
            path TEXT NOT NULL,
            feature_names TEXT NOT NULL,
            threshold REAL NOT NULL,
            data_fingerprint TEXT,
            data_bars INTEGER,
            data_first TEXT,
            data_last TEXT,
            samples TEXT,
            out_of_sample TEXT,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS model_training_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version_id INTEGER NOT NULL
                REFERENCES model_versions (id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
            folds INTEGER,
            embargo_bars INTEGER,
            horizon_bars INTEGER,
            l2 REAL,
            epochs INTEGER,
            learning_rate REAL,
            signals INTEGER,
            trainable INTEGER,
            wins INTEGER,
            losses INTEGER,
            timeouts INTEGER,
            metrics TEXT,
            started_at TEXT,
            finished_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            day TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            strategy TEXT,
            message TEXT NOT NULL,
            detail TEXT,
            blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            level TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
            category TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'engine',
            message TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # ── columns added to the original tables ──────────────────────
    for column, definition in (
        ("position_id", "INTEGER"),
        ("magic", "INTEGER"),
        ("commission", "REAL"),
        ("swap", "REAL"),
        ("slippage_points", "REAL"),
        ("mae_r", "REAL"),
        ("mfe_r", "REAL"),
        ("r_multiple", "REAL"),
        ("close_reason", "TEXT"),
        ("gross_profit_usd", "REAL"),
        ("signal_id", "INTEGER REFERENCES signals (id)"),
        ("ml_prediction_id", "INTEGER REFERENCES ml_predictions (id)"),
    ):
        _add_column(connection, "trades", column, definition)

    for column, definition in (
        ("timeframe", "TEXT"),
        ("trade_id", "INTEGER REFERENCES trades (id)"),
        ("ml_prediction_id", "INTEGER REFERENCES ml_predictions (id)"),
    ):
        _add_column(connection, "signals", column, definition)

    for column, definition in (
        ("model_version_id", "INTEGER REFERENCES model_versions (id)"),
        ("training_run_id", "INTEGER REFERENCES model_training_runs (id)"),
    ):
        _add_column(connection, "ml_predictions", column, definition)

    # Databases created before position_id existed need it backfilled so
    # closing deals can be matched by MT5 position, not order ticket.
    if _has_table(connection, "trades"):
        connection.execute(
            "UPDATE trades SET position_id = ticket WHERE position_id IS NULL"
        )

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_trades_status_open_time
            ON trades (status, open_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_status_close_time
            ON trades (status, close_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_strategy_time
            ON trades (strategy, close_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_open_time
            ON trades (open_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades (position_id);
        CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades (signal_id);
        CREATE INDEX IF NOT EXISTS idx_trades_ml_prediction ON trades (ml_prediction_id);
        CREATE INDEX IF NOT EXISTS idx_trades_close_day ON trades (date(close_time));
        CREATE INDEX IF NOT EXISTS idx_trades_entry_day ON trades (date(open_time));

        CREATE INDEX IF NOT EXISTS idx_signals_signal_time
            ON signals (signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_signals_strategy_time
            ON signals (strategy, signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_signals_executed
            ON signals (executed, signal_time DESC);

        CREATE INDEX IF NOT EXISTS idx_ml_predictions_signal_time
            ON ml_predictions (signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_ml_predictions_ticket
            ON ml_predictions (trade_ticket);
        CREATE INDEX IF NOT EXISTS idx_ml_predictions_version
            ON ml_predictions (model_version, signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_ml_predictions_decision
            ON ml_predictions (decision, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_market_data_lookup
            ON market_data (symbol, timeframe, bar_time DESC);

        CREATE INDEX IF NOT EXISTS idx_exec_trade ON trade_executions (trade_id);
        CREATE INDEX IF NOT EXISTS idx_exec_position ON trade_executions (position_id);

        CREATE INDEX IF NOT EXISTS idx_strategy_performance_day
            ON strategy_performance (day DESC);

        CREATE INDEX IF NOT EXISTS idx_backtest_strategy
            ON backtest_results (strategy, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_training_runs_version
            ON model_training_runs (model_version_id, finished_at DESC);

        CREATE INDEX IF NOT EXISTS idx_risk_events_time
            ON risk_events (event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_risk_events_type
            ON risk_events (event_type, event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_risk_events_day
            ON risk_events (day DESC);

        CREATE INDEX IF NOT EXISTS idx_system_events_time
            ON system_events (event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_system_events_category
            ON system_events (category, event_time DESC);

        CREATE INDEX IF NOT EXISTS idx_trading_settings_effective
            ON trading_settings (effective_from DESC);
        """
    )

    # Duplicate prevention that the original schema left to application code.
    # Existing rows are checked first: a constraint the data already violates
    # must be reported, not thrown as a failed startup.
    # Exact duplicate signal rows were possible before the identity index
    # existed (the old code swallowed the failure). Identical duplicates carry
    # no information, so they are collapsed before the index is retried; every
    # distinct signal and every trade is kept.
    try:
        duplicate_check = connection.execute(
            """
            SELECT strategy, signal_type, signal_time, COUNT(*) AS copies
            FROM signals
            GROUP BY strategy, signal_type, signal_time
            HAVING copies > 1
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        duplicate_check = []
    if duplicate_check:
        # Only rows that are identical in every field are collapsed: two rows
        # that share a bar but disagree on price are different information and
        # are left alone (the index will then be skipped with a warning).
        removed = connection.execute(
            """
            DELETE FROM signals
            WHERE id NOT IN (
                SELECT MIN(id) FROM signals
                GROUP BY strategy, signal_type, signal_time, entry_price, sl, tp
            )
            AND EXISTS (
                SELECT 1 FROM signals AS other
                WHERE other.id < signals.id
                  AND other.strategy = signals.strategy
                  AND other.signal_type = signals.signal_type
                  AND other.signal_time = signals.signal_time
                  AND other.entry_price = signals.entry_price
                  AND other.sl = signals.sl
                  AND other.tp = signals.tp
            )
            """
        ).rowcount
        log.warning(
            "Collapsed %d byte-identical duplicate signal row(s)", removed
        )

    for statement, name in (
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_identity
                ON signals (strategy, signal_type, signal_time)
            """,
            "signals identity",
        ),
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_position_unique
                ON trades (position_id) WHERE position_id IS NOT NULL
            """,
            "trades position_id",
        ),
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_exec_order_unique
                ON trade_executions (order_ticket)
                WHERE order_ticket IS NOT NULL AND order_ticket > 0
            """,
            "execution order ticket",
        ),
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_exec_deal_unique
                ON trade_executions (deal_ticket)
                WHERE deal_ticket IS NOT NULL AND deal_ticket > 0
            """,
            "execution deal ticket",
        ),
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_active_unique
                ON model_versions (is_active) WHERE is_active = 1
            """,
            "single active model",
        ),
    ):
        try:
            connection.execute(statement)
        except sqlite3.DatabaseError as error:
            log.warning(
                "Could not add the %s constraint (%s); existing rows violate it",
                name, error,
            )

    # Read-only alias for the entity name the pipeline documentation uses, so
    # reporting can query model_predictions without a second copy of the rows.
    connection.execute(
        """
        CREATE VIEW IF NOT EXISTS model_predictions AS
        SELECT p.id, p.signal_key, p.model_version, mv.id AS model_version_id,
               p.strategy, p.symbol, p.signal_type, p.signal_time,
               p.probability, p.threshold, p.decision, p.features,
               p.trade_ticket, p.outcome_r, p.outcome_usd, p.outcome_result,
               p.created_at
        FROM ml_predictions p
        LEFT JOIN model_versions mv ON mv.version = p.model_version
        """
    )


MIGRATIONS = (
    (1, "baseline schema, execution/risk/model history, indexes", _migration_001_baseline),
)


def migrate():
    """Apply every pending migration and return what was applied.

    A migration's ``schema_migrations`` row is written only after it returns,
    and every statement inside it is idempotent, so an interrupted upgrade is
    re-run from the same version on the next start rather than leaving a
    half-migrated database behind.
    """
    applied = []
    with _DatabaseConnection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]

        for version, name, function in MIGRATIONS:
            if version <= current:
                continue
            function(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
            applied.append({"version": version, "name": name})

    for entry in applied:
        log.info("Applied migration %s: %s", entry["version"], entry["name"])
    return applied


def init_db():
    """Bring the database up to the current schema. Safe to call repeatedly."""
    applied = migrate()
    _INITIALIZED.add(str(DATABASE_PATH))
    return applied


def schema_version():
    _ensure_ready()
    with _DatabaseConnection() as connection:
        return connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]


def database_health():
    """Operational facts about the file, for tests and diagnostics."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        counts = {}
        for table in tables:
            if table == "sqlite_sequence":
                continue
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
    return {
        "path": str(DATABASE_PATH),
        "schema_version": schema_version(),
        "journal_mode": journal,
        "foreign_keys": bool(foreign_keys),
        "busy_timeout_ms": busy_timeout,
        "tables": tables,
        "counts": counts,
    }


# ── signals ────────────────────────────────────────────────────────


def log_signal(symbol, strategy, signal_type, entry, sl, tp, signal_time, timeframe=None):
    """Record a newly generated strategy signal as unexecuted.

    Returns ``True`` only when the signal was new: the unique index on
    (strategy, signal_type, signal_time) is what de-duplicates across restarts.
    """
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO signals (
                symbol, strategy, signal_type, entry_price, sl, tp, signal_time,
                executed, timeframe
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                symbol, strategy, signal_type, entry, sl, tp,
                _timestamp(signal_time), timeframe,
            ),
        )
        return cursor.rowcount > 0


def get_signal(symbol, strategy, signal_type, signal_time):
    """Return the stored signal for that identity, or ``None``."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            """
            SELECT * FROM signals
            WHERE strategy = ? AND signal_type = ? AND signal_time = ?
            """,
            (strategy, signal_type, _timestamp(signal_time)),
        ).fetchone()
    return dict(row) if row is not None else None


def mark_signal_traded(signal_id, trade_id, ml_prediction_id=None):
    """Point a signal at the trade it produced, once."""
    if not signal_id:
        return 0
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE signals
            SET executed = 1, trade_id = ?, ml_prediction_id = COALESCE(?, ml_prediction_id)
            WHERE id = ?
            """,
            (trade_id, ml_prediction_id, signal_id),
        )
        return cursor.rowcount


def get_recent_signals(limit=100):
    """Return the most recently generated signals as dictionaries."""
    limit = _validated_limit(limit)
    if limit == 0:
        return []

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM signals
            ORDER BY signal_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ── trades ─────────────────────────────────────────────────────────


def log_trade_open(ticket, symbol, trade_type, strategy, entry, sl, tp, lots, open_time,
                   position_id=None, magic=None, signal_id=None, ml_prediction_id=None):
    """Record an MT5 trade after its opening order has been accepted.

    Returns the row id (``None`` when the ticket already existed), which the
    caller uses to attach its execution record.
    """
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO trades (
                ticket, position_id, symbol, trade_type, strategy,
                entry_price, sl, tp, lots, open_time, status,
                magic, signal_id, ml_prediction_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
            ON CONFLICT(ticket) DO NOTHING
            """,
            (
                ticket,
                position_id if position_id is not None else ticket,
                symbol, trade_type, strategy, entry, sl, tp, lots,
                _timestamp(open_time), magic, signal_id, ml_prediction_id,
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None


def log_trade_close(ticket, exit_price, profit_pips, profit_usd, close_time,
                    commission=None, swap=None, close_reason=None, r_multiple=None,
                    mae_r=None, mfe_r=None, slippage_points=None):
    """Mark an existing MT5 trade as closed and save its realised result."""
    _ensure_ready()
    gross = None
    if profit_usd is not None:
        gross = float(profit_usd) - float(commission or 0.0) - float(swap or 0.0)
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE trades
            SET exit_price = ?,
                profit_pips = ?,
                profit_usd = ?,
                close_time = ?,
                status = 'CLOSED',
                commission = COALESCE(?, commission),
                swap = COALESCE(?, swap),
                gross_profit_usd = COALESCE(?, gross_profit_usd),
                close_reason = COALESCE(?, close_reason),
                r_multiple = COALESCE(?, r_multiple),
                mae_r = COALESCE(?, mae_r),
                mfe_r = COALESCE(?, mfe_r),
                slippage_points = COALESCE(?, slippage_points)
            WHERE ticket = ?
            """,
            (
                exit_price, profit_pips, profit_usd, _timestamp(close_time),
                commission, swap, gross, close_reason, r_multiple,
                mae_r, mfe_r, slippage_points, ticket,
            ),
        )
        return cursor.rowcount


def log_trade_mae_mfe(ticket, mae_r, mfe_r):
    """Record how far a trade ran against and in favour of itself, in R."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE trades
            SET mae_r = COALESCE(?, mae_r), mfe_r = COALESCE(?, mfe_r)
            WHERE ticket = ?
            """,
            (mae_r, mfe_r, ticket),
        )
        return cursor.rowcount


def log_execution(trade_id, position_id=None, order_ticket=None, deal_ticket=None,
                  requested_price=None, filled_price=None, requested_volume=None,
                  filled_volume=None, spread_points=None, retcode=None, attempts=1,
                  latency_ms=None, comment=None, executed_at=None):
    """Record the MT5 execution behind a trade, request versus fill."""
    if not trade_id:
        return None
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO trade_executions (
                trade_id, position_id, order_ticket, deal_ticket,
                requested_price, filled_price, requested_volume, filled_volume,
                spread_points, retcode, attempts, latency_ms, comment, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, position_id, order_ticket, deal_ticket,
                requested_price, filled_price, requested_volume, filled_volume,
                spread_points, retcode, int(attempts or 1), latency_ms, comment,
                _timestamp(executed_at or _now()),
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None


def get_executions(trade_ticket=None, limit=50):
    """Execution rows, for one trade ticket or the most recent ones."""
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        if trade_ticket is None:
            rows = connection.execute(
                """
                SELECT e.*, t.ticket, t.strategy
                FROM trade_executions e
                JOIN trades t ON t.id = e.trade_id
                ORDER BY e.executed_at DESC, e.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT e.*, t.ticket, t.strategy
                FROM trade_executions e
                JOIN trades t ON t.id = e.trade_id
                WHERE t.ticket = ?
                ORDER BY e.executed_at, e.id
                """,
                (trade_ticket,),
            ).fetchall()
    return [dict(row) for row in rows]


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


def get_open_trades():
    """Return every trade still marked OPEN, oldest first."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id"
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


# ── market data ────────────────────────────────────────────────────


def upsert_market_bars(symbol, timeframe, bars, source="mt5"):
    """Insert collected bars, ignoring ones already stored.

    ``bars`` is either an object exposing ``time/open/high/low/close/
    tick_volume/spread`` arrays (``src.ml.features.Bars``) or a sequence of
    dicts. Returns the number of rows that were new.
    """
    columns = ("time", "open", "high", "low", "close", "tick_volume", "spread")
    if hasattr(bars, "time") and hasattr(bars, "close"):
        rows = zip(*[getattr(bars, name) for name in columns])
    else:
        rows = [
            tuple(item.get(name) for name in columns)
            for item in bars
        ]

    payload = []
    for stamp, open_price, high, low, close, volume, spread in rows:
        if isinstance(stamp, datetime):
            text = stamp.strftime(TS_FORMAT)
        elif isinstance(stamp, (int, float)) or (
            isinstance(stamp, str) and stamp.isdigit()
        ):
            text = datetime.fromtimestamp(int(stamp), timezone.utc).strftime(TS_FORMAT)
        else:
            text = _timestamp(stamp)
        payload.append((
            symbol, timeframe, text, float(open_price), float(high), float(low),
            float(close), int(volume or 0), int(spread or 0), source,
        ))

    if not payload:
        return 0

    _ensure_ready()
    with _DatabaseConnection() as connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO market_data (
                symbol, timeframe, bar_time, open, high, low, close,
                tick_volume, spread, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return connection.total_changes - before


def get_market_bars(symbol, timeframe, start=None, end=None, limit=2000):
    """Stored bars, oldest first, optionally bounded by time."""
    limit = _validated_limit(limit)
    _ensure_ready()
    query = """
        SELECT bar_time, open, high, low, close, tick_volume, spread
        FROM market_data
        WHERE symbol = ? AND timeframe = ?
    """
    params = [symbol, timeframe]
    if start:
        query += " AND bar_time >= ?"
        params.append(_timestamp(start))
    if end:
        query += " AND bar_time <= ?"
        params.append(_timestamp(end))
    query += " ORDER BY bar_time DESC LIMIT ?"
    params.append(limit if limit else 10 ** 9)

    with _DatabaseConnection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in reversed(rows)]


def count_market_bars(symbol=None, timeframe=None):
    _ensure_ready()
    query = "SELECT COUNT(*) FROM market_data WHERE 1 = 1"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if timeframe:
        query += " AND timeframe = ?"
        params.append(timeframe)
    with _DatabaseConnection() as connection:
        return connection.execute(query, params).fetchone()[0]


# ── settings snapshots ─────────────────────────────────────────────


def record_trading_settings(config, source="config/settings.py", note=None,
                            effective_from=None):
    """Store an immutable snapshot of the configuration an engine started with.

    ``config`` is the ``CONFIG.snapshot()`` dict. Python stays the source of
    truth (Task 3); this table is the audit trail of what was actually in use,
    so a result can always be traced to the settings that produced it.
    """
    payload = _json(config)
    # hashlib, not hash(): Python's string hash is salted per process, which
    # would store a new "identical" snapshot on every restart.
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    when = _timestamp(effective_from or _now())
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO trading_settings
                (effective_from, source, checksum, config, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (when, source, checksum, payload, note),
        )
        if cursor.rowcount:
            return cursor.lastrowid
        row = connection.execute(
            "SELECT id FROM trading_settings WHERE checksum = ? AND effective_from = ?",
            (checksum, when),
        ).fetchone()
    return row["id"] if row else None


def get_trading_settings(limit=10):
    """Most recent configuration snapshots, newest first."""
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT id, effective_from, source, checksum, note, created_at
            FROM trading_settings
            ORDER BY effective_from DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_trading_settings_by_id(settings_id):
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            "SELECT * FROM trading_settings WHERE id = ?", (settings_id,)
        ).fetchone()
    return dict(row) if row is not None else None


# ── strategy performance rollup ────────────────────────────────────


def refresh_strategy_performance(day=None):
    """Rebuild one day's per-strategy rollup from the trades table.

    ``strategy_performance`` is derived data: this function is the only writer,
    and it recomputes rather than incrementing, so the rollup can never drift
    from the trades it summarises.
    """
    _ensure_ready()
    with _DatabaseConnection() as connection:
        if day is None:
            row = connection.execute(
                "SELECT date(close_time) FROM trades WHERE status = 'CLOSED' "
                "ORDER BY close_time DESC LIMIT 1"
            ).fetchone()
            day = row[0] if row and row[0] else None
        if day is None:
            return 0

        rows = connection.execute(
            """
            SELECT strategy,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN profit_usd > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN profit_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                   COALESCE(SUM(profit_usd), 0) AS net,
                   COALESCE(SUM(COALESCE(commission, 0) + COALESCE(swap, 0)), 0) AS costs,
                   COALESCE(SUM(r_multiple), 0) AS total_r,
                   COALESCE(AVG(r_multiple), 0) AS avg_r
            FROM trades
            WHERE status = 'CLOSED' AND date(close_time) = ?
            GROUP BY strategy
            """,
            (str(day),),
        ).fetchall()

        written = 0
        for row in rows:
            trades = row["trades"] or 0
            writes = connection.execute(
                """
                INSERT INTO strategy_performance (
                    strategy, day, trades, wins, losses, gross_profit_usd,
                    net_profit_usd, total_r, avg_r, win_rate, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, day) DO UPDATE SET
                    trades = excluded.trades,
                    wins = excluded.wins,
                    losses = excluded.losses,
                    gross_profit_usd = excluded.gross_profit_usd,
                    net_profit_usd = excluded.net_profit_usd,
                    total_r = excluded.total_r,
                    avg_r = excluded.avg_r,
                    win_rate = excluded.win_rate,
                    computed_at = excluded.computed_at
                """,
                (
                    row["strategy"], str(day), trades, row["wins"] or 0,
                    row["losses"] or 0, round((row["net"] or 0) + (row["costs"] or 0), 2),
                    round(row["net"] or 0, 2), round(row["total_r"] or 0, 3),
                    round(row["avg_r"] or 0, 3),
                    round((row["wins"] or 0) / trades, 4) if trades else 0.0,
                    _now(),
                ),
            )
            written += writes.rowcount
        return written


def get_strategy_performance(days=30):
    limit_days = max(int(days), 1)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM strategy_performance
            WHERE day >= date('now', ?)
            ORDER BY day DESC, strategy
            """,
            (f"-{limit_days} days",),
        ).fetchall()
    return [dict(row) for row in rows]


# ── backtests ──────────────────────────────────────────────────────


def record_backtest_result(run_label, strategy, symbol, timeframe, **fields):
    """Store one backtest run. Historical rows are never overwritten."""
    _ensure_ready()
    columns = (
        "data_start", "data_end", "parameters", "trades", "wins", "win_rate",
        "expectancy_r", "total_r", "max_drawdown_r", "profit_usd",
        "model_version_id", "notes",
    )
    values = []
    for name in columns:
        value = fields.get(name)
        if name in ("parameters",):
            value = _json(value)
        elif name in ("data_start", "data_end"):
            value = _timestamp(value) if value else None
        values.append(value)

    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            f"""
            INSERT INTO backtest_results
                (run_label, strategy, symbol, timeframe, {', '.join(columns)})
            VALUES (?, ?, ?, ?, {', '.join('?' for _ in columns)})
            """,
            (run_label, strategy, symbol, timeframe, *values),
        )
        return cursor.lastrowid


def get_backtest_results(limit=50):
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            "SELECT * FROM backtest_results ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ── models ─────────────────────────────────────────────────────────


def record_model_training(version, family, path, feature_names, threshold,
                          data=None, samples=None, out_of_sample=None,
                          run=None, active=False):
    """Store a model version and the training run that produced it.

    The JSON files remain the artefact the engine loads; these rows are the
    queryable history (which version was trained on what, with which scores).
    """
    data = data or {}
    run = run or {}
    _ensure_ready()
    with _DatabaseConnection() as connection:
        if active:
            connection.execute("UPDATE model_versions SET is_active = 0 WHERE is_active = 1")
        cursor = connection.execute(
            """
            INSERT INTO model_versions (
                version, family, path, feature_names, threshold,
                data_fingerprint, data_bars, data_first, data_last,
                samples, out_of_sample, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                path = excluded.path,
                threshold = excluded.threshold,
                out_of_sample = excluded.out_of_sample,
                samples = excluded.samples,
                is_active = excluded.is_active
            """,
            (
                version, family, path, _json(feature_names), float(threshold),
                data.get("fingerprint"), data.get("bars"),
                _timestamp(data["first"]) if data.get("first") else None,
                _timestamp(data["last"]) if data.get("last") else None,
                _json(samples), _json(out_of_sample), 1 if active else 0,
            ),
        )
        row = connection.execute(
            "SELECT id FROM model_versions WHERE version = ?", (version,)
        ).fetchone()
        version_id = row["id"] if row else cursor.lastrowid

        run_id = None
        if run:
            run_cursor = connection.execute(
                """
                INSERT INTO model_training_runs (
                    model_version_id, status, folds, embargo_bars, horizon_bars,
                    l2, epochs, learning_rate, signals, trainable, wins, losses,
                    timeouts, metrics, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, run.get("status", "completed"), run.get("folds"),
                    run.get("embargo_bars"), run.get("horizon_bars"),
                    run.get("l2"), run.get("epochs"), run.get("learning_rate"),
                    run.get("signals"), run.get("trainable"), run.get("wins"),
                    run.get("losses"), run.get("timeouts"), _json(run.get("metrics")),
                    _timestamp(run.get("started_at")) if run.get("started_at") else None,
                    _timestamp(run.get("finished_at") or _now()),
                ),
            )
            run_id = run_cursor.lastrowid

    return {"model_version_id": version_id, "training_run_id": run_id}


def get_model_versions(limit=20):
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            "SELECT * FROM model_versions ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_model_training_runs(limit=20):
    limit = _validated_limit(limit)
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT r.*, v.version
            FROM model_training_runs r
            JOIN model_versions v ON v.id = r.model_version_id
            ORDER BY r.finished_at DESC, r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_active_model(version):
    """Mark one model version active and every other one inactive."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            "SELECT id FROM model_versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            return False
        connection.execute("UPDATE model_versions SET is_active = 0")
        connection.execute(
            "UPDATE model_versions SET is_active = 1 WHERE id = ?", (row["id"],)
        )
    return True


# ── predictions ────────────────────────────────────────────────────


def log_prediction(signal_key, model_version, strategy, symbol, signal_type, signal_time,
                   probability, threshold, decision, features=None):
    """Record one gated signal. Returns ``True`` only when it was new."""
    _ensure_ready()
    payload = _json(features)
    with _DatabaseConnection() as connection:
        version_row = connection.execute(
            "SELECT id FROM model_versions WHERE version = ?", (model_version,)
        ).fetchone()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO ml_predictions (
                signal_key, model_version, model_version_id, strategy, symbol,
                signal_type, signal_time, probability, threshold, decision, features
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_key, model_version, version_row["id"] if version_row else None,
                strategy, symbol, signal_type, _timestamp(signal_time),
                float(probability), float(threshold), decision, payload,
            ),
        )
        return cursor.rowcount > 0


def get_prediction(signal_key):
    """One prediction row by its signal key, or ``None``."""
    if not signal_key:
        return None
    _ensure_ready()
    with _DatabaseConnection() as connection:
        row = connection.execute(
            "SELECT * FROM ml_predictions WHERE signal_key = ?", (signal_key,)
        ).fetchone()
    return dict(row) if row is not None else None


def link_prediction(signal_key, trade_ticket):
    """Attach the MT5 order ticket to the prediction that allowed it."""
    if not signal_key or not trade_ticket:
        return 0
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            "UPDATE ml_predictions SET trade_ticket = ? WHERE signal_key = ?",
            (trade_ticket, signal_key),
        )
        return cursor.rowcount


def link_prediction_to_trade(signal_key, trade_row_id):
    """Point a prediction at the trades row (and its ticket) after a fill."""
    if not signal_key or not trade_row_id:
        return 0
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE ml_predictions
            SET trade_ticket = (SELECT ticket FROM trades WHERE id = ?)
            WHERE signal_key = ?
            """,
            (trade_row_id, signal_key),
        )
        return cursor.rowcount


def resolve_prediction(trade_ticket, outcome_r, outcome_usd, outcome_result):
    """Store a closed trade's realised result against its prediction."""
    if not trade_ticket:
        return 0
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            UPDATE ml_predictions
            SET outcome_r = ?, outcome_usd = ?, outcome_result = ?
            WHERE trade_ticket = ?
            """,
            (float(outcome_r), float(outcome_usd), outcome_result, trade_ticket),
        )
        return cursor.rowcount


def get_ml_predictions(limit=200):
    """Most recent predictions, newest first, for the dashboard."""
    limit = _validated_limit(limit)
    if limit == 0:
        return []
    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(
            """
            SELECT signal_key, model_version, model_version_id, strategy, signal_type,
                   signal_time, probability, threshold, decision, trade_ticket,
                   outcome_r, outcome_usd, outcome_result, created_at
            FROM ml_predictions
            ORDER BY signal_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ── risk and system events ─────────────────────────────────────────


def log_risk_event(event_type, message, severity="info", strategy=None, detail=None,
                   blocked=True, event_time=None, day=None):
    """Record a risk decision: why a trade was allowed, refused or stopped."""
    when = _timestamp(event_time or _now())
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO risk_events (
                event_time, day, event_type, severity, strategy, message, detail, blocked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when, str(day or when[:10]), event_type, severity, strategy,
                message, _json(detail), 1 if blocked else 0,
            ),
        )
        return cursor.lastrowid


def get_risk_events(limit=100, event_type=None, severity=None, day=None):
    limit = _validated_limit(limit)
    query = "SELECT * FROM risk_events WHERE 1 = 1"
    params = []
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    if day:
        query += " AND day = ?"
        params.append(str(day))
    query += " ORDER BY event_time DESC, id DESC LIMIT ?"
    params.append(limit)

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def log_system_event(message, level="info", category="engine", source="engine",
                     detail=None, event_time=None):
    """Record a connection, order or operational event."""
    _ensure_ready()
    with _DatabaseConnection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO system_events (event_time, level, category, source, message, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _timestamp(event_time or _now()), level, category, source,
                message, _json(detail),
            ),
        )
        return cursor.lastrowid


def get_system_events(limit=100, level=None, category=None):
    limit = _validated_limit(limit)
    query = "SELECT * FROM system_events WHERE 1 = 1"
    params = []
    if level:
        query += " AND level = ?"
        params.append(level)
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY event_time DESC, id DESC LIMIT ?"
    params.append(limit)

    _ensure_ready()
    with _DatabaseConnection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ── users ──────────────────────────────────────────────────────────


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
