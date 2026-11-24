"""Smoke test for the live pipeline.

Stubs MetaTrader5 and pandas so the reconciliation, risk-manager and execution
paths can be checked without a terminal, a demo account or the pandas install.

Run: python tests/test_live_pipeline.py
"""
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SERVER_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _install_stubs():
    """Put fake MetaTrader5/pandas modules in sys.modules and return the fake state."""
    mt5 = types.ModuleType("MetaTrader5")
    mt5.TIMEFRAME_M5 = 5
    mt5.POSITION_TYPE_BUY = 0
    mt5.POSITION_TYPE_SELL = 1
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TIME_GTC = 0
    mt5.ORDER_FILLING_IOC = 1
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.DEAL_ENTRY_IN = 0
    mt5.DEAL_ENTRY_OUT = 1
    mt5.DEAL_ENTRY_INOUT = 2
    mt5.DEAL_ENTRY_OUT_BY = 3

    fake = {"positions": [], "orders": [], "history": [], "opening_deals": {}}

    mt5.initialize = lambda *a, **k: True
    mt5.shutdown = lambda *a, **k: True
    mt5.account_info = lambda: types.SimpleNamespace(balance=10000.0)
    mt5.positions_get = lambda symbol=None: list(fake["positions"])
    mt5.symbol_info_tick = lambda symbol=None: types.SimpleNamespace(
        bid=2000.0, ask=2000.3, time=int(datetime.now().timestamp())
    )
    mt5.symbol_info = lambda symbol=None: types.SimpleNamespace(
        trade_contract_size=100.0, volume_min=0.01, volume_max=10.0, volume_step=0.01
    )

    def order_send(request):
        fake["orders"].append(request)
        n = len(fake["orders"])
        return types.SimpleNamespace(
            retcode=mt5.TRADE_RETCODE_DONE, order=1000 + n, deal=5000 + n, comment="ok"
        )

    mt5.order_send = order_send

    def history_deals_get(*args, **kwargs):
        if "ticket" in kwargs:
            deal = fake["opening_deals"].get(kwargs["ticket"])
            return [deal] if deal else None
        return list(fake["history"])

    mt5.history_deals_get = history_deals_get

    pandas = types.ModuleType("pandas")
    pandas.DataFrame = lambda *a, **k: None
    pandas.to_datetime = lambda *a, **k: None
    pandas.isna = lambda value: value is None

    sys.modules["MetaTrader5"] = mt5
    sys.modules["pandas"] = pandas
    return mt5, fake


def _trade(side, entry, sl, tp, age_seconds):
    return {
        "type": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "time": SERVER_NOW - timedelta(seconds=age_seconds),
        "strategy": "BREAKOUT",
    }


