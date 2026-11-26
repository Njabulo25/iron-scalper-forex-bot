"""Flask terminal for the Iron Scalper engine.

The page itself lives in ``templates/`` and ``static/``; this module only serves
it and exposes the data the terminal renders. Every number returned here comes
from the engine's published state or from SQLite - nothing is sampled or
invented, and an endpoint with no data says so instead of returning a shape that
looks like a result.

Routes fall into three groups:

* control  - ``/start``, ``/stop`` and authenticated state polling,
* trading  - positions, account, performance, trades and their detail,
* research - model versions, walk-forward backtests, events and health.
"""

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hmac
import json
import logging
import os
import secrets
import threading
import time

import numpy as np

from config.settings import CONFIG
from src.engine import database
from src.engine.auth import (
    admin_required,
    authenticate,
    login_allowed,
    login_required,
    record_login_attempt,
)
from src.engine.analytics import (
    get_daily_summary,
    get_equity_curve,
    get_monthly_series,
    get_performance_summary,
    get_session_analysis,
    get_strategy_breakdown,
    get_trade_detail,
    get_trade_log,
    get_weekly_comparison,
)
from src.engine.graph_analyzer import UPLOAD_FOLDER, analyze_chart, save_upload

_APP_STARTED_MONOTONIC = time.monotonic()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config.update(
    SECRET_KEY=os.environ.get("IRON_SCALPER_SECRET_KEY") or secrets.token_urlsafe(48),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("IRON_SCALPER_COOKIE_SECURE", "").lower()
    in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# Compatibility export for the older dashboard module. Live state uses the
# authenticated HTTP polling loop rather than an unauthenticated broadcast bus.
socketio = None


@app.context_processor
def inject_csrf_token():
    """Expose a session-bound token to forms and the dashboard fetch helper."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": lambda: session["_csrf_token"]}


@app.before_request
def protect_state_changes():
    """Require a same-session token for every unsafe HTTP method."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"error": "invalid or missing CSRF token"}), 400
    return None


@app.after_request
def harden_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'",
    )
    return response


# The trading loop and the Flask handlers both touch ``state``, so every read
# and write takes this lock. ``_control_lock`` serialises start/stop, which
# stops a double click from launching two loops against one account.
state_lock = threading.Lock()
_control_lock = threading.Lock()

state = {
    "running": False, "balance": None, "starting_balance": None,
    "daily_pnl": 0, "open_positions": [], "trade_history": [],
    "daily_trades": 0, "total_trades": 0, "wins": 0, "losses": 0,
    "win_rate": 0, "status": "Stopped", "bid": 0, "ask": 0,
    "upcoming": [], "page": "dashboard",
    # Republished by the loop on every pass so the header shows real health.
    "mt5_connected": False, "mt5_reconnects": 0, "mt5_error": "",
    "feed_age_seconds": 0, "feed_stale": False,
    "mt5_ready": False, "mt5_issues": [],
    "mt5_terminal": {}, "mt5_account": {}, "mt5_symbol": {},
    "pending_orders": [], "manual_positions": 0,
    "loop_heartbeat_epoch": 0, "loop_duration_ms": None,
    # Published by the engine at startup: the configuration it trades with.
    "config": {},
    # Published by the engine when an ML gate is configured or active.
    "ml": {},
}

trader_instance = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _snapshot_state():
    """A consistent copy of the shared state for JSON replies and sockets."""
    with state_lock:
        return dict(state)


def _config_payload():
    """The configuration the engine is actually using, or the file's values.

    When the bot is running this is the snapshot the engine published at
    startup, i.e. exactly the object its strategies and risk manager read - not
    a second copy parsed out of the file. When it is stopped there is no engine
    to report, so the configured values are shown and labelled as such.
    """
    with state_lock:
        published = state.get("config") or {}
        running = bool(state.get("running"))

    if published:
        return {"source": "engine", "running": running, "config": published}
    return {"source": "settings.py", "running": False, "config": CONFIG.snapshot()}


