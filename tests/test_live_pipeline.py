"""Smoke test for the live pipeline.

Stubs MetaTrader5 and pandas so the reconciliation, risk-manager and execution
paths can be checked without a terminal, a demo account or the pandas install.

Run: python tests/test_live_pipeline.py
"""
import json
import sys
import tempfile
import time
import types
from dataclasses import replace
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

    fake = {
        "positions": [], "orders": [], "pending": [], "history": [],
        "opening_deals": {},
    }

    mt5.initialize = lambda *a, **k: True
    mt5.shutdown = lambda *a, **k: True
    mt5.symbol_select = lambda *a, **k: True
    mt5.order_check = lambda request: types.SimpleNamespace(retcode=0, comment="ok")
    mt5.account_info = lambda: types.SimpleNamespace(
        login=777, server="RCG-Demo", company="RCG Markets", currency="ZAR",
        leverage=500, balance=10000.0, equity=10000.0, margin_free=10000.0,
        trade_allowed=True, trade_expert=True,
    )
    mt5.terminal_info = lambda: types.SimpleNamespace(
        name="MetaTrader 5", company="MetaQuotes Software Corp.",
        path="C:/Program Files/MetaTrader 5/terminal64.exe",
        connected=True, trade_allowed=True, build=4260,
    )
    mt5.positions_get = lambda symbol=None: list(fake["positions"])
    mt5.orders_get = lambda symbol=None: list(fake["pending"])
    mt5.symbol_info_tick = lambda symbol=None: types.SimpleNamespace(
        bid=2000.0, ask=2000.3, time=int(datetime.now().timestamp())
    )
    mt5.symbol_info = lambda symbol=None: types.SimpleNamespace(
        trade_contract_size=100.0, volume_min=0.01, volume_max=10.0, volume_step=0.01,
        point=0.01, digits=2, trade_stops_level=0, trade_mode=4, visible=True,
    )

    def order_send(request):
        fake["orders"].append(request)
        n = len(fake["orders"])
        # A real fill reports its own price and volume, which is what the trade
        # row must store instead of the signal's requested price.
        return types.SimpleNamespace(
            retcode=mt5.TRADE_RETCODE_DONE, order=1000 + n, deal=5000 + n,
            price=request.get("price"), volume=request.get("volume"), comment="ok",
        )

    mt5.order_send = order_send

    def history_deals_get(*args, **kwargs):
        if "ticket" in kwargs:
            deal = fake["opening_deals"].get(kwargs["ticket"])
            return [deal] if deal else None
        if "position" in kwargs:
            return [
                deal for deal in fake["history"]
                if getattr(deal, "position_id", None) == kwargs["position"]
            ]
        return list(fake["history"])

    mt5.history_deals_get = history_deals_get

    pandas = types.ModuleType("pandas")
    pandas.DataFrame = lambda *a, **k: None
    pandas.to_datetime = lambda *a, **k: None
    pandas.isna = lambda value: value is None

    sys.modules["MetaTrader5"] = mt5
    sys.modules["pandas"] = pandas
    return mt5, fake


def _trade(side, entry, sl, tp, age_seconds, strategy="BREAKOUT"):
    return {
        "type": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "time": SERVER_NOW - timedelta(seconds=age_seconds),
        "strategy": strategy,
    }


