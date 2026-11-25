# src/engine/analytics.py
"""Read-only queries behind the dashboard.

Every figure the terminal shows is computed here from the `trades` table - there
are no sample values in this module. A query with no matching rows returns
zeroed structures plus an explicit ``trades: 0``, which is what lets the UI say
"nothing measured yet" instead of drawing a flat line that looks like a result.

Closed trades are the unit of measurement. A trade counts as a win when
``profit_usd > 0`` for money aggregates and ``profit_pips > 0`` for pip
aggregates; the two agree in practice because both are written on close.
"""

from datetime import datetime, timedelta

from src.engine.database import _connect as get_connection, _ensure_ready


def _range(start=None, end=None):
    """SQL fragment for an inclusive close-date window.

    Dates are ``YYYY-MM-DD`` strings compared against ``date(close_time)``, the
    same convention the rest of the engine writes (broker clock). Missing
    bounds are simply omitted, so an all-time query stays an all-time query.
    """
    clause = ""
    params = []
    if start:
        clause += " AND date(close_time) >= ?"
        params.append(str(start))
    if end:
        clause += " AND date(close_time) <= ?"
        params.append(str(end))
    return clause, params


def _closed_where(start=None, end=None):
    clause, params = _range(start, end)
    return "WHERE status = 'CLOSED'" + clause, params