def main():
    mt5, fake = _install_stubs()

    import src.engine.database as database
    import src.engine.live_trader as live_module

    tmp = Path(tempfile.mkdtemp())
    database.DATABASE_PATH = tmp / "test.db"
    live_module.STATE_FILE = tmp / "bot_state.json"

    from src.engine.live_trader import LiveTrader
    from src.strategies.risk_manager import RiskManager

    web_state = {"upcoming": [], "open_positions": [], "trade_history": []}
    trader = LiveTrader(web_state=web_state)
    checks = []

    def check(label, condition):
        checks.append((label, bool(condition)))
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    # 1. A stop that price has already passed must not be sent.
    trader.place_trade(_trade("BUY", 2000.0, 2005.0, 2020.0, 5))
    check("invalid SL (already through price) is rejected", len(fake["orders"]) == 0)

    # 2. A valid order is stored with its MT5 position ID.
    fake["opening_deals"][5001] = types.SimpleNamespace(ticket=5001, position_id=9001)
    trader.place_trade(_trade("BUY", 2000.0, 1995.0, 2010.0, 5))
    rows = database.get_recent_trades(10)
    check("order was sent", len(fake["orders"]) == 1)
    check("trade stored as OPEN", len(rows) == 1 and rows[0]["status"] == "OPEN")
    check("trade stores the position ID", rows and rows[0]["position_id"] == 9001)

    # 3. The closing deal reconciles by position_id, not by order ticket.
    ticket = rows[0]["ticket"]
    fake["history"] = [
        types.SimpleNamespace(
            entry=mt5.DEAL_ENTRY_OUT, position_id=9001, order=999999,
            price=2005.0, time=int(datetime.now().timestamp()),
            profit=50.0, swap=-1.0, commission=-0.5,
        )
    ]
    trader._check_closed_positions()

    rows = database.get_recent_trades(10)
    closed = rows[0]
    check("trade reconciled to CLOSED", closed["status"] == "CLOSED")
    check("exit price recorded", abs((closed["exit_price"] or 0) - 2005.0) < 1e-9)
    expected_pips = (2005.0 - closed["entry_price"]) * 100  # entry is the real fill, not the signal price
    check("pips recorded", abs((closed["profit_pips"] or 0) - expected_pips) < 1e-6)
    check("net USD includes swap/commission", abs((closed["profit_usd"] or 0) - 48.5) < 1e-9)
    check("risk manager got the realised P/L", abs(trader.risk_mgr.daily_pnl - 48.5) < 1e-9)
    check("DB stats are real now", database.get_trade_stats()["total_trades"] == 1)
    trader._refresh_stats()  # exactly what the next loop pass does
    check("dashboard stats refreshed", web_state.get("total_trades") == 1 and web_state.get("win_rate") == 100.0)
    check("dashboard history populated", len(web_state.get("trade_history", [])) == 1)
    check("dashboard exposes risk headroom", web_state.get("risk_budget") == 300.0)

    # 4. Re-scanning history must not double count.
    trader._check_closed_positions()
    check("reconciliation is idempotent", abs(trader.risk_mgr.daily_pnl - 48.5) < 1e-9)

    # 5. Three losses in a row block new entries.
    rm = RiskManager(10000)
    for _ in range(3):
        rm.register_close(-10.0)
    allowed, reason = rm.can_trade()
    check("consecutive-loss stop fires", not allowed and "consecutive" in reason)

    # 6. Hitting the daily loss limit blocks new entries.
    rm = RiskManager(10000)
    rm.register_close(-301.0)
    allowed, reason = rm.can_trade()
    check("daily-loss stop fires", not allowed and "Daily loss" in reason)

    # 8. The schema is created once per process, not on every query.
    database._INITIALIZED.clear()
    real_init = database.init_db
    init_calls = {"n": 0}

    def counted_init():
        init_calls["n"] += 1
        return real_init()

    database.init_db = counted_init
    for _ in range(5):
        database.get_trade_stats()
    database.init_db = real_init
    check("schema initialised once, not per query", init_calls["n"] == 1)

    # 9. One stats build per pass, even when a close happens in that pass.
    fake["opening_deals"][5002] = types.SimpleNamespace(ticket=5002, position_id=9002)
    trader.place_trade(_trade("SELL", 2000.0, 2005.0, 1990.0, 5))
    fake["history"].append(
        types.SimpleNamespace(
            entry=mt5.DEAL_ENTRY_OUT, position_id=9002, order=999998,
            price=1995.0, time=int(datetime.now().timestamp()),
            profit=-25.0, swap=0.0, commission=-0.5,
        )
    )

    snapshots = {"n": 0}
    real_snapshot = live_module.get_dashboard_snapshot

    def counted_snapshot(limit=60):
        snapshots["n"] += 1
        return real_snapshot(limit)

    live_module.get_dashboard_snapshot = counted_snapshot
    trader.get_data = lambda *a, **k: object()
    trader._generate_signals = lambda df: []
    trader._stop = False
    live_module.time.sleep = lambda seconds: setattr(trader, "_stop", True)
    trader.run_web()
    live_module.get_dashboard_snapshot = real_snapshot

    check("one stats build per pass", snapshots["n"] == 1)
    check("close in that pass was reconciled", database.get_recent_trades(5)[0]["status"] == "CLOSED")

    # 7. Fresh signals trade immediately; stale ones do not.
    executed = []
    fresh = _trade("BUY", 2000.0, 1995.0, 2010.0, 5)
    stale = _trade("SELL", 2000.0, 2005.0, 1990.0, 3600)

    trader.get_data = lambda *a, **k: object()
    trader._generate_signals = lambda df: [fresh, stale]
    trader.place_trade = executed.append
    trader._stop = False
    live_module.time.sleep = lambda seconds: setattr(trader, "_stop", True)
    trader.run_web()

    check("5-second-old signal executes immediately", fresh in executed)
    check("1-hour-old signal is refused", stale not in executed)

    # 10. A mid-day restart keeps the daily-loss stop armed.
    restarted = LiveTrader(web_state={"upcoming": [], "open_positions": [], "trade_history": []})
    today = database.get_day_state(live_module.LiveTrader._today(restarted))
    check("restart restores today's realised P/L", abs(restarted.risk_mgr.daily_pnl - today["realised_usd"]) < 1e-9)
    check("restart restores the loss streak", restarted.risk_mgr.consecutive_losses == today["trailing_losses"] > 0)
    check("restart restores the day-start balance", restarted.risk_mgr.day_start_balance > 0)

    restarted._save_state()
    saved = json.loads(live_module.STATE_FILE.read_text())
    check("day-start balance is persisted", "day_start_balance" in saved and saved["day_start_balance"] > 0)
    check("daily P&L is persisted", "daily_pnl" in saved)

    failed = [label for label, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print(f"DB rows: {database.get_recent_trades(5)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