def _json_field(row, name, default=None):
    """Decode one of the JSON TEXT columns the database stores."""
    value = row.get(name)
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ── pages ──────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Display the login form and establish an authenticated session."""
    if request.method == 'GET' and session.get("logged_in") is True:
        return redirect('/dashboard')

    error = None
    setup_hint = None
    if database.user_count() == 0:
        setup_hint = (
            "No operator is configured. Run "
            "python -m src.engine.auth add-user --username <name> --role admin."
        )
    if request.method == 'POST':
        if not login_allowed(request.remote_addr):
            return render_template(
                'login.html', error="Too many attempts. Wait 15 minutes and try again.",
                setup_hint=setup_hint,
            ), 429
        username = request.form.get('username', '').strip()
        passphrase = request.form.get('passphrase', '')
        user = authenticate(username, passphrase)
        record_login_attempt(request.remote_addr, user is not None)
        if user:
            session.clear()
            session['logged_in'] = True
            session['username'] = user["username"]
            session['role'] = user["role"]
            session.permanent = True
            session['_csrf_token'] = secrets.token_urlsafe(32)
            return redirect('/dashboard')
        error = 'Invalid username or passphrase. Passphrases must be at least 12 characters.'
    return render_template('login.html', error=error, setup_hint=setup_hint)


@app.route('/logout', methods=['POST'])
def logout():
    """Clear the session and return the user to the login page."""
    session.clear()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({"status": "logged_out"})

    return redirect('/')


@app.route('/')
@app.route('/landing')
def landing():
    return render_template(
        'landing.html',
        logged_in=session.get("logged_in") is True,
        username=session.get("username", ""),
        scalper_timeframe=CONFIG.scalper.timeframe,
        breakout_timeframe=CONFIG.breakout.timeframe,
        max_daily_trades=CONFIG.risk.max_daily_trades,
    )


@app.route('/dashboard')
@login_required
def index():
    role = session.get('role', 'viewer')
    backtest_strategies = []
    if CONFIG.scalper.enabled and CONFIG.scalper.timeframe == CONFIG.ml.timeframe:
        backtest_strategies.append('EMA scalper')
    if CONFIG.breakout.enabled and CONFIG.breakout.timeframe == CONFIG.ml.timeframe:
        backtest_strategies.append('Asian breakout')
    return render_template(
        'index.html',
        username=session.get('username', 'User'),
        role=role,
        role_label='Administrator' if role == 'admin' else 'Read-only viewer',
        backtest_timeframe=CONFIG.ml.timeframe,
        backtest_strategy_label=' + '.join(backtest_strategies) or 'no matching strategy',
    )


# ── control ────────────────────────────────────────────────────────

@app.route('/start', methods=['POST'])
@admin_required
def start():
    global trader_instance

    with _control_lock:
        if trader_instance is not None and state.get("running"):
            return jsonify({"status": "already_running"})

        try:
            import sys
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            from src.engine.live_trader import LiveTrader

            trader = LiveTrader(web_state=state, socketio=None, state_lock=state_lock)
        except Exception as error:
            # Connecting can fail (terminal closed, wrong account). Report it and
            # stay Stopped instead of leaving the panel stuck on "Running".
            with state_lock:
                state["running"] = False
                state["status"] = "Stopped"
                state["mt5_connected"] = False
                state["mt5_error"] = str(error)
            return jsonify({"status": "error", "error": str(error)}), 503

        trader_instance = trader
        with state_lock:
            state["running"] = True
            state["status"] = "Running"
            state["mt5_connected"] = True
            state["mt5_error"] = ""
            state["loop_heartbeat_epoch"] = 0
            state["loop_duration_ms"] = None

        threading.Thread(target=trader.run_web, daemon=True, name="iron-trader").start()

    return jsonify({"status": "started"})


@app.route('/stop', methods=['POST'])
@admin_required
def stop():
    global trader_instance

    with _control_lock:
        trader = trader_instance
        trader_instance = None
        if trader is not None:
            trader.stop()
        with state_lock:
            state["running"] = False
            state["status"] = "Stopped"

    return jsonify({"status": "stopped"})


@app.route('/state')
@login_required
def get_state():
    return jsonify(_snapshot_state())


# ── account and live trading ──────────────────────────────────────

@app.route('/api/account')
@login_required
def api_account():
    """Account and equity as the terminal reports it, plus realised P&L.

    ``live`` is false when the engine is stopped: the terminal has no session to
    read, so the numbers below would be the last published values. The UI shows
    that distinction rather than passing stale balances off as current.
    """
    with state_lock:
        running = bool(state.get("running"))
        connected = bool(state.get("mt5_connected"))
        account = dict(state.get("mt5_account") or {})
        positions = list(state.get("open_positions") or [])
        daily_pnl = state.get("daily_pnl")
        starting_balance = state.get("starting_balance")

    try:
        daily = get_daily_summary()
    except Exception:
        daily = {"total_trades": 0, "total_profit": None}
    if not running:
        daily_pnl = daily.get("total_profit") if daily.get("total_trades") else None

    balance = account.get("balance")
    equity = account.get("equity")
    floating = None
    if balance is not None and equity is not None:
        floating = round(float(equity) - float(balance), 2)

    try:
        realised = database.get_trade_stats()
    except Exception as error:  # a locked database must not break the panel
        realised = {"error": str(error), "total_usd": 0, "total_trades": 0}

    return jsonify({
        "live": running and connected,
        "engine_running": running,
        "account": account,
        "currency": account.get("currency") or "USD",
        "balance": balance,
        "equity": equity,
        "margin_free": account.get("margin_free"),
        "margin_used": account.get("margin"),
        "margin_level": account.get("margin_level"),
        "floating_pl": floating,
        "open_positions": len(positions),
        "open_position_pl": round(sum(float(p.get("profit") or 0) for p in positions), 2),
        "daily_pnl": daily_pnl,
        "daily_trades": daily.get("total_trades", 0),
        "realised": realised,
        "starting_balance": starting_balance,
    })


@app.route('/api/positions')
@login_required
def api_positions():
    with state_lock:
        running = bool(state.get("running"))
        payload = {
            "running": running,
            "connected": bool(state.get("mt5_connected")),
            "positions": list(state.get("open_positions") or []),
            "pending": list(state.get("pending_orders") or []),
            "symbol": (state.get("mt5_symbol") or {}).get("name") or CONFIG.mt5.symbol,
            "bid": state.get("bid") or 0,
            "ask": state.get("ask") or 0,
            "manual_positions": state.get("manual_positions") or 0,
            "trading_allowed": state.get("trading_allowed"),
            "risk_reason": state.get("risk_reason") or "",
        }
    return jsonify(payload)


@app.route('/api/health')
@login_required
def api_health():
    """Database and engine health, for the monitoring page."""
    try:
        health = database.database_health()
    except Exception as error:
        health = {"error": str(error)}

    db_file = Path(database.DATABASE_PATH)
    try:
        stat = db_file.stat()
        file_info = {
            "path": str(db_file),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except OSError:
        file_info = {"path": str(db_file), "size_bytes": None, "modified_at": None}

    with state_lock:
        heartbeat_epoch = state.get("loop_heartbeat_epoch") or 0
        try:
            heartbeat_age = max(0.0, time.time() - float(heartbeat_epoch)) if heartbeat_epoch else None
        except (TypeError, ValueError):
            heartbeat_age = None
        configured_interval = (
            (state.get("config") or {}).get("execution", {}).get("loop_seconds")
            or CONFIG.execution.loop_seconds
        )
        heartbeat_stale_after = max(20.0, float(configured_interval) * 5)
        heartbeat_status = (
            "stopped" if not state.get("running") else
            "waiting" if heartbeat_age is None else
            "stale" if heartbeat_age > heartbeat_stale_after else "healthy"
        )
        engine = {
            "running": bool(state.get("running")),
            "status": state.get("status"),
            "loop_heartbeat_status": heartbeat_status,
            "loop_heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "loop_stale_after_seconds": round(heartbeat_stale_after, 1),
            "loop_duration_ms": state.get("loop_duration_ms"),
            "mt5_connected": bool(state.get("mt5_connected")),
            "mt5_ready": bool(state.get("mt5_ready")),
            "feed_age_seconds": state.get("feed_age_seconds"),
            "feed_stale": bool(state.get("feed_stale")),
            "reconnects": state.get("mt5_reconnects"),
            "error": state.get("mt5_error") or "",
            "issues": list(state.get("mt5_issues") or []),
            "trading_allowed": state.get("trading_allowed"),
            "risk_reason": state.get("risk_reason") or "",
            "ml": dict(state.get("ml") or {}),
        }

    try:
        latest_trade = database.get_recent_trades(1)
    except Exception:
        latest_trade = []

    return jsonify({
        "database": health,
        "file": file_info,
        "engine": engine,
        "counts": (health or {}).get("counts", {}) if isinstance(health, dict) else {},
        "last_trade": latest_trade[0] if latest_trade else None,
        "schema_version": health.get("schema_version") if isinstance(health, dict) else None,
        "uptime_seconds": round(time.monotonic() - _APP_STARTED_MONOTONIC),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route('/api/logs')
@login_required
def api_logs():
    kind = request.args.get('kind', 'system')
    limit = request.args.get('limit', 100, type=int)
    limit = max(1, min(limit, 500))

    if kind == 'risk':
        events = database.get_risk_events(
            limit=limit,
            event_type=request.args.get('type') or None,
            severity=request.args.get('level') or None,
            day=request.args.get('day') or None,
        )
    else:
        events = database.get_system_events(
            limit=limit,
            level=request.args.get('level') or None,
            category=request.args.get('category') or None,
        )

    for event in events:
        event["detail"] = _json_field(event, "detail", {})

    return jsonify({"kind": kind, "events": events, "count": len(events)})


# ── analytics ─────────────────────────────────────────────────────

def _range_args():
    return (
        request.args.get('start') or None,
        request.args.get('end') or None,
    )


@app.route('/api/performance')
@login_required
def api_performance():
    start, end = _range_args()
    balance = request.args.get('balance', type=float)
    if balance is None:
        # A configured placeholder balance is not an account measurement.
        # The live engine publishes the real day-start balance after MT5 has
        # connected; without that evidence, leave drawdown percentage unknown.
        with state_lock:
            balance = state.get("starting_balance")

    return jsonify({
        "summary": get_performance_summary(start, end, balance),
        "equity": get_equity_curve(start, end),
        "monthly": get_monthly_series(start, end),
        "strategies": get_strategy_breakdown(start, end),
        "sessions": get_session_analysis(start, end),
        "weekly": get_weekly_comparison(),
        "today": get_daily_summary(),
        "range": {"start": start, "end": end},
    })


@app.route('/api/analytics/daily')
@login_required
def api_daily():
    return jsonify(get_daily_summary())


@app.route('/api/analytics/weekly')
@login_required
def api_weekly():
    return jsonify(get_weekly_comparison())


@app.route('/api/analytics/strategies')
@login_required
def api_strategies():
    start, end = _range_args()
    return jsonify(get_strategy_breakdown(start, end))


@app.route('/api/analytics/sessions')
@login_required
def api_sessions():
    start, end = _range_args()
    return jsonify(get_session_analysis(start, end))


@app.route('/api/analytics/equity')
@login_required
def api_equity():
    start, end = _range_args()
    return jsonify(get_equity_curve(start, end))


@app.route('/api/analytics/monthly')
@login_required
def api_monthly():
    start, end = _range_args()
    return jsonify(get_monthly_series(start, end))


@app.route('/api/trades')
@login_required
def api_trades():
    start, end = _range_args()
    return jsonify(get_trade_log(
        limit=request.args.get('limit', 50, type=int),
        offset=request.args.get('offset', 0, type=int),
        strategy=request.args.get('strategy', 'all'),
        result=request.args.get('result', 'all'),
        start=start,
        end=end,
    ))


@app.route('/api/trades/<int:trade_id>')
@login_required
def api_trade_detail(trade_id):
    detail = get_trade_detail(trade_id)
    if detail is None:
        return jsonify({"error": "trade not found"}), 404
    return jsonify(detail)


@app.route('/api/config')
@login_required
def api_config():
    """Return the configuration the trading engine is actually using."""
    return jsonify(_config_payload())


# ── machine learning ──────────────────────────────────────────────

@app.route('/api/ml')
@login_required
def api_ml():
    """Model version, walk-forward record and live prediction performance."""
    from src.ml import monitor as ml_monitor

    with state_lock:
        status = dict(state.get("ml") or {})

    registry_path = os.path.join(os.path.dirname(CONFIG.ml.model_path), "registry.json")
    latest = ml_monitor.latest_model(registry_path)
    try:
        live = ml_monitor.summary(CONFIG.ml.monitor_days)
    except Exception as error:
        live = {"error": str(error), "predictions": 0}

    return jsonify({
        "status": status,
        "registry": latest,
        "monitor": live,
        "configured": {
            "enabled": CONFIG.ml.enabled,
            "timeframe": CONFIG.ml.timeframe,
            "confidence_min": CONFIG.ml.confidence_min,
            "model_path": CONFIG.ml.model_path,
            "horizon_bars": CONFIG.ml.horizon_bars,
            "folds": CONFIG.ml.folds,
            "monitor_days": CONFIG.ml.monitor_days,
        },
    })


@app.route('/api/models')
@login_required
def api_models():
    """Model versions and training runs as recorded in the database."""
    versions = []
    for row in database.get_model_versions(20):
        row["feature_names"] = _json_field(row, "feature_names", [])
        row["samples"] = _json_field(row, "samples", {})
        row["out_of_sample"] = _json_field(row, "out_of_sample", {})
        row["data"] = {
            "fingerprint": row.get("data_fingerprint"),
            "bars": row.get("data_bars"),
            "first": row.get("data_first"),
            "last": row.get("data_last"),
        }
        versions.append(row)

    # A model can be trained successfully even if its database write failed.
    # The registry is the artifact's durable index, so include its real entries
    # when the database does not already contain that version.
    registry_path = Path(os.path.dirname(CONFIG.ml.model_path)) / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry_versions = {
            entry.get("version"): entry for entry in registry.get("versions", [])
        }
        for row in versions:
            entry = registry_versions.get(row.get("version"), {})
            row["timeframe"] = entry.get("timeframe")
        known_versions = {row.get("version") for row in versions}
        for entry in registry.get("versions", []):
            if entry.get("version") in known_versions:
                continue
            versions.append({
                "version": entry.get("version"),
                "timeframe": entry.get("timeframe"),
                "created_at": entry.get("trained_at"),
                "data": entry.get("data") or {},
                "samples": entry.get("samples") or {},
                "out_of_sample": entry.get("out_of_sample") or {},
                "is_active": 0,
                "registry_only": True,
            })
        versions.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        versions = versions[:20]
    except (OSError, ValueError, TypeError):
        pass

    runs = []
    for row in database.get_model_training_runs(20):
        row["metrics"] = _json_field(row, "metrics", {})
        runs.append(row)

    try:
        predictions = database.get_ml_predictions(500)
    except Exception:
        predictions = []

    resolved = [p for p in predictions if p.get("outcome_result")]
    taken = [p for p in predictions if p.get("decision") == "taken"]
    monitor_data = {}
    try:
        from src.ml import monitor as ml_monitor
        monitor_data = ml_monitor.summary(CONFIG.ml.monitor_days)
    except Exception as error:
        monitor_data = {"error": str(error)}

    return jsonify({
        "versions": versions,
        "runs": runs,
        "active": next((v["version"] for v in versions if v.get("is_active")), None),
        "predictions": {
            "total": len(predictions),
            "taken": len(taken),
            "resolved": len(resolved),
        },
        "monitor": monitor_data,
        "configured": {
            "enabled": CONFIG.ml.enabled,
            "confidence_min": CONFIG.ml.confidence_min,
            "timeframe": CONFIG.ml.timeframe,
            "data_path": CONFIG.ml.data_path,
            "folds": CONFIG.ml.folds,
            "horizon_bars": CONFIG.ml.horizon_bars,
            "embargo_bars": CONFIG.ml.embargo_bars,
        },
    })


# ── backtests ─────────────────────────────────────────────────────

_backtest_lock = threading.Lock()
_backtest_job = {
    "id": None, "status": "idle", "label": "", "started_at": None,
    "finished_at": None, "error": None, "run_id": None, "result": None,
}

# Metrics that describe one side of the comparison, shared by the runner.
_BACKTEST_METRICS = ("trades", "wins", "win_rate", "avg_r", "total_r",
                     "profit_factor", "max_drawdown_r")


def _stored_bars(symbol, timeframe):
    """Bars for a walk-forward run: the database copy first, then the CSV.

    Returns ``(bars, source_label)``. The label is stored with the result so the
    run records which history it was measured on.
    """
    from src.ml import history as ml_history
    from src.ml.features import Bars

    if database.count_market_bars(symbol, timeframe):
        rows = database.get_market_bars(symbol, timeframe, limit=200000)
        rows = sorted(rows, key=lambda row: row["bar_time"])
        if rows:
            bars = Bars(
                [row["bar_time"] for row in rows],
                [row["open"] for row in rows],
                [row["high"] for row in rows],
                [row["low"] for row in rows],
                [row["close"] for row in rows],
                [row.get("volume") or 0 for row in rows],
                [row.get("spread") or 0 for row in rows],
                symbol=symbol,
            )
            return bars, f"market_data ({len(rows)} bars)"

    candidates = [Path(CONFIG.ml.data_path), PROJECT_ROOT / "data" / "raw" / f"{symbol}_{timeframe}.csv"]
    for path in candidates:
        if path.exists():
            bars = ml_history.load_bars(path)
            return bars, f"{path} ({len(bars)} bars)"

    raise FileNotFoundError(
        "no stored bars: run `python train_ml.py --collect --store-db` or place "
        f"data/raw/{symbol}_{timeframe}.csv"
    )


def _slice_bars(bars, start=None, end=None):
    from src.ml.features import Bars

    mask = np.ones(len(bars), dtype=bool)
    if start:
        mask &= bars.time >= int(datetime.strptime(start, "%Y-%m-%d")
                                 .replace(tzinfo=timezone.utc).timestamp())
    if end:
        mask &= bars.time <= int(datetime.strptime(end, "%Y-%m-%d")
                                 .replace(tzinfo=timezone.utc).timestamp()) + 86_399
    if mask.all():
        return bars
    return Bars(
        *(getattr(bars, name)[mask] for name in
          ("time", "open", "high", "low", "close", "tick_volume", "spread")),
        symbol=bars.symbol,
    )


def _run_backtest(job_id, label, start, end):
    """Walk-forward on stored history, recorded in ``backtest_results``.

    This is a re-simulation of the strategies on bars, not live trading. The
    result row keeps the data window, the parameters and both sides of the
    comparison, so a later run can be compared with it.
    """
    from src.ml import pipeline

    try:
        bars, source = _stored_bars(CONFIG.mt5.symbol, CONFIG.ml.timeframe)
        bars = _slice_bars(bars, start, end)
        if len(bars) < 300:
            raise ValueError(f"only {len(bars)} bars in this window; a walk-forward needs at least 300")

        dataset = pipeline.build_dataset(bars, CONFIG)
        report = pipeline.walk_forward(dataset, CONFIG)
        aggregate = report.get("aggregate") or {}
        gated = aggregate.get("gated") or {}
        if not aggregate or not gated.get("trades"):
            raise RuntimeError(report.get("error") or "the walk-forward produced no out-of-sample trades")

        baseline = aggregate.get("baseline") or {}
        active = next((v for v in database.get_model_versions(5) if v.get("is_active")), None)
        window_start = datetime.fromtimestamp(int(bars.time[0]), timezone.utc).strftime("%Y-%m-%d")
        window_end = datetime.fromtimestamp(int(bars.time[-1]), timezone.utc).strftime("%Y-%m-%d")

        run_id = database.record_backtest_result(
            label or "walk-forward",
            "+".join(
                strategy for strategy in ("SCALPER", "BREAKOUT")
                if int(dataset["counts"].get(strategy.lower(), 0)) > 0
            ),
            CONFIG.mt5.symbol,
            CONFIG.ml.timeframe,
            data_start=window_start,
            data_end=window_end,
            parameters={
                "source": source,
                "bars": int(len(bars)),
                "folds": CONFIG.ml.folds,
                "horizon_bars": CONFIG.ml.horizon_bars,
                "embargo_bars": CONFIG.ml.embargo_bars,
                "threshold": CONFIG.ml.confidence_min,
                "timeframe": CONFIG.ml.timeframe,
                "strategies": {
                    "SCALPER": int(dataset["counts"].get("scalper", 0)),
                    "BREAKOUT": int(dataset["counts"].get("breakout", 0)),
                },
                "requested_start": start,
                "requested_end": end,
            },
            trades=gated.get("trades"),
            wins=gated.get("wins"),
            win_rate=gated.get("win_rate"),
            expectancy_r=gated.get("avg_r"),
            total_r=gated.get("total_r"),
            max_drawdown_r=gated.get("max_drawdown_r"),
            model_version_id=active["id"] if active else None,
            notes={
                "baseline": {key: baseline.get(key) for key in _BACKTEST_METRICS},
                "gated": {key: gated.get(key) for key in _BACKTEST_METRICS},
                "skipped": aggregate.get("skipped"),
                "take_rate": aggregate.get("take_rate"),
                "uplift": aggregate.get("uplift"),
                "classification": aggregate.get("classification"),
                "folds": report.get("folds"),
                "signals": int(len(dataset["labels"])),
            },
        )
        database.log_system_event(
            f"Backtest completed: {label or 'walk-forward'}",
            level="info", category="backtest",
            detail={"run_id": run_id, "trades": gated.get("trades"),
                    "total_r": gated.get("total_r"), "source": source},
        )

        with _backtest_lock:
            _backtest_job.update({
                "status": "done", "run_id": run_id,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": {
                    "run_id": run_id,
                    "start": window_start,
                    "end": window_end,
                    "bars": int(len(bars)),
                    "source": source,
                    "baseline": {key: baseline.get(key) for key in _BACKTEST_METRICS},
                    "gated": {key: gated.get(key) for key in _BACKTEST_METRICS},
                    "take_rate": aggregate.get("take_rate"),
                    "uplift": aggregate.get("uplift"),
                },
            })
    except Exception as error:
        with _backtest_lock:
            _backtest_job.update({
                "status": "failed", "error": str(error),
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        try:
            database.log_system_event(
                f"Backtest failed: {label or 'walk-forward'}",
                level="warning", category="backtest", detail={"error": str(error)},
            )
        except Exception:
            pass


@app.route('/api/backtests')
@login_required
def api_backtests():
    runs = []
    for row in database.get_backtest_results(50):
        row["parameters"] = _json_field(row, "parameters", {})
        row["notes"] = _json_field(row, "notes", {})
        runs.append(row)

    with _backtest_lock:
        job = dict(_backtest_job)

    return jsonify({"runs": runs, "job": job, "count": len(runs)})


@app.route('/api/backtests/run', methods=['POST', 'GET'])
@login_required
def api_backtest_run():
    if request.method == 'GET':
        with _backtest_lock:
            return jsonify(dict(_backtest_job))

    if session.get("role") != "admin":
        return jsonify({"error": "administrator access required"}), 403

    payload = request.get_json(silent=True) or {}
    start = (payload.get("start") or "").strip() or None
    end = (payload.get("end") or "").strip() or None
    label = (payload.get("label") or "").strip()[:80]

    for value in (start, end):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "dates must be YYYY-MM-DD"}), 400

    with _backtest_lock:
        if _backtest_job["status"] == "running":
            return jsonify({"error": "a backtest is already running", "job": dict(_backtest_job)}), 409
        job_id = f"bt-{int(time.time() * 1000)}"
        _backtest_job.update({
            "id": job_id, "status": "running", "label": label or "walk-forward",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None, "error": None, "run_id": None, "result": None,
        })

    threading.Thread(
        target=_run_backtest, args=(job_id, label, start, end),
        daemon=True, name="backtest",
    ).start()
    return jsonify({"status": "started", "job_id": job_id}), 202


# ── chart upload ──────────────────────────────────────────────────

@app.route('/upload_graph', methods=['POST'])
@admin_required
def upload_graph():
    """Save an uploaded chart image and return its basic analysis."""
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({"error": "No file uploaded"})

    file = request.files['file']
    file.filename = secure_filename(file.filename)
    if not file.filename:
        return jsonify({"error": "No file uploaded"})

    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        image_path = save_upload(file)
        return jsonify(analyze_chart(image_path))
    except ValueError as error:
        return jsonify({"error": str(error), "price_trend": "NOT ANALYZED"}), 400
    except Exception as error:
        logging.getLogger("iron.dashboard").warning("Chart upload failed: %s", error)
        return jsonify({"error": "The chart image could not be processed."}), 400


def run(host="127.0.0.1", port=5000):
    """Initialise persistent storage and serve the local dashboard."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        if (
            os.environ.get("IRON_SCALPER_ALLOW_REMOTE", "") != "1"
            or not os.environ.get("IRON_SCALPER_SECRET_KEY")
            or os.environ.get("IRON_SCALPER_COOKIE_SECURE", "").lower() not in {"1", "true", "yes"}
        ):
            raise ValueError(
                "Remote binding requires IRON_SCALPER_ALLOW_REMOTE=1, a stable secret key, "
                "and secure cookies behind a TLS reverse proxy."
            )

    database.init_db()
    Path("logs").mkdir(exist_ok=True)
    dashboard_logger = logging.getLogger("werkzeug")
    if not any(getattr(handler, "baseFilename", "").endswith("dashboard.log")
               for handler in dashboard_logger.handlers):
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            "logs/dashboard.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
        ))
        dashboard_logger.addHandler(file_handler)
    print("\n" + "=" * 50)
    print("IRON SCALPER")
    print("=" * 50)
    print(f"\nhttp://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    run()