def get_daily_summary():
    _ensure_ready()
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN profit_pips <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(profit_pips), 0) as total_pips,
            COALESCE(SUM(profit_usd), 0) as total_profit,
            MIN(profit_pips) as worst_trade,
            MAX(profit_pips) as best_trade
        FROM trades
        WHERE status = 'CLOSED' AND date(close_time) = ?
    """, (today,)).fetchone()
    conn.close()

    if row and row["total_trades"] > 0:
        win_rate = (row["wins"] / row["total_trades"]) * 100
    else:
        win_rate = 0

    return {
        "total_trades": row["total_trades"] or 0,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "win_rate": round(win_rate, 1),
        "total_pips": round(row["total_pips"] or 0, 1),
        "total_profit": round(row["total_profit"] or 0, 2),
        "best_trade": round(row["best_trade"] or 0, 1),
        "worst_trade": round(row["worst_trade"] or 0, 1),
    }


def get_weekly_comparison():
    _ensure_ready()
    conn = get_connection()
    today = datetime.now()
    this_week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    last_week_start = (today - timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")
    last_week_end = (today - timedelta(days=today.weekday() + 1)).strftime("%Y-%m-%d")

    this_week = conn.execute("""
        SELECT COALESCE(SUM(profit_pips), 0) as pips, COUNT(*) as trades
        FROM trades WHERE status = 'CLOSED' AND date(close_time) >= ?
    """, (this_week_start,)).fetchone()

    last_week = conn.execute("""
        SELECT COALESCE(SUM(profit_pips), 0) as pips, COUNT(*) as trades
        FROM trades WHERE status = 'CLOSED' AND date(close_time) >= ? AND date(close_time) <= ?
    """, (last_week_start, last_week_end)).fetchone()
    conn.close()

    return {
        "this_week_pips": round(this_week["pips"] or 0, 1),
        "this_week_trades": this_week["trades"] or 0,
        "last_week_pips": round(last_week["pips"] or 0, 1),
        "last_week_trades": last_week["trades"] or 0,
    }


def get_strategy_breakdown(start=None, end=None):
    _ensure_ready()
    conn = get_connection()
    where, params = _closed_where(start, end)
    rows = conn.execute(f"""
        SELECT
            strategy,
            COUNT(*) as total_trades,
            SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(profit_pips), 0) as total_pips,
            COALESCE(SUM(profit_usd), 0) as total_profit,
            COALESCE(AVG(r_multiple), 0) as avg_r
        FROM trades
        {where}
        GROUP BY strategy
        ORDER BY total_profit DESC
    """, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        win_rate = (r["wins"] / r["total_trades"] * 100) if r["total_trades"] > 0 else 0
        results.append({
            "strategy": r["strategy"],
            "total_trades": r["total_trades"],
            "wins": r["wins"],
            "win_rate": round(win_rate, 1),
            "total_pips": round(r["total_pips"] or 0, 1),
            "total_profit": round(r["total_profit"] or 0, 2),
            "avg_r": round(r["avg_r"] or 0, 3),
        })
    return results


def get_session_analysis(start=None, end=None):
    _ensure_ready()
    conn = get_connection()
    where, params = _closed_where(start, end)
    rows = conn.execute(f"""
        SELECT
            strftime('%H', close_time) as hour,
            COUNT(*) as trades,
            SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(profit_pips), 0) as pips,
            COALESCE(SUM(profit_usd), 0) as profit
        FROM trades
        {where}
        GROUP BY hour
        ORDER BY hour
    """, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        win_rate = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        results.append({
            "hour": int(r["hour"] or 0),
            "trades": r["trades"],
            "wins": r["wins"],
            "win_rate": round(win_rate, 1),
            "pips": round(r["pips"] or 0, 1),
            "profit": round(r["profit"] or 0, 2),
        })
    return results


def get_equity_curve(start=None, end=None):
    _ensure_ready()
    conn = get_connection()
    where, params = _closed_where(start, end)
    rows = conn.execute(f"""
        SELECT
            date(close_time) as day,
            COALESCE(SUM(profit_usd), 0) as daily_profit,
            COUNT(*) as trades
        FROM trades
        {where}
        GROUP BY day
        ORDER BY day
    """, params).fetchall()
    conn.close()

    equity = []
    running_total = 0.0
    peak = 0.0
    for r in rows:
        running_total += r["daily_profit"] or 0
        peak = max(peak, running_total)
        equity.append({
            "day": r["day"],
            "daily_profit": round(r["daily_profit"] or 0, 2),
            "trades": r["trades"],
            "cumulative": round(running_total, 2),
            "drawdown": round(min(running_total - peak, 0), 2),
        })
    return equity


def get_monthly_series(start=None, end=None):
    _ensure_ready()
    conn = get_connection()
    where, params = _closed_where(start, end)
    rows = conn.execute(f"""
        SELECT
            strftime('%Y-%m', close_time) as month,
            COUNT(*) as trades,
            SUM(CASE WHEN profit_usd > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(profit_usd), 0) as net,
            COALESCE(SUM(profit_pips), 0) as pips
        FROM trades
        {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()
    conn.close()

    months = []
    running = 0.0
    for r in rows:
        running += r["net"] or 0
        months.append({
            "month": r["month"],
            "trades": r["trades"],
            "wins": r["wins"] or 0,
            "win_rate": round((r["wins"] or 0) / r["trades"] * 100, 1) if r["trades"] else 0.0,
            "net": round(r["net"] or 0, 2),
            "pips": round(r["pips"] or 0, 1),
            "cumulative": round(running, 2),
        })
    return months


def get_performance_summary(start=None, end=None, starting_balance=None):
    """The headline numbers: win rate, profit factor, drawdown, streaks.

    ``max_drawdown`` is computed on the realised equity path, not on the
    account equity, so open positions never flatter it. ``profit_factor`` is
    ``None`` when there is no losing trade to divide by: rendering "inf" is the
    honest answer there, and 0.00 would be a lie.
    """
    _ensure_ready()
    conn = get_connection()
    where, params = _closed_where(start, end)
    rows = conn.execute(f"""
        SELECT profit_usd, profit_pips, r_multiple, close_time, strategy
        FROM trades
        {where}
        ORDER BY close_time
    """, params).fetchall()
    conn.close()

    trades = len(rows)
    profits = [float(r["profit_usd"] or 0) for r in rows]
    pips = [float(r["profit_pips"] or 0) for r in rows]
    r_values = [float(r["r_multiple"]) for r in rows if r["r_multiple"] is not None]

    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    flat = [p for p in profits if p == 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(profits)

    win_streak = loss_streak = 0
    best_win_streak = best_loss_streak = 0
    for value in profits:
        if value > 0:
            win_streak += 1
            loss_streak = 0
            best_win_streak = max(best_win_streak, win_streak)
        elif value < 0:
            loss_streak += 1
            win_streak = 0
            best_loss_streak = max(best_loss_streak, loss_streak)
        else:
            win_streak = loss_streak = 0

    running = peak = 0.0
    max_dd = 0.0
    for value in profits:
        running += value
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    drawdown_pct = None
    base = (starting_balance or 0) + peak
    if starting_balance and base > 0:
        drawdown_pct = round(max_dd / base * 100, 2)

    return {
        "start": start,
        "end": end,
        "trades": trades,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(flat),
        "win_rate": round(len(wins) / trades * 100, 1) if trades else 0.0,
        "net_profit": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_trade": round(net / trades, 2) if trades else 0.0,
        "best_trade": round(max(profits), 2) if profits else 0.0,
        "worst_trade": round(min(profits), 2) if profits else 0.0,
        "expectancy_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "total_r": round(sum(r_values), 2) if r_values else None,
        "r_samples": len(r_values),
        "total_pips": round(sum(pips), 1),
        "longest_win_streak": best_win_streak,
        "longest_loss_streak": best_loss_streak,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": drawdown_pct,
    }


def get_trade_log(limit=100, offset=0, strategy=None, result=None, start=None, end=None):
    _ensure_ready()
    conn = get_connection()
    # The filter is shared by the page query and the count, so pagination counts
    # what is actually being paged instead of every closed trade.
    where, params = _closed_where(start, end)

    if strategy and strategy != "all":
        where += " AND strategy = ?"
        params.append(strategy)

    if result == "wins":
        where += " AND profit_usd > 0"
    elif result == "losses":
        where += " AND profit_usd <= 0"

    rows = conn.execute(
        f"SELECT * FROM trades {where} ORDER BY close_time DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    total_count = conn.execute(
        f"SELECT COUNT(*) FROM trades {where}", params
    ).fetchone()[0]
    conn.close()

    return {"trades": [dict(r) for r in rows], "total_count": total_count}


def get_trade_detail(trade_id):
    """One trade with the execution, signal and prediction rows behind it."""
    _ensure_ready()
    conn = get_connection()
    trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if trade is None:
        conn.close()
        return None

    executions = conn.execute(
        "SELECT * FROM trade_executions WHERE trade_id = ? ORDER BY id", (trade_id,)
    ).fetchall()
    signal = None
    if trade["signal_id"]:
        signal = conn.execute(
            "SELECT * FROM signals WHERE id = ?", (trade["signal_id"],)
        ).fetchone()
    prediction = None
    if trade["ml_prediction_id"]:
        prediction = conn.execute(
            "SELECT * FROM ml_predictions WHERE id = ?", (trade["ml_prediction_id"],)
        ).fetchone()
    conn.close()

    return {
        "trade": dict(trade),
        "executions": [dict(row) for row in executions],
        "signal": dict(signal) if signal else None,
        "prediction": dict(prediction) if prediction else None,
    }
