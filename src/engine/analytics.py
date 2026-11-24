# src/engine/analytics.py
from datetime import datetime, timedelta
from src.engine.database import _connect as get_connection


def get_daily_summary():
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


def get_strategy_breakdown():
    conn = get_connection()
    rows = conn.execute("""
        SELECT 
            strategy,
            COUNT(*) as total_trades,
            SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(profit_pips), 0) as total_pips,
            COALESCE(SUM(profit_usd), 0) as total_profit
        FROM trades 
        WHERE status = 'CLOSED'
        GROUP BY strategy
        ORDER BY total_profit DESC
    """).fetchall()
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
        })
    return results


def get_session_analysis():
    conn = get_connection()
    rows = conn.execute("""
        SELECT 
            strftime('%H', close_time) as hour,
            COUNT(*) as trades,
            SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(profit_pips), 0) as pips
        FROM trades 
        WHERE status = 'CLOSED'
        GROUP BY hour
        ORDER BY hour
    """).fetchall()
    conn.close()
    
    results = []
    for r in rows:
        win_rate = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        results.append({
            "hour": r["hour"],
            "trades": r["trades"],
            "wins": r["wins"],
            "win_rate": round(win_rate, 1),
            "pips": round(r["pips"] or 0, 1),
        })
    return results


def get_equity_curve():
    conn = get_connection()
    rows = conn.execute("""
        SELECT 
            date(close_time) as day,
            COALESCE(SUM(profit_usd), 0) as daily_profit,
            COUNT(*) as trades
        FROM trades 
        WHERE status = 'CLOSED'
        GROUP BY day
        ORDER BY day
    """).fetchall()
    conn.close()
    
    equity = []
    running_total = 0
    for r in rows:
        running_total += r["daily_profit"] or 0
        equity.append({
            "day": r["day"],
            "daily_profit": round(r["daily_profit"] or 0, 2),
            "trades": r["trades"],
            "cumulative": round(running_total, 2),
        })
    return equity


def get_trade_log(limit=100, offset=0, strategy=None, result=None):
    conn = get_connection()
    query = "SELECT * FROM trades WHERE status = 'CLOSED'"
    params = []
    
    if strategy and strategy != "all":
        query += " AND strategy = ?"
        params.append(strategy)
    
    if result == "wins":
        query += " AND profit_pips > 0"
    elif result == "losses":
        query += " AND profit_pips <= 0"
    
    query += " ORDER BY close_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    rows = conn.execute(query, params).fetchall()
    total_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'CLOSED'").fetchone()[0]
    conn.close()
    
    return {"trades": [dict(r) for r in rows], "total_count": total_count}