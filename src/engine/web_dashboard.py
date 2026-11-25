from flask import Flask, jsonify, redirect, render_template_string, request, session
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename
import threading
import time
import os
from datetime import datetime

from config.settings import CONFIG
from src.engine.auth import check_auth, login_required
from src.engine.analytics import get_daily_summary, get_weekly_comparison, get_strategy_breakdown, get_session_analysis, get_equity_curve, get_trade_log
from src.engine.graph_analyzer import UPLOAD_FOLDER, analyze_chart, save_upload

app = Flask(__name__)
app.config["SECRET_KEY"] = "iron-scalper-dashboard-session-key"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
socketio = SocketIO(app, cors_allowed_origins="*")

# The trading loop and the Flask handlers both touch ``state``, so every read
# and write takes this lock. ``_control_lock`` serialises start/stop, which
# stops a double click from launching two loops against one account.
state_lock = threading.Lock()
_control_lock = threading.Lock()

state = {
    "running": False, "balance": 10000, "starting_balance": 10000,
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
    # Published by the engine at startup: the configuration it trades with.
    "config": {},
}

trader_instance = None


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

LOGIN_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Iron Scalper | Login</title>
    <style>
        :root { --gold: #d4a017; --background: #0a0a0a; --card: #111111; --text: #ffffff; --muted: #aaaaaa; }
        * { box-sizing: border-box; }
        body {
            align-items: center; background: var(--background); color: var(--text); display: flex;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; justify-content: center;
            margin: 0; min-height: 100vh; padding: 20px;
        }
        .login-card { background: var(--card); border: 1px solid var(--gold); border-radius: 12px; padding: 32px; width: min(100%, 380px); }
        h1 { color: var(--gold); font-size: 26px; margin: 0 0 8px; }
        p { color: var(--muted); margin: 0 0 24px; }
        label { display: block; font-size: 14px; margin: 16px 0 7px; }
        input {
            background: #0a0a0a; border: 1px solid var(--gold); border-radius: 6px; color: #ffffff;
            font-size: 16px; padding: 12px; width: 100%;
        }
        button { background: var(--gold); border: 0; border-radius: 6px; color: #000000; cursor: pointer; font-size: 16px; font-weight: 700; margin-top: 24px; padding: 12px; width: 100%; }
        .error { background: #3d0000; border: 1px solid #ff1744; border-radius: 6px; color: #ffb3b3; margin-bottom: 16px; padding: 10px; }
    </style>
</head>
<body>
    <main class="login-card">
        <h1>Iron Scalper</h1>
        <p>Sign in to access the trading dashboard.</p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post" action="/login">
            <label for="username">Username</label>
            <input id="username" name="username" type="text" autocomplete="username" required>
            <label for="pin">PIN</label>
            <input id="pin" name="pin" type="password" inputmode="numeric" autocomplete="current-password" required>
            <button type="submit">Log in</button>
        </form>
    </main>
</body>
</html>
'''

HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Iron Scalper | Gold Trading Bot</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        :root {
            --bg: #0a0a0a;
            --card: #111111;
            --border: #222222;
            --gold: #d4a017;
            --green: #00c853;
            --red: #ff1744;
            --text: #cccccc;
            --muted: #666666;
            --sidebar-w: 220px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg); color: var(--text); min-height: 100vh;
            display: flex;
        }
        
        .sidebar {
            width: var(--sidebar-w); background: #0d0d0d; border-right: 1px solid var(--border);
            padding: 20px 0; position: fixed; top: 0; left: 0; height: 100vh;
            bottom: 0; z-index: 100; display: flex; flex-direction: column;
            overflow-y: auto; -webkit-overflow-scrolling: touch; transition: transform 0.3s;
        }
        .sidebar .logo {
            padding: 0 20px 20px; border-bottom: 1px solid var(--border); margin-bottom: 20px;
        }
        .sidebar .logo h2 { color: var(--gold); font-size: 20px; }
        .sidebar .logo p { color: var(--muted); font-size: 11px; margin-top: 4px; }
        .sidebar nav a {
            display: block; padding: 12px 20px; color: var(--text); text-decoration: none;
            font-size: 14px; border-left: 3px solid transparent; transition: all 0.2s;
        }
        .sidebar nav a:hover, .sidebar nav a.active {
            background: #1a1a1a; border-left-color: var(--gold); color: var(--gold);
        }
        .sidebar .warning {
            margin-top: auto; padding: 20px; font-size: 10px; color: var(--muted);
            border-top: 1px solid var(--border); line-height: 1.5;
        }
        .sidebar .warning strong { color: var(--red); }
        .sidebar-user {
            padding: 12px 20px 8px; color: var(--gold); font-size: 12px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 8px;
        }
        .sidebar nav a.logout-link {
            color: #ff8a80;
        }
        .sidebar nav a.logout-link:hover, .sidebar nav a.logout-link.active {
            background: rgba(255, 23, 68, 0.08);
            border-left-color: var(--red);
            color: #ff8a80;
        }

        .overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.6);
            z-index: 99;
        }
        .overlay.active { display: block; }
        
        .main {
            margin-left: var(--sidebar-w); flex: 1; padding: 20px;
            max-width: calc(100vw - var(--sidebar-w));
        }
        .topbar {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 20px; flex-wrap: wrap; gap: 10px;
        }
        .topbar h1 { font-size: 24px; }
        .topbar h1 span { color: var(--gold); }
        .badge {
            padding: 8px 20px; border-radius: 20px; font-size: 13px; font-weight: 600;
            display: inline-block;
        }
        .badge-running { background: #003d1a; color: var(--green); border: 1px solid var(--green); }
        .badge-stopped { background: #3d0000; color: var(--red); border: 1px solid var(--red); }
        .chip {
            display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px;
            border: 1px solid var(--border); border-radius: 20px;
            font-size: 12px; color: var(--muted);
        }
        .chip::before {
            content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--muted);
        }
        .chip-live { color: var(--green); border-color: var(--green); }
        .chip-live::before { background: var(--green); }
        .chip-lost { color: var(--red); border-color: var(--red); }
        .chip-lost::before { background: var(--red); }
        
        .cards {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px; margin-bottom: 20px;
        }
        @media (min-width: 1025px) {
            .cards { grid-template-columns: repeat(4, 1fr); }
        }
        .card {
            background: var(--card); border: 1px solid var(--border); border-radius: 10px;
            padding: 16px;
        }
        .card .label { font-size: 10px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; letter-spacing: 1px; }
        .card .value { font-size: 26px; font-weight: 700; }
        .card .sub { font-size: 12px; margin-top: 4px; }
        .gold-text { color: var(--gold); }
        .green-text { color: var(--green); }
        .red-text { color: var(--red); }
        
        .btns { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .btn {
            padding: 12px 24px; border: none; border-radius: 8px; font-size: 14px;
            font-weight: 600; cursor: pointer; transition: all 0.2s;
        }
        .btn-start { background: var(--green); color: #000; }
        .btn-start:hover { opacity: 0.85; }
        .btn-stop { background: var(--red); color: #fff; }
        .btn-stop:hover { opacity: 0.85; }
        .btn:disabled { opacity: 0.3; cursor: not-allowed; }
        
        .section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 16px; }
        .section h3 { padding: 14px 16px; border-bottom: 1px solid var(--border); font-size: 14px; color: var(--gold); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #1a1a1a; font-size: 12px; }
        th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 10px; }
        .tag-win { background: #003d1a; color: var(--green); padding: 3px 10px; border-radius: 10px; font-size: 10px; }
        .tag-loss { background: #3d0000; color: var(--red); padding: 3px 10px; border-radius: 10px; font-size: 10px; }
        .tag-buy { color: var(--green); font-weight: 600; }
        .tag-sell { color: var(--red); font-weight: 600; }
        
        .page { display: none; }
        .page.active { display: block; }

        .graph-upload { padding: 18px 16px; }
        .graph-upload input[type="file"] {
            background: #0a0a0a; border: 1px solid var(--gold); border-radius: 6px;
            color: var(--text); padding: 10px; width: 100%;
        }
        .graph-upload button { background: var(--gold); border: 0; border-radius: 6px; color: #000; cursor: pointer; font-weight: 700; margin-top: 12px; padding: 10px 18px; }
        .graph-preview { display: none; margin-top: 16px; max-height: 360px; max-width: 100%; object-fit: contain; }
        .analysis-card { display: none; margin: 16px; padding: 16px; }
        .analysis-card.error { border-color: var(--red); }
        .analysis-card h4 { color: var(--gold); margin-bottom: 12px; }
        .analysis-card p { margin: 8px 0; }
        .analysis-card strong { color: var(--muted); display: inline-block; min-width: 100px; }
        
        .menu-toggle { display: none; background: none; border: none; color: var(--gold); font-size: 24px; cursor: pointer; }

        .risk { padding: 14px 16px; }
        .risk-head {
            display: flex; justify-content: space-between; align-items: baseline;
            font-size: 11px; color: var(--muted); letter-spacing: 1px;
            margin-bottom: 8px; text-transform: uppercase;
        }
        .risk-head strong { color: var(--text); font-weight: 600; letter-spacing: 0; }
        .risk-track { background: #1a1a1a; border-radius: 2px; height: 4px; overflow: hidden; }
        .risk-fill { background: var(--green); border-radius: 2px; height: 100%; width: 0; transition: width 0.3s ease; }
        .risk-fill.warn { background: var(--gold); }
        .risk-fill.hot { background: var(--red); }
        .card .value.small { font-size: 20px; }
        
        @media (max-width: 768px) {
            .sidebar {
                transform: translateX(-100%);
                z-index: 101;
            }
            .sidebar.open { transform: translateX(0); }
            .main { margin-left: 0; max-width: 100vw; }
            .menu-toggle { display: block; }
            .overlay { display: none; }
            .overlay.active { display: block; }
            .topbar h1 { font-size: 18px; }
            .cards { grid-template-columns: repeat(2, 1fr); }
            .card .value { font-size: 20px; }
        }

        @media (min-width: 769px) {
            .overlay { display: none !important; }
            .sidebar { transform: none !important; }
        }
    </style>
</head>
<body>

<div class="overlay" id="overlay" onclick="closeSidebar()"></div>

<div class="sidebar" id="sidebar">
    <div class="logo">
        <h2>IRON SCALPER</h2>
        <p>Gold Never Rusts</p>
    </div>
<div class="sidebar-user">Logged in as {{ session.get('username', 'User') }}</div>
<nav>
    <a href="#" class="active" onclick="showPage('dashboard', this)">Dashboard</a>
    <a href="#" onclick="showPage('signals', this)">Signals</a>
    <a href="#" onclick="showPage('history', this)">History</a>
    <a href="#" onclick="showPage('graph', this)">Graph Analysis</a>
    <a href="#" onclick="showPage('settings', this)">Settings</a>
    <a href="#" onclick="showPage('share', this)">Share</a>
    <a href="/logout" class="logout-link" onclick="return confirm('Are you sure you want to logout?')">Logout</a>
</nav>
<div class="warning">
        <strong>RISK WARNING:</strong> Forex and CFD trading involves substantial risk of loss. Past performance does not guarantee future results. Only trade with money you can afford to lose. This bot is for educational purposes.
    </div>
</div>

<div class="main">
    <div class="topbar">
        <div style="display:flex;align-items:center;gap:12px;">
            <button class="menu-toggle" onclick="toggleSidebar()">&#9776;</button>
            <h1>IRON <span>SCALPER</span></h1>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
            <span id="conn" class="chip">MT5 idle</span>
            <span id="badge" class="badge badge-stopped">Stopped</span>
        </div>
    </div>
    
    <div class="btns">
        <button class="btn btn-start" id="go" onclick="fetch('/start',{method:'POST'})">Start Bot</button>
        <button class="btn btn-stop" id="st" onclick="fetch('/stop',{method:'POST'})">Stop Bot</button>
    </div>
    
    <!-- DASHBOARD PAGE -->
    <div class="page active" id="page-dashboard">
        <div class="cards">
            <div class="card"><div class="label">Balance</div><div class="value gold-text" id="bal">$0</div></div>
            <div class="card"><div class="label">Daily P&L</div><div class="value" id="dpnl">$0</div></div>
            <div class="card"><div class="label">Day Return</div><div class="value" id="ret">0%</div></div>
            <div class="card"><div class="label">Win Rate</div><div class="value gold-text" id="wr">0%</div></div>
            <div class="card"><div class="label">Total Trades</div><div class="value" id="tt">0</div></div>
            <div class="card"><div class="label">Today</div><div class="value" id="dt">0</div></div>
            <div class="card"><div class="label">XAUUSD</div><div class="value gold-text" id="px" style="font-size:20px;">0</div></div>
            <div class="card"><div class="label">Spread</div><div class="value" id="sp">0</div></div>
        </div>
        <div class="cards">
            <div class="card"><div class="label">Closed P&amp;L</div><div class="value" id="cpnl">$0</div></div>
            <div class="card"><div class="label">Total Pips</div><div class="value" id="tpips">0</div></div>
            <div class="card"><div class="label">Win / Loss</div><div class="value small" id="wl">0 / 0</div></div>
            <div class="card"><div class="label">Loss Streak</div><div class="value small" id="streak">0</div></div>
        </div>

        <div class="section risk">
            <div class="risk-head"><span>Daily risk used</span><strong id="risktxt">--</strong></div>
            <div class="risk-track"><div class="risk-fill" id="riskfill"></div></div>
        </div>

        <div class="section">
            <h3>MetaTrader 5 Session</h3>
            <table>
                <thead><tr><th>Field</th><th>Value</th><th>Field</th><th>Value</th></tr></thead>
                <tbody id="mt5-panel"><tr><td colspan="4" style="text-align:center;color:var(--muted);">Engine stopped - start the bot to read the terminal</td></tr></tbody>
            </table>
            <div id="mt5-issues" style="padding:10px 16px;font-size:11px;color:var(--red);"></div>
        </div>
        
        <div class="section">
            <h3>Open Positions</h3>
            <table><thead><tr><th>Type</th><th>Entry</th><th>SL</th><th>TP</th><th>Strategy</th></tr></thead>
            <tbody id="pos"><tr><td colspan="5" style="text-align:center;color:var(--muted);">No open positions</td></tr></tbody></table>
        </div>
    </div>
    
    <!-- SIGNALS PAGE -->
    <div class="page" id="page-signals">
        <div class="section" style="border-color:#d2991d;">
            <h3 style="color:#d2991d;"> Scheduled Trade Alerts</h3>
            <div style="padding:8px 16px;font-size:11px;color:var(--muted);">Times are South African Time (SAST/GMT+2).</div>
            <table><thead><tr><th>Time (SA)</th><th>Strategy</th><th>Alert</th><th>Entry</th><th>SL</th><th>TP</th></tr></thead>
            <tbody id="upcoming"><tr><td colspan="6" style="text-align:center;color:var(--muted);">Scanning market...</td></tr></tbody></table>
        </div>
    </div>
    
    <!-- HISTORY PAGE -->
    <div class="page" id="page-history">
        <div class="section">
            <h3>Trade History</h3>
            <table><thead><tr><th>Time (SA)</th><th>Type</th><th>Entry</th><th>Exit</th><th>Pips</th><th>P&amp;L</th><th>Result</th></tr></thead>
            <tbody id="hist"><tr><td colspan="7" style="text-align:center;color:var(--muted);">No trades yet</td></tr></tbody></table>
        </div>
    </div>

    <!-- GRAPH ANALYSIS PAGE -->
    <div class="page" id="page-graph">
        <div class="section">
            <h3>Graph Analysis</h3>
            <div class="graph-upload">
                <form id="graph-upload-form" action="/upload_graph" method="POST" enctype="multipart/form-data">
                    <input id="graph-file" type="file" name="file" accept="image/*" required>
                    <button type="submit">Analyze Chart</button>
                </form>
                <img id="graph-preview" class="graph-preview" alt="Uploaded chart preview">
            </div>
            <div id="analysis-results" class="card analysis-card" aria-live="polite"></div>
        </div>
    </div>
    
    <!-- SETTINGS PAGE -->
    <div class="page" id="page-settings">
        <div class="section">
            <h3>Configuration In Use</h3>
            <div id="conf-source" style="padding:10px 16px;font-size:11px;color:var(--muted);">Loading configuration…</div>
            <table>
                <thead><tr><th>Setting</th><th>Value</th></tr></thead>
                <tbody id="conf-rows"><tr><td colspan="2" style="text-align:center;color:var(--muted);">Loading…</td></tr></tbody>
            </table>
        </div>
    </div>
    
    <!-- SHARE PAGE -->
    <div class="page" id="page-share">
        <div class="section">
            <h3>How to Share This Bot</h3>
            <table>
                <tr><td style="color:var(--gold);font-weight:600;">1</td><td>Copy the entire <code>Iron Scalper</code> folder to their computer</td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">2</td><td>Install Python 3.10+ and MetaTrader 5</td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">3</td><td>Run: <code>pip install -r requirements.txt</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">4</td><td>Run: <code>python src/engine/web_dashboard.py</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">5</td><td>Open <code>http://localhost:5000</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">6</td><td>Log into MT5 demo and click Start Bot</td></tr>
            </table>
        </div>
    </div>
</div>

<script>
const s = io();

function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('overlay').classList.remove('active');
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('overlay');
    const isOpen = sidebar.classList.contains('open');
    sidebar.classList.toggle('open', !isOpen);
    overlay.classList.toggle('active', !isOpen);
}

function showPage(name, el) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
    if (el) el.classList.add('active');
    closeSidebar();
}

const graphUploadForm = document.getElementById('graph-upload-form');
const graphFileInput = document.getElementById('graph-file');
const graphPreview = document.getElementById('graph-preview');
const analysisResults = document.getElementById('analysis-results');

graphFileInput.addEventListener('change', function() {
    const file = this.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(event) {
        graphPreview.src = event.target.result;
        graphPreview.style.display = 'block';
    };
    reader.readAsDataURL(file);
});

graphUploadForm.addEventListener('submit', async function(event) {
    event.preventDefault();
    analysisResults.style.display = 'block';
    analysisResults.classList.remove('error');
    analysisResults.textContent = 'Analyzing chart...';

    try {
        const response = await fetch('/upload_graph', {
            method: 'POST',
            body: new FormData(graphUploadForm)
        });
        const analysis = await response.json();

        if (analysis.error) {
            analysisResults.classList.add('error');
            analysisResults.textContent = analysis.error;
            return;
        }

        analysisResults.innerHTML =
            '<h4>Analysis Results</h4>' +
            '<p><strong>Trend:</strong> ' + analysis.trend + '</p>' +
            '<p><strong>Volatility:</strong> ' + analysis.volatility + '</p>' +
            '<p><strong>Brightness:</strong> ' + Number(analysis.brightness).toFixed(2) + '</p>' +
            '<p><strong>Image size:</strong> ' + analysis.image_size[0] + ' × ' + analysis.image_size[1] + '</p>';
    } catch (error) {
        analysisResults.classList.add('error');
        analysisResults.textContent = 'Unable to analyze the uploaded chart.';
    }
});

function pairsTable(rows, emptyText) {
    if (!rows.length) {
        return '<tr><td colspan="4" style="text-align:center;color:var(--muted);">' + emptyText + '</td></tr>';
    }
    var html = '';
    for (var i = 0; i < rows.length; i += 2) {
        var left = rows[i];
        var right = rows[i + 1] || ['', ''];
        html += '<tr><td style="color:var(--muted);white-space:nowrap;">' + left[0] +
                '</td><td>' + left[1] +
                '</td><td style="color:var(--muted);white-space:nowrap;">' + right[0] +
                '</td><td>' + right[1] + '</td></tr>';
    }
    return html;
}

function money(value) {
    if (value === undefined || value === null || value === '') return '';
    return '$' + Number(value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function renderMt5(d) {
    var panel = document.getElementById('mt5-panel');
    if (!panel) return;

    var t = d.mt5_terminal || {}, a = d.mt5_account || {}, sym = d.mt5_symbol || {};
    var rows = [];
    function add(label, value) {
        if (value === undefined || value === null || value === '') return;
        rows.push([label, String(value)]);
    }

    add('Terminal', t.name);
    add('Company', t.company);
    add('Build', t.build);
    add('Terminal path', t.path);
    add('Login', a.login);
    add('Server', a.server);
    add('Currency', a.currency);
    add('Leverage', a.leverage ? '1:' + a.leverage : '');
    add('Balance', money(a.balance));
    add('Equity', money(a.equity));
    add('Free margin', money(a.margin_free));
    add('Trade allowed', a.trade_allowed === undefined ? '' : (a.trade_allowed ? 'Yes' : 'No'));
    add('Algo trading', t.trade_allowed === undefined ? '' : (t.trade_allowed ? 'Enabled' : 'Disabled'));
    add('Symbol', sym.name);
    add('Trade mode', sym.trade_mode_text);
    add('Digits', sym.digits);
    add('Contract size', sym.contract_size);
    add('Volume min', sym.volume_min);
    add('Volume step', sym.volume_step);
    add('Volume max', sym.volume_max);
    add('Stops level', sym.stops_level === undefined || sym.stops_level === null ? '' : sym.stops_level + ' pts');
    add('Spread', d.ask && d.bid ? ((((d.ask - d.bid) * 100) || 0).toFixed(0) + ' pts') : '');
    // Live freshness only means something while a loop is publishing it.
    if (d.running) {
        add('Tick age', (d.feed_age_seconds || 0) + 's' + (d.feed_stale ? ' (stale)' : ''));
        add('Reconnects', d.mt5_reconnects || 0);
        add('Pending orders', (d.pending_orders || []).length);
        add('Manual positions', d.manual_positions || 0);
        add('Last error', d.mt5_error);
    }

    panel.innerHTML = pairsTable(rows, d.running
        ? 'Waiting for the trading engine'
        : 'Engine stopped - start the bot to read the terminal');

    var issues = document.getElementById('mt5-issues');
    if (issues) {
        var list = d.mt5_issues || [];
        issues.textContent = list.length ? 'Not ready to trade: ' + list.join('; ') : '';
    }
}

var CONFIG_GROUPS = [
    ['Risk', 'risk', [
        ['risk_per_trade', 'Risk per trade'], ['max_daily_loss_pct', 'Max daily loss'],
        ['max_daily_trades', 'Max daily trades'], ['max_open_positions', 'Max open positions'],
        ['max_consecutive_losses', 'Loss-streak stop'], ['same_direction_guard_usd', 'Duplicate guard (USD)'],
        ['starting_balance', 'Fallback balance']]],
    ['Execution', 'execution', [
        ['loop_seconds', 'Loop interval (s)'], ['max_signal_age_seconds', 'Max signal age (s)'],
        ['history_scan_seconds', 'Deal-history scan (s)'], ['position_sync_seconds', 'Position sync (s)'],
        ['max_spread_points', 'Max spread (points)']]],
    ['MetaTrader 5', 'mt5', [
        ['symbol', 'Symbol'], ['magic', 'Magic number'], ['deviation_points', 'Deviation (points)'],
        ['retry_attempts', 'Order attempts'], ['reconnect_seconds', 'Reconnect backoff (s)'],
        ['max_backoff_seconds', 'Max backoff (s)'], ['feed_stale_seconds', 'Feed stale after (s)'],
        ['terminal_path', 'Pinned terminal'], ['expected_login', 'Expected login'],
        ['expected_server', 'Expected server'], ['require_algo_trading', 'Require algo trading']]],
    ['Breakout', 'breakout', [
        ['enabled', 'Enabled'], ['symbol', 'Symbol'], ['asian_session', 'Asian range'],
        ['session', 'Entry session'], ['min_asian_bars', 'Min Asian bars'],
        ['atr_period', 'ATR period'], ['atr_multiplier', 'ATR multiplier'],
        ['min_range', 'Min Asian range'], ['sl_range_pct', 'Stop (% of range)'], ['tp_rr', 'Target (R multiple)']]],
    ['Scalper', 'scalper', [
        ['enabled', 'Enabled'], ['symbol', 'Symbol'], ['timeframe', 'Timeframe'],
        ['session', 'Session'], ['atr_period', 'ATR period'], ['rsi_period', 'RSI period'],
        ['ema_fast', 'Fast EMA'], ['ema_slow', 'Slow EMA'], ['pullback_atr', 'Pullback (ATR)'],
        ['sl_atr', 'Stop (ATR)'], ['tp_atr', 'Target (ATR)'],
        ['rsi_long_min', 'RSI long min'], ['rsi_long_max', 'RSI long max'],
        ['rsi_short_min', 'RSI short min'], ['rsi_short_max', 'RSI short max'],
        ['min_bar_spacing', 'Bars between entries']]],
    ['Machine learning', 'ml', [
        ['enabled', 'Gate enabled'], ['confidence_min', 'Min confidence'], ['model_path', 'Model path']]]
];

function configValue(key, value) {
    if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
    if (key === 'risk_per_trade' || key === 'max_daily_loss_pct' || key === 'sl_range_pct' || key === 'confidence_min') {
        return (Number(value) * 100).toFixed(1) + '%';
    }
    return String(value);
}

function renderConfig(cfg) {
    var body = document.getElementById('conf-rows');
    if (!body) return;

    var html = '';
    CONFIG_GROUPS.forEach(function(group) {
        var values = cfg[group[1]] || {};
        html += '<tr><td colspan="2" style="color:var(--gold);font-size:10px;letter-spacing:1px;' +
                'text-transform:uppercase;padding-top:14px;">' + group[0] + '</td></tr>';
        group[2].forEach(function(field) {
            var value = values[field[0]];
            if (value === undefined || value === null || value === '') return;
            html += '<tr><td style="color:var(--muted);">' + field[1] + '</td><td>' +
                    configValue(field[0], value) + '</td></tr>';
        });
    });

    body.innerHTML = html || '<tr><td colspan="2" style="text-align:center;color:var(--muted);">No configuration published.</td></tr>';
}

var configLoadedFor = null;

function loadConfig(running) {
    if (configLoadedFor === running) return;
    configLoadedFor = running;

    fetch('/api/config').then(function(response) { return response.json(); }).then(function(payload) {
        var note = document.getElementById('conf-source');
        if (note) {
            note.textContent = payload.source === 'engine'
                ? (payload.running
                    ? 'These are the exact values the running trading engine is using.'
                    : 'Values the last engine run was built with.')
                : 'Configured in config/settings.py. The engine is stopped, so nothing is trading these yet.';
        }
        renderConfig(payload.config || {});
    }).catch(function() {
        var note = document.getElementById('conf-source');
        if (note) note.textContent = 'Could not load the configuration.';
    });
}

function renderConn(d) {
    var el = document.getElementById('conn');
    if (!el) return;
    if (!d.running) {
        el.className = 'chip';
        el.textContent = 'MT5 idle';
        return;
    }
    var live = !!d.mt5_connected;
    var age = Math.round(d.feed_age_seconds || 0);
    el.className = 'chip ' + (live ? 'chip-live' : 'chip-lost');
    if (live) {
        el.textContent = age >= 5 ? 'MT5 live \u00b7 feed ' + age + 's' : 'MT5 live';
    } else {
        el.textContent = 'MT5 lost \u00b7 retry ' + (d.mt5_reconnects || 0);
        el.title = d.mt5_error || 'Reconnecting to MetaTrader 5';
    }
}

s.on('state_update', d => {
    document.getElementById('badge').textContent = d.status || 'Stopped';
    document.getElementById('badge').className = 'badge ' + (d.running ? 'badge-running' : 'badge-stopped');
    document.getElementById('go').disabled = d.running;
    document.getElementById('st').disabled = !d.running;
    renderConn(d);
    renderMt5(d);
    loadConfig(d.running);
    
    document.getElementById('bal').textContent = '$' + (d.balance || 0).toLocaleString('en-US', {minimumFractionDigits: 2});
    
    var pnl = d.daily_pnl || 0;
    var dp = document.getElementById('dpnl');
    dp.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toLocaleString('en-US', {minimumFractionDigits: 2});
    dp.className = 'value ' + (pnl >= 0 ? 'green-text' : 'red-text');
    
    var rt = ((d.balance - d.starting_balance) / d.starting_balance * 100) || 0;
    var re = document.getElementById('ret');
    re.textContent = (rt >= 0 ? '+' : '') + rt.toFixed(2) + '%';
    re.className = 'value ' + (rt >= 0 ? 'green-text' : 'red-text');
    
    document.getElementById('wr').textContent = (d.win_rate || 0).toFixed(0) + '%';
    document.getElementById('tt').textContent = d.total_trades || 0;
    document.getElementById('dt').textContent = d.daily_trades || 0;
    document.getElementById('px').textContent = (d.bid || 0).toFixed(2);
    document.getElementById('sp').textContent = ((d.ask - d.bid) * 100 || 0).toFixed(0) + ' pips';
    
    // Open positions
    var pos = d.open_positions || [];
    var ph = '';
    if (pos.length === 0) ph = '<tr><td colspan="5" style="text-align:center;color:var(--muted);">No open positions</td></tr>';
    else pos.forEach(function(p) {
        ph += '<tr><td class="tag-' + (p.type === 'BUY' ? 'buy' : 'sell') + '">' + p.type + '</td><td>' + (p.entry||0).toFixed(2) + '</td><td>' + (p.sl||0).toFixed(2) + '</td><td>' + (p.tp||0).toFixed(2) + '</td><td>' + (p.strategy||'') + '</td></tr>';
    });
    document.getElementById('pos').innerHTML = ph;
    
    // Upcoming signals WITH TIME AND ALERT
    var up = d.upcoming || [];
    var uh = '';
    if (up.length === 0) uh = '<tr><td colspan="6" style="text-align:center;color:var(--muted);">No upcoming signals</td></tr>';
    else up.forEach(function(u) {
        var alertMsg = u.alert || ('Watch for ' + u.type + ' at ' + (u.entry||0).toFixed(2));
        uh += '<tr>';
        uh += '<td style="color:#d2991d;font-weight:600;white-space:nowrap;">' + (u.time||'--:--') + '</td>';
        uh += '<td>' + (u.strategy||'') + '</td>';
        uh += '<td style="color:#fff;font-weight:500;">' + alertMsg + '</td>';
        uh += '<td>' + (u.entry||0).toFixed(2) + '</td>';
        uh += '<td>' + (u.sl||0).toFixed(2) + '</td>';
        uh += '<td>' + (u.tp||0).toFixed(2) + '</td>';
        uh += '</tr>';
    });
    document.getElementById('upcoming').innerHTML = uh;
    
    // Trade history
    var hist = d.trade_history || [];
    var hh = '';
    if (hist.length === 0) hh = '<tr><td colspan="7" style="text-align:center;color:var(--muted);">No trades yet</td></tr>';
    else hist.slice(-30).reverse().forEach(function(h) {
        hh += '<tr><td>' + (h.time||'') + '</td><td class="tag-' + (h.type === 'BUY' ? 'buy' : 'sell') + '">' + (h.type||'') + '</td><td>' + (h.entry||0).toFixed(2) + '</td><td>' + (h.exit||0).toFixed(2) + '</td><td>' + (h.pips||0).toFixed(0) + '</td><td class="' + ((h.usd||0) >= 0 ? 'green-text' : 'red-text') + '">' + ((h.usd||0) >= 0 ? '+' : '-') + Math.abs(h.usd||0).toFixed(2) + '</td><td><span class="tag-' + (h.win ? 'win' : 'loss') + '">' + (h.win ? 'WIN' : 'LOSS') + '</span></td></tr>';
    });
    document.getElementById('hist').innerHTML = hh;

    // Realised performance and daily risk headroom
    var cpnl = d.closed_pnl || 0;
    var cpnlEl = document.getElementById('cpnl');
    cpnlEl.textContent = (cpnl < 0 ? '-$' : '+$') + Math.abs(cpnl).toLocaleString('en-US', {minimumFractionDigits: 2});
    cpnlEl.className = 'value ' + (cpnl >= 0 ? 'green-text' : 'red-text');

    document.getElementById('tpips').textContent = (d.total_pips || 0).toLocaleString('en-US') + ' p';
    document.getElementById('wl').textContent = (d.wins || 0) + ' / ' + (d.losses || 0);

    var streak = d.consecutive_losses || 0;
    var streakEl = document.getElementById('streak');
    streakEl.textContent = streak >= 3 ? streak + ' paused' : streak;
    streakEl.className = 'value small ' + (streak >= 3 ? 'red-text' : '');

    var budget = d.risk_budget || 0;
    var used = Math.max(-(d.daily_pnl || 0), 0);
    var pct = budget > 0 ? Math.min(used / budget * 100, 100) : 0;
    var fill = document.getElementById('riskfill');
    fill.style.width = pct.toFixed(0) + '%';
    fill.className = 'risk-fill' + (pct >= 99 ? ' hot' : pct >= 60 ? ' warn' : '');
    document.getElementById('risktxt').textContent = budget > 0
        ? used.toFixed(2) + ' / ' + budget.toFixed(2) + ' USD  ' + pct.toFixed(0) + '%'
        : '--';
});

setInterval(function() {
    fetch('/state').then(r => r.json()).then(d => {
        document.getElementById('badge').textContent = d.status || 'Stopped';
        document.getElementById('badge').className = 'badge ' + (d.running ? 'badge-running' : 'badge-stopped');
        document.getElementById('go').disabled = d.running;
        document.getElementById('st').disabled = !d.running;
        renderConn(d);
        renderMt5(d);
        loadConfig(d.running);
    });
}, 10000);
</script>
</body>
</html>
'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Display the login form and establish an authenticated session."""
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        pin = request.form.get('pin', '')
        if check_auth(username, pin):
            session['logged_in'] = True
            session['username'] = username
            return redirect('/')
        error = 'Invalid credentials'
    return render_template_string(LOGIN_HTML, error=error)


@app.route('/logout')
def logout():
    """Clear the session and return the user to the login page."""
    session.clear()
    session['logged_in'] = False

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({"status": "logged_out"})

    return redirect('/login')


@app.route('/')
@login_required
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
@login_required
def start():
    global trader_instance

    with _control_lock:
        if trader_instance is not None and state.get("running"):
            return jsonify({"status": "already_running"})

        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from src.engine.live_trader import LiveTrader

        try:
            trader = LiveTrader(web_state=state, socketio=socketio, state_lock=state_lock)
        except Exception as error:
            # Connecting can fail (terminal closed, wrong account). Report it and
            # stay Stopped instead of leaving the panel stuck on "Running".
            with state_lock:
                state["running"] = False
                state["status"] = "Stopped"
                state["mt5_connected"] = False
                state["mt5_error"] = str(error)
            socketio.emit('state_update', _snapshot_state())
            return jsonify({"status": "error", "error": str(error)}), 503

        trader_instance = trader
        with state_lock:
            state["running"] = True
            state["status"] = "Running"
            state["mt5_connected"] = True
            state["mt5_error"] = ""

        threading.Thread(target=trader.run_web, daemon=True, name="iron-trader").start()

    socketio.emit('state_update', _snapshot_state())
    return jsonify({"status": "started"})

@app.route('/stop', methods=['POST'])
@login_required
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

    socketio.emit('state_update', _snapshot_state())
    return jsonify({"status": "stopped"})

@app.route('/state')
@login_required
def get():
    return jsonify(_snapshot_state())


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
    return jsonify(get_strategy_breakdown())

@app.route('/api/analytics/sessions')
@login_required
def api_sessions():
    return jsonify(get_session_analysis())

@app.route('/api/analytics/equity')
@login_required
def api_equity():
    return jsonify(get_equity_curve())

@app.route('/api/trades')
@login_required
def api_trades():
    strategy = request.args.get('strategy', 'all')
    result = request.args.get('result', 'all')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    return jsonify(get_trade_log(limit, offset, strategy, result))

@app.route('/api/config')
@login_required
def api_config():
    """Return the configuration the trading engine is actually using."""
    return jsonify(_config_payload())


@app.route('/upload_graph', methods=['POST'])
@login_required
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
    except Exception as error:
        return jsonify({"error": str(error), "trend": "UNABLE TO ANALYZE"})

def run():
    print("\n" + "=" * 50)
    print("IRON SCALPER")
    print("=" * 50)
    print("\nhttp://localhost:5000\n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    run()