def _position(ticket, magic=123456, side=0, price=2000.0, sl=1995.0, tp=2010.0,
              volume=0.10, comment="IronScalper_BREAKOUT"):
    return types.SimpleNamespace(
        ticket=ticket, magic=magic, type=side, price_open=price, sl=sl, tp=tp,
        volume=volume, comment=comment, profit=0.0,
        time=int(datetime.now().timestamp()),
    )


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
    trader._check_closed_positions(force=True)

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
    trader._check_closed_positions(force=True)
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

    # 9a. The deal-history scan is throttled instead of running every pass.
    scan_calls = {"n": 0}
    real_history = mt5.history_deals_get

    def counted_history(*args, **kwargs):
        if "ticket" not in kwargs:
            scan_calls["n"] += 1
        return real_history(*args, **kwargs)

    mt5.history_deals_get = counted_history
    trader._check_closed_positions()  # not forced: inside the throttle window
    mt5.history_deals_get = real_history
    check("history scan is throttled, not per pass", scan_calls["n"] == 0)

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
    trader._last_history_scan = 0.0  # pretend the throttle window has elapsed
    live_module.time.sleep = lambda seconds: setattr(trader, "_stop", True)
    trader.run_web()
    live_module.get_dashboard_snapshot = real_snapshot

    check("one stats build per pass", snapshots["n"] == 1)
    check("close in that pass was reconciled", database.get_recent_trades(5)[0]["status"] == "CLOSED")

    # 7. Fresh signals trade immediately; stale ones do not.
    executed = []
    fresh = _trade("BUY", 2000.0, 1995.0, 2010.0, 5)
    stale = _trade("SELL", 2000.0, 2005.0, 1990.0, 3600)

    real_place_trade = trader.place_trade
    trader.get_data = lambda *a, **k: object()
    trader._generate_signals = lambda df: [fresh, stale]
    trader.place_trade = executed.append
    trader._stop = False
    live_module.time.sleep = lambda seconds: setattr(trader, "_stop", True)
    trader.run_web()
    trader.place_trade = real_place_trade

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

    # 11. A partial close settles the trade once, with the whole result.
    trader._stop = False  # the loop above stopped it
    fake["opening_deals"][5003] = types.SimpleNamespace(ticket=5003, position_id=9003)
    trader.place_trade(_trade("BUY", 2000.0, 1995.0, 2020.0, 5, strategy="SCALPER"))
    opened = [
        row for row in database.get_recent_trades(10) if row["position_id"] == 9003
    ][0]
    check("take profit is the strategy's level", abs(opened["tp"] - 2020.0) < 1e-9)

    close_epoch = int(datetime.now().timestamp())
    fake["history"].extend([
        types.SimpleNamespace(
            entry=mt5.DEAL_ENTRY_OUT, position_id=9003, order=77, price=2010.0,
            volume=0.10, time=close_epoch, profit=50.0, swap=0.0, commission=-0.5,
        ),
        types.SimpleNamespace(
            entry=mt5.DEAL_ENTRY_OUT, position_id=9003, order=78, price=2014.0,
            volume=0.10, time=close_epoch + 1, profit=70.0, swap=0.0, commission=-0.5,
        ),
    ])
    trader._check_closed_positions(force=True)
    settled = [
        row for row in database.get_recent_trades(10) if row["position_id"] == 9003
    ][0]
    check("partial close settles once", settled["status"] == "CLOSED")
    check("partial close sums every deal's net", abs(settled["profit_usd"] - 119.0) < 1e-9)
    check("partial close price is volume weighted", abs(settled["exit_price"] - 2012.0) < 1e-9)

    # 12. Losing the terminal is reported, then recovered by the backoff loop.
    real_account = mt5.account_info
    mt5.account_info = lambda: None
    down = trader.broker.poll()
    check("a dead terminal reports disconnected, not an exception", not down.healthy)

    mt5.account_info = real_account
    trader.broker._backoff = 0.0
    trader.broker._last_attempt = -1e9
    up = trader.broker.poll()
    check("the broker reconnects on its own", up.healthy and trader.broker.reconnects >= 1)

    # 13. A candle-feed outage must not freeze the stats on the dashboard.
    trader.get_data = lambda *a, **k: None
    trader._stop = False
    live_module.time.sleep = lambda seconds: setattr(trader, "_stop", True)
    trader.run_web()
    check("stats still published when the feed is down", web_state.get("total_trades", 0) >= 1)
    check("connection health is published", web_state.get("mt5_connected") is True)
    check("reconnect count is published", web_state.get("mt5_reconnects", 0) >= 1)

    # ── MT5 identity, permissions, synchronisation and the central config ──
    from config.settings import CONFIG
    from src.engine.broker import Broker

    trader._stop = False

    # 14. The terminal must be signed into the account we expect.
    wrong_account = Broker(symbol="XAUUSD", magic=123456, expected_login=424242)
    check(
        "a terminal signed into another account is refused",
        wrong_account.connect() is False
        and "expected 424242" in (wrong_account.last_error or ""),
    )

    # 15. Algo trading switched off in the terminal is refused at connect.
    good_terminal = mt5.terminal_info
    mt5.terminal_info = lambda: types.SimpleNamespace(
        name="MetaTrader 5", company="MetaQuotes", path="C:/MT5/terminal64.exe",
        connected=True, trade_allowed=False, build=4260,
    )
    algo_off = Broker(symbol="XAUUSD", magic=123456)
    check(
        "algorithmic trading disabled is refused at connect",
        algo_off.connect() is False
        and "algorithmic" in (algo_off.last_error or "").lower(),
    )
    mt5.terminal_info = good_terminal

    # 16. The dashboard is given the real terminal, account and symbol facts.
    trader._readiness = trader.broker.readiness()
    trader._publish_connection(trader.broker.poll())
    check("account identity reaches the dashboard", web_state["mt5_account"].get("login") == 777)
    check(
        "terminal identity reaches the dashboard",
        web_state["mt5_terminal"].get("name") == "MetaTrader 5",
    )
    check(
        "symbol specification reaches the dashboard",
        web_state["mt5_symbol"].get("contract_size") == 100.0
        and web_state["mt5_symbol"].get("trade_mode_text") == "full",
    )
    check(
        "the engine publishes the config it trades with",
        web_state.get("config", {}).get("breakout", {}).get("atr_multiplier")
        == CONFIG.breakout.atr_multiplier,
    )

    # 17. A symbol the broker has restricted blocks entries, with the reason.
    real_symbol_info = mt5.symbol_info
    mt5.symbol_info = lambda symbol=None: types.SimpleNamespace(
        trade_contract_size=100.0, volume_min=0.01, volume_max=10.0, volume_step=0.01,
        point=0.01, digits=2, trade_stops_level=0, trade_mode=1, visible=True,
    )
    trader._readiness = trader.broker.readiness()
    orders_before = len(fake["orders"])
    trader.place_trade(_trade("BUY", 2000.0, 1995.0, 2010.0, 5))
    check("a close-only symbol blocks entries", len(fake["orders"]) == orders_before)
    check(
        "the refusal names the symbol restriction",
        any("close only" in issue for issue in trader._readiness["issues"]),
    )
    mt5.symbol_info = real_symbol_info
    trader._readiness = trader.broker.readiness()

    # 18. A quote feed that stops ticking blocks entries.
    real_tick = mt5.symbol_info_tick
    frozen_epoch = int(datetime.now().timestamp()) - 600
    mt5.symbol_info_tick = lambda symbol=None: types.SimpleNamespace(
        bid=2000.0, ask=2000.3, time=frozen_epoch
    )
    trader.broker.poll()  # record the frozen tick
    trader.broker._last_tick_change = time.monotonic() - 120
    orders_before = len(fake["orders"])
    trader.place_trade(_trade("SELL", 2000.0, 2005.0, 1990.0, 5))
    check("a stale quote feed blocks entries", len(fake["orders"]) == orders_before)
    mt5.symbol_info_tick = real_tick
    trader.broker._last_tick_change = time.monotonic()

    # 19. Pending orders on the symbol are synchronised for display.
    fake["pending"] = [types.SimpleNamespace(
        ticket=555, type=2, volume_current=0.10, price_open=2010.0, sl=0.0, tp=0.0,
        symbol="XAUUSD", magic=123456,
    )]
    trader._publish_connection(trader.broker.poll())
    check(
        "pending orders are synchronised to the dashboard",
        len(web_state["pending_orders"]) == 1
        and web_state["pending_orders"][0]["type"] == "BUY_LIMIT",
    )
    fake["pending"] = []

    # 20. A position opened by hand is reported, not written into our trades.
    fake["positions"] = [_position(4242, magic=0, comment="")]
    manual_snapshot = trader.broker.poll()
    trader._sync_positions(manual_snapshot, force=True)
    trader._publish_connection(manual_snapshot)  # exactly what the loop publishes
    check("manual positions are counted for exposure", web_state["manual_positions"] == 1)
    check("manual positions are not recorded as bot trades", not database.get_open_trades())
    fake["positions"] = []

    # 21. A position closed outside the bot is settled by the periodic sync.
    # The stub numbers deals 5000+n, so register the one this order will get.
    next_deal = 5000 + len(fake["orders"]) + 1
    fake["opening_deals"][next_deal] = types.SimpleNamespace(
        ticket=next_deal, position_id=9010
    )
    # A different bar from the earlier breakout entry, or signal dedupe (correctly)
    # refuses it and there is nothing to settle.
    trader.place_trade(_trade("BUY", 2000.0, 1995.0, 2010.0, 300, strategy="BREAKOUT"))
    fake["history"].append(types.SimpleNamespace(
        entry=mt5.DEAL_ENTRY_OUT, position_id=9010, order=91, price=2007.0, volume=0.1,
        time=int(datetime.now().timestamp()), profit=70.0, swap=0.0, commission=-0.3,
    ))
    trader._sync_positions(trader.broker.poll(), force=True)
    exited = [row for row in database.get_recent_trades(10) if row["position_id"] == 9010]
    check(
        "an externally closed position is settled by the sync",
        exited and exited[0]["status"] == "CLOSED"
        and abs(exited[0]["profit_usd"] - 69.7) < 1e-9,
    )

    # 22. An order whose answer is lost must not be sent twice.
    real_send = mt5.order_send

    def exploding_send(request):
        fake["positions"].append(_position(7788, magic=123456, price=request["price"]))
        raise RuntimeError("terminal vanished mid-order")

    mt5.order_send = exploding_send
    orders_before = len(fake["orders"])
    recovered = trader.broker.send_order(
        {"action": 1, "symbol": "XAUUSD", "volume": 0.10, "price": 2000.0,
         "sl": 1995.0, "tp": 2010.0, "type": 0}, "BUY")
    mt5.order_send = real_send
    fake["positions"] = []
    check(
        "a lost order answer is resolved against MT5",
        recovered.ok and recovered.attempts == 1,
    )
    check(
        "a lost order answer does not produce a duplicate",
        len(fake["orders"]) == orders_before,
    )

    # 23. A fill that ignores the requested geometry is reported.
    honoured, notes = trader.broker.verify_fill(
        _position(7790, volume=0.05, sl=0.0, tp=0.0), 0.10, 1995.0, 2010.0
    )
    check("a fill that ignores volume and stops is reported", not honoured and len(notes) >= 3)

    # 24. The central config is valid, complete and JSON-safe.
    config_snapshot = CONFIG.snapshot()
    check(
        "config exposes every trading group",
        set(config_snapshot) >= {"risk", "execution", "mt5", "breakout", "scalper", "ml"},
    )
    check("config snapshot is JSON serialisable", isinstance(json.dumps(config_snapshot), str))
    check(
        "sessions are exposed as clock strings",
        config_snapshot["breakout"]["session"] == "07:00-12:00"
        and config_snapshot["scalper"]["session"] == "08:00-16:00",
    )
    try:
        replace(CONFIG, risk=replace(CONFIG.risk, risk_per_trade=2.0)).validate()
        rejected = False
    except ValueError:
        rejected = True
    check("an impossible risk setting is rejected at load", rejected)
    check(
        "risk limits come from the central config",
        trader.risk_mgr.max_concurrent == CONFIG.risk.max_open_positions
        and abs(trader.risk_mgr.max_daily_risk - CONFIG.risk.max_daily_loss_pct) < 1e-12,
    )

    # 25. The loss-streak stop is configurable, not a literal 3.
    strict = RiskManager(10000, max_consecutive_losses=2)
    strict.register_close(-10.0)
    strict.register_close(-10.0)
    allowed, reason = strict.can_trade()
    check("the loss-streak stop follows the config", not allowed and "consecutive" in reason)

    # 26. The database records what the engine did, not only the trade result.
    executions = database.get_executions(limit=20)
    check("an order execution is recorded with its fill", bool(executions) and executions[0]["order_ticket"] is not None)
    check("the configuration snapshot is stored", bool(database.get_trading_settings(5)))
    check("operational events are recorded", bool(database.get_system_events(20)))
    check("risk refusals are recorded", bool(database.get_risk_events(20)))
    check("strategy performance is rolled up", database.refresh_strategy_performance() >= 1)

    failed = [label for label, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print(f"DB rows: {database.get_recent_trades(5)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
