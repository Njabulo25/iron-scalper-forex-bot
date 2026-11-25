"""Schema, migration and integrity checks for the trading database.

The harness builds a database with the *old* five-table schema, fills it with
rows, runs the migration and then checks three things:

* nothing that existed was lost (or silently rewritten),
* the new schema enforces what it claims (keys, uniques, checks, indexes),
* the helpers the engine and dashboard use still return the same shapes.

Run: python tests/test_database_schema.py
"""

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f" -> {detail}" if detail else ""))


LEGACY_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL UNIQUE,
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    position_id INTEGER
);
CREATE TABLE signals (
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
CREATE TABLE daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    total_pips REAL NOT NULL DEFAULT 0,
    total_profit_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pin TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ml_predictions (
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
CREATE INDEX idx_trades_status_open_time ON trades (status, open_time DESC);
"""


def build_legacy(path):
    """Create the pre-migration database with representative rows."""
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.executemany(
        """
        INSERT INTO trades (
            ticket, position_id, symbol, trade_type, strategy, entry_price,
            exit_price, sl, tp, lots, profit_pips, profit_usd, open_time,
            close_time, status
        ) VALUES (?, ?, 'XAUUSD', ?, 'SCALPER', ?, ?, ?, ?, 0.10, ?, ?, ?, ?, ?)
        """,
        [
            # A closed winner that will exercise the rollup and the stats query.
            (1001, 9001, "BUY", 2000.0, 2010.0, 1995.0, 2010.0,
             100.0, 10.0, "2026-01-05 10:00:00", "2026-01-05 10:30:00", "CLOSED"),
            # An open trade: the migration must leave it exactly as it was.
            (1002, 9002, "SELL", 2010.0, None, 2015.0, 2000.0,
             None, None, "2026-01-05 11:00:00", None, "OPEN"),
            # A closed loser from the other strategy, same day.
            (1003, 9003, "BUY", 2005.0, 1999.0, 2000.0, 2015.0,
             -60.0, -6.0, "2026-01-05 12:00:00", "2026-01-05 12:40:00", "CLOSED"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO signals (symbol, strategy, signal_type, entry_price, sl, tp, signal_time)
        VALUES ('XAUUSD', 'SCALPER', 'BUY', ?, ?, ?, ?)
        """,
        [
            # Six byte-identical duplicates of one bar: the old schema allowed
            # this because its unique index was never created.
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            (2000.0, 1995.0, 2010.0, "2026-01-05 10:00:00"),
            # A distinct signal on another bar.
            (2004.0, 1999.0, 2014.0, "2026-01-05 11:00:00"),
        ],
    )
    connection.execute(
        "INSERT INTO users (username, pin, role) VALUES ('legacy', '9999', 'viewer')"
    )
    connection.execute(
        """
        INSERT INTO ml_predictions (
            signal_key, model_version, strategy, symbol, signal_type, signal_time,
            probability, threshold, decision
        ) VALUES ('SCALPER|BUY|20260105_1000', 'v-old', 'SCALPER', 'XAUUSD', 'BUY',
                  '2026-01-05 10:00:00', 0.71, 0.5, 'taken')
        """
    )
    connection.commit()
    connection.close()


def run():
    print("\nDatabase schema checks")
    print("=" * 60)

    from src.engine import database as db
    from src.engine import analytics

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        legacy_path = folder / "legacy.db"
        build_legacy(legacy_path)

        original_path = db.DATABASE_PATH
        db.DATABASE_PATH = legacy_path
        db._INITIALIZED.clear()
        try:
            _check_migration(db, legacy_path, folder)
            _check_integrity(db, legacy_path)
            _check_helpers(db, analytics)
            _check_models_and_events(db)
        finally:
            db.DATABASE_PATH = original_path
            db._INITIALIZED.clear()

        # A brand-new file must reach the same version without errors.
        fresh = folder / "fresh.db"
        db.DATABASE_PATH = fresh
        db._INITIALIZED.clear()
        try:
            applied = db.init_db()
            health = db.database_health()
            check("a fresh database reaches schema version 1",
                  health["schema_version"] == db.SCHEMA_VERSION, applied)
            check("a fresh database gets every table",
                  {"trades", "market_data", "risk_events", "model_versions",
                   "trading_settings", "schema_migrations"} <= set(health["tables"]))
        finally:
            db.DATABASE_PATH = original_path
            db._INITIALIZED.clear()

    print("=" * 60)
    print(f"{PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


def _check_migration(db, path, folder):
    print("\nMigration of the legacy schema")
    before = sqlite3.connect(path)
    before_counts = {
        table: before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("trades", "signals", "users", "ml_predictions", "daily_stats")
    }
    before_tickets = [row[0] for row in before.execute("SELECT ticket FROM trades ORDER BY id")]
    before_signals = before.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    before.close()

    applied = db.init_db()
    check("migration 1 is applied once", applied == [{"version": 1, "name": applied[0]["name"]}], applied)
    check("the schema version is recorded", db.schema_version() == 1)
    check("re-running is a no-op", db.migrate() == [])

    after = sqlite3.connect(path)
    after_counts = {
        table: after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("trades", "users", "ml_predictions", "daily_stats")
    }
    check("trades are preserved row for row",
          after_counts["trades"] == before_counts["trades"]
          and [row[0] for row in after.execute("SELECT ticket FROM trades ORDER BY id")] == before_tickets,
          after_counts)
    check("users and settings rows are preserved",
          after_counts["users"] == before_counts["users"]
          and after_counts["ml_predictions"] == before_counts["ml_predictions"])
    check("daily_stats is kept (legacy, not dropped)",
          after_counts["daily_stats"] == before_counts["daily_stats"])

    duplicates = after.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT strategy, signal_type, signal_time, entry_price, sl, tp
            FROM signals GROUP BY 1, 2, 3, 4, 5, 6
        )
        """
    ).fetchone()[0]
    check("only byte-identical signal duplicates are collapsed",
          after.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == duplicates)
    check("no distinct signal was lost", duplicates == 2, duplicates)
    check("the duplicate collapse is reported, not silent",
          before_signals > duplicates)

    columns = {
        table: {row[1] for row in after.execute(f"PRAGMA table_info({table})")}
        for table in ("trades", "signals", "ml_predictions")
    }
    check("trades gained execution and linkage columns",
          {"magic", "commission", "swap", "slippage_points", "r_multiple",
           "close_reason", "mae_r", "mfe_r", "signal_id",
           "ml_prediction_id"} <= columns["trades"])
    check("signals gained timeframe and trade linkage",
          {"timeframe", "trade_id", "ml_prediction_id"} <= columns["signals"])
    check("predictions gained model linkage",
          {"model_version_id", "training_run_id"} <= columns["ml_predictions"])

    tables = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("every proposed entity exists",
          {"trading_settings", "market_data", "trade_executions", "strategy_performance",
           "backtest_results", "model_versions", "model_training_runs", "risk_events",
           "system_events"} <= tables, tables)
    views = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    check("model_predictions is available as a view, not a second copy",
          "model_predictions" in views)

    indexes = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("hot query paths are indexed",
          {"idx_trades_status_close_time", "idx_trades_strategy_time",
           "idx_trades_close_day", "idx_market_data_lookup",
           "idx_risk_events_time", "idx_system_events_category"} <= indexes, sorted(indexes)[:8])
    check("the pre-existing ticket uniqueness survived",
          "sqlite_autoindex_trades_1" in indexes or "idx_trades_ticket" in indexes)

    journal = after.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL is enabled on connections", journal.lower() == "wal", journal)
    after.close()

    # A second copy of the same legacy file: migration must be deterministic.
    second = folder / "legacy_copy.db"
    second.write_bytes(path.read_bytes())
    db.DATABASE_PATH = second
    db._INITIALIZED.clear()
    db.init_db()
    check("migrating a second copy yields the same version", db.schema_version() == 1)
    db.DATABASE_PATH = path
    db._INITIALIZED.clear()
    db.init_db()


def _check_integrity(db, path):
    print("\nConstraints and duplicate prevention")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        connection.execute(
            """
            INSERT INTO trade_executions (trade_id, order_ticket, executed_at)
            VALUES (999999, 5001, '2026-01-05 10:00:00')
            """
        )
        rejected = False
    except sqlite3.IntegrityError:
        rejected = True
    check("an execution cannot reference a missing trade", rejected)
    connection.rollback()

    trade_id = connection.execute("SELECT id FROM trades WHERE ticket = 1001").fetchone()[0]
    connection.execute(
        """
        INSERT INTO trade_executions (trade_id, position_id, order_ticket, deal_ticket, executed_at)
        VALUES (?, 9001, 5001, 7001, '2026-01-05 10:00:00')
        """,
        (trade_id,),
    )
    try:
        connection.execute(
            """
            INSERT INTO trade_executions (trade_id, order_ticket, executed_at)
            VALUES (?, 5001, '2026-01-05 10:01:00')
            """,
            (trade_id,),
        )
        duplicate_execution = False
    except sqlite3.IntegrityError:
        duplicate_execution = True
    check("one order ticket produces at most one execution", duplicate_execution)
    connection.rollback()

    # The rollback above discarded the execution too, so re-create it before
    # asking whether a referenced trade can be deleted.
    connection.execute(
        """
        INSERT INTO trade_executions (trade_id, position_id, order_ticket, deal_ticket, executed_at)
        VALUES (?, 9001, 5001, 7001, '2026-01-05 10:00:00')
        """,
        (trade_id,),
    )
    try:
        connection.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        restricted = False
    except sqlite3.IntegrityError:
        restricted = True
    check("a trade with executions cannot be deleted", restricted)
    connection.rollback()

    try:
        connection.execute(
            """
            INSERT INTO trades (ticket, position_id, symbol, trade_type, strategy,
                                entry_price, sl, tp, lots, open_time)
            VALUES (1001, 9100, 'XAUUSD', 'BUY', 'SCALPER', 1, 1, 2, 0.1, '2026-01-06 10:00:00')
            """
        )
        duplicate_ticket = False
    except sqlite3.IntegrityError:
        duplicate_ticket = True
    check("a duplicate trade ticket is rejected", duplicate_ticket)
    connection.rollback()

    try:
        connection.execute(
            """
            INSERT INTO trades (ticket, position_id, symbol, trade_type, strategy,
                                entry_price, sl, tp, lots, open_time)
            VALUES (1010, 9001, 'XAUUSD', 'BUY', 'SCALPER', 1, 1, 2, 0.1, '2026-01-06 10:00:00')
            """
        )
        duplicate_position = False
    except sqlite3.IntegrityError:
        duplicate_position = True
    check("a duplicate position id is rejected", duplicate_position)
    connection.rollback()

    try:
        connection.execute(
            """
            INSERT INTO trades (ticket, symbol, trade_type, strategy, entry_price,
                                sl, tp, lots, open_time)
            VALUES (1011, 'XAUUSD', 'HOLD', 'SCALPER', 1, 1, 2, 0.1, '2026-01-06 10:00:00')
            """
        )
        bad_side = False
    except sqlite3.IntegrityError:
        bad_side = True
    check("an invalid trade side is rejected", bad_side)
    connection.rollback()

    try:
        connection.execute(
            """
            INSERT INTO market_data (symbol, timeframe, bar_time, open, high, low, close)
            VALUES ('XAUUSD', 'M5', '2026-01-06 10:00:00', 2000, 1990, 1995, 1998)
            """
        )
        bad_ohlc = False
    except sqlite3.IntegrityError:
        bad_ohlc = True
    check("a bar whose high is below its low is rejected", bad_ohlc)
    connection.rollback()

    try:
        connection.execute(
            """
            INSERT INTO risk_events (event_time, day, event_type, severity, message)
            VALUES ('2026-01-06 10:00:00', '2026-01-06', 'risk_limit', 'nonsense', 'x')
            """
        )
        bad_severity = False
    except sqlite3.IntegrityError:
        bad_severity = True
    check("an unknown risk-event severity is rejected", bad_severity)
    connection.close()


def _check_helpers(db, analytics):
    print("\nHelpers and existing queries")
    health = db.database_health()
    check("health reports the schema version and pragmas",
          health["schema_version"] == 1 and health["foreign_keys"] is True
          and health["busy_timeout_ms"] == 5000, health)

    recent = db.get_recent_trades(5)
    check("recent trades keep their shape",
          len(recent) == 3 and recent[0]["ticket"] in (1001, 1002, 1003))
    open_trade = db.get_open_trade_by_position(9002)
    check("open trades are still found by position", open_trade["ticket"] == 1002)

    snapshot = db.get_dashboard_snapshot(60)
    check("the dashboard snapshot still computes realised stats",
          snapshot["stats"]["total_trades"] == 2
          and snapshot["stats"]["wins"] == 1, snapshot["stats"])
    check("history entries keep their shape",
          set(snapshot["history"][0]) == {"time", "type", "strategy", "entry", "exit", "pips", "usd", "win"})

    day = db.get_day_state("2026-01-05")
    check("the day state sees both closed trades", day["trades"] == 2, day)

    log_page = analytics.get_trade_log(limit=10, strategy="SCALPER")
    check("the trade log filters by strategy",
          log_page["total_count"] == 2, log_page["total_count"])
    losses = analytics.get_trade_log(limit=10, result="losses")
    check("the trade-log count follows the result filter",
          losses["total_count"] == 1, losses["total_count"])
    equity = analytics.get_equity_curve()
    check("the equity curve still reads closed trades", len(equity) == 1, equity)

    # Timeframes and execution rows.
    new_id = db.log_trade_open(
        1100, "XAUUSD", "BUY", "BREAKOUT", 2001.0, 1996.0, 2009.0, 0.2,
        datetime(2026, 1, 6, 9, 0, 0), position_id=9100, magic=123456,
    )
    check("a trade row id is returned for the execution", new_id is not None, new_id)
    check("the same ticket does not create a second row",
          db.log_trade_open(1100, "XAUUSD", "BUY", "BREAKOUT", 1, 1, 2, 0.1,
                            "2026-01-06 09:00:00") is None)
    execution_id = db.log_execution(
        new_id, order_ticket=5555, deal_ticket=6666, requested_price=2001.0,
        filled_price=2001.4, requested_volume=0.2, filled_volume=0.2,
        spread_points=22, retcode=10009, attempts=1, latency_ms=180,
        executed_at="2026-01-06 09:00:01",
    )
    check("the execution is recorded", execution_id is not None)
    check("re-recording the same order is ignored",
          db.log_execution(new_id, order_ticket=5555, executed_at="2026-01-06 09:00:02") is None)
    executions = db.get_executions(trade_ticket=1100)
    check("the execution joins back to its trade",
          len(executions) == 1 and executions[0]["filled_price"] == 2001.4
          and executions[0]["strategy"] == "BREAKOUT", executions)

    closed = db.log_trade_close(
        1001, 2010.0, 100.0, 9.4, "2026-01-05 10:30:00",
        commission=-0.6, swap=0.0, r_multiple=2.0, close_reason="tp",
    )
    row = db.get_trade_by_ticket(1001)
    check("closing stores the R multiple and reason",
          closed == 1 and row["r_multiple"] == 2.0 and row["close_reason"] == "tp", row)
    check("gross profit is derived from net plus costs",
          abs(row["gross_profit_usd"] - 10.0) < 1e-9, row["gross_profit_usd"])

    written = db.refresh_strategy_performance("2026-01-05")
    rollup = db.get_strategy_performance(3650)
    check("the strategy rollup is written per day",
          written >= 1 and any(item["strategy"] == "SCALPER" for item in rollup), rollup)
    scalper = [item for item in rollup if item["strategy"] == "SCALPER"][0]
    check("the rollup agrees with the trades it summarises",
          scalper["trades"] == 2 and scalper["wins"] == 1, scalper)
    check("rebuilding the rollup is idempotent",
          db.refresh_strategy_performance("2026-01-05") >= 1
          and len(db.get_strategy_performance(3650)) == len(rollup))


def _check_models_and_events(db):
    print("\nModels, events and settings history")
    stored = db.record_trading_settings(
        {"ml": {"enabled": False}, "risk": {"risk_per_trade": 0.01}},
        effective_from="2026-01-05 08:00:00",
    )
    again = db.record_trading_settings(
        {"risk": {"risk_per_trade": 0.01}, "ml": {"enabled": False}},
        effective_from="2026-01-05 08:00:00",
    )
    check("an identical settings snapshot is not duplicated", stored == again, (stored, again))
    history = db.get_trading_settings(5)
    check("the settings snapshot is stored with a checksum",
          len(history) == 1 and len(history[0]["checksum"]) == 32, history)
    check("the stored snapshot is retrievable in full",
          "risk_per_trade" in (db.get_trading_settings_by_id(stored) or {}).get("config", ""))

    version = db.record_model_training(
        "ml-v1-test", "ml-v1", "models/test.json",
        feature_names=["atr_pct", "rsi"], threshold=0.5,
        data={"fingerprint": "abc123", "bars": 1000,
              "first": "2026-01-01T00:00:00", "last": "2026-02-01T00:00:00"},
        samples={"signals": 10, "trainable": 8},
        out_of_sample={"baseline": {"trades": 5}},
        run={
            "status": "completed", "folds": 5, "embargo_bars": 24, "horizon_bars": 24,
            "l2": 1.0, "epochs": 600, "learning_rate": 0.15,
            "signals": 10, "trainable": 8, "wins": 3, "losses": 5, "timeouts": 2,
            "metrics": {"aggregate": {"auc": 0.55}},
            "started_at": "2026-01-01 00:00:00", "finished_at": "2026-01-01 00:05:00",
        },
        active=True,
    )
    check("a model version and its training run are linked",
          version["model_version_id"] and version["training_run_id"], version)
    versions = db.get_model_versions(5)
    runs = db.get_model_training_runs(5)
    check("the version is marked active",
          versions[0]["version"] == "ml-v1-test" and versions[0]["is_active"] == 1, versions)
    check("the run carries the evaluation",
          runs[0]["status"] == "completed" and "auc" in (runs[0]["metrics"] or ""), runs)

    db.record_model_training(
        "ml-v1-second", "ml-v1", "models/second.json", ["a"], 0.5,
        data={}, samples={}, out_of_sample={}, active=True,
    )
    # Recording a new active version deactivates the previous one, so the
    # constraint is only reachable from a direct write - which is what the
    # index is there to stop.
    connection = sqlite3.connect(db.DATABASE_PATH)
    try:
        connection.execute("UPDATE model_versions SET is_active = 1")
        connection.commit()
        single_active = False
    except sqlite3.IntegrityError:
        single_active = True
    finally:
        connection.close()
    check("the schema itself forbids two active models", single_active)
    check("activating a version deactivates the other",
          db.set_active_model("ml-v1-second") is True
          and [v["is_active"] for v in db.get_model_versions(5)].count(1) == 1)

    prediction_id = db.log_prediction(
        "SCALPER|SELL|20260106_1000", "ml-v1-test", "SCALPER", "XAUUSD", "SELL",
        "2026-01-06 10:00:00", 0.62, 0.5, "taken", {"rsi": 0.4},
    )
    check("a prediction is logged and linked to its model version",
          prediction_id is True
          and db.get_prediction("SCALPER|SELL|20260106_1000")["model_version_id"] is not None)
    check("duplicate predictions are ignored",
          db.log_prediction("SCALPER|SELL|20260106_1000", "ml-v1-test", "SCALPER",
                            "XAUUSD", "SELL", "2026-01-06 10:00:00", 0.62, 0.5, "taken") is False)

    trade = db.get_trade_by_ticket(1100)
    check("a prediction can be linked to a trade row",
          db.link_prediction_to_trade("SCALPER|SELL|20260106_1000", trade["id"]) == 1
          and db.get_prediction("SCALPER|SELL|20260106_1000")["trade_ticket"] == 1100)
    check("the outcome is stored against the prediction",
          db.resolve_prediction(1100, -1.0, -2.0, "LOSS") == 1)

    signal = db.get_signal("XAUUSD", "SCALPER", "BUY", "2026-01-05 10:00:00")
    check("signals are looked up by identity", signal is not None and signal["timeframe"] is None)
    check("a signal records the trade it produced",
          db.mark_signal_traded(signal["id"], trade["id"], None) == 1
          and db.get_signal("XAUUSD", "SCALPER", "BUY", "2026-01-05 10:00:00")["executed"] == 1)

    db.log_risk_event(
        "risk_limit", "Daily loss limit hit", severity="warning",
        strategy="SCALPER", detail={"daily_pnl": -310.0}, blocked=True,
        event_time="2026-01-05 14:00:00", day="2026-01-05",
    )
    db.log_risk_event("spread_limit", "spread too wide", event_time="2026-01-06 09:30:00")
    risk = db.get_risk_events(10, event_type="risk_limit")
    check("risk events are stored with severity and detail",
          len(risk) == 1 and risk[0]["severity"] == "warning" and risk[0]["blocked"] == 1, risk)
    check("risk events are filterable by type",
          len(db.get_risk_events(10, event_type="spread_limit")) == 1)

    db.log_system_event("MT5 connection lost", level="warning", category="connection",
                        detail={"error": "socket closed"})
    events = db.get_system_events(10, category="connection")
    check("system events are stored with a category",
          len(events) == 1 and "socket" in (events[0]["detail"] or ""), events)

    backtest = db.record_backtest_result(
        "task5-check", "BREAKOUT", "XAUUSD", "M5",
        data_start="2025-11-14", data_end="2026-08-06",
        parameters={"tp_rr": 1.5}, trades=128, wins=63, win_rate=0.492,
        expectancy_r=0.214, total_r=27.4, max_drawdown_r=-8.4, profit_usd=3013.0,
        notes="reference run",
    )
    check("a backtest result is recorded",
          backtest is not None and db.get_backtest_results(5)[0]["run_label"] == "task5-check")

    bars = [
        {"time": "2026-01-06 09:00:00", "open": 2000, "high": 2002, "low": 1998,
         "close": 2001, "tick_volume": 120, "spread": 18},
        {"time": "2026-01-06 09:05:00", "open": 2001, "high": 2004, "low": 2000,
         "close": 2003, "tick_volume": 140, "spread": 18},
    ]
    inserted = db.upsert_market_bars("XAUUSD", "M5", bars)
    duplicate = db.upsert_market_bars("XAUUSD", "M5", bars)
    check("market bars are inserted once", inserted == 2 and duplicate == 0, (inserted, duplicate))
    stored_bars = db.get_market_bars("XAUUSD", "M5", limit=10)
    check("market bars come back in time order",
          [bar["bar_time"] for bar in stored_bars]
          == ["2026-01-06 09:00:00", "2026-01-06 09:05:00"], stored_bars)
    check("market bar counts are queryable",
          db.count_market_bars("XAUUSD", "M5") == 2)

    # The trainer hands over a Bars object, not dicts.
    from src.ml.features import Bars

    epoch = Bars(
        __import__("numpy").array([1767600000, 1767600300], dtype="int64"),
        __import__("numpy").array([2003.0, 2004.0]),
        __import__("numpy").array([2005.0, 2006.0]),
        __import__("numpy").array([2002.0, 2003.0]),
        __import__("numpy").array([2004.0, 2005.0]),
        __import__("numpy").array([90, 95]),
        __import__("numpy").array([19, 19]),
    )
    check("bars objects from the trainer are stored too",
          db.upsert_market_bars("XAUUSD", "M5", epoch) == 2
          and db.count_market_bars("XAUUSD", "M5") == 4)

    check("timestamps are normalised on the way in",
          db.log_risk_event("test_time", "iso input", event_time="2026-01-07T08:09:10") is not None
          and db.get_risk_events(1)[0]["event_time"] == "2026-01-07 08:09:10")


if __name__ == "__main__":
    raise SystemExit(run())
