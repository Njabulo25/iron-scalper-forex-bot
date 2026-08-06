from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO
import threading
import time
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

state = {
    "running": False, "balance": 10000, "starting_balance": 10000,
    "daily_pnl": 0, "open_positions": [], "trade_history": [],
    "daily_trades": 0, "total_trades": 0, "wins": 0, "losses": 0,
    "win_rate": 0, "status": "Stopped", "bid": 0, "ask": 0,
    "upcoming": [], "page": "dashboard"
}

trader_thread = None
trader_instance = None

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
        
        /* SIDEBAR */
        .sidebar {
            width: var(--sidebar-w); background: #0d0d0d; border-right: 1px solid var(--border);
            padding: 20px 0; position: fixed; top: 0; left: 0; bottom: 0;
            z-index: 100; display: flex; flex-direction: column;
            transition: transform 0.3s;
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
        
        /* MAIN */
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
        
        /* CARDS */
        .cards {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px; margin-bottom: 20px;
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
        
        /* BUTTONS */
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
        
        /* TABLES */
        .section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 16px; }
        .section h3 { padding: 14px 16px; border-bottom: 1px solid var(--border); font-size: 14px; color: var(--gold); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #1a1a1a; font-size: 12px; }
        th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 10px; }
        .tag-win { background: #003d1a; color: var(--green); padding: 3px 10px; border-radius: 10px; font-size: 10px; }
        .tag-loss { background: #3d0000; color: var(--red); padding: 3px 10px; border-radius: 10px; font-size: 10px; }
        .tag-buy { color: var(--green); font-weight: 600; }
        .tag-sell { color: var(--red); font-weight: 600; }
        
        /* PAGES */
        .page { display: none; }
        .page.active { display: block; }
        
        /* MOBILE */
        .menu-toggle { display: none; background: none; border: none; color: var(--gold); font-size: 24px; cursor: pointer; }
        
        @media (max-width: 768px) {
            .sidebar { transform: translateX(-100%); }
            .sidebar.open { transform: translateX(0); }
            .main { margin-left: 0; max-width: 100vw; }
            .menu-toggle { display: block; }
            .topbar h1 { font-size: 18px; }
            .cards { grid-template-columns: repeat(2, 1fr); }
            .card .value { font-size: 20px; }
        }
    </style>
</head>
<body>

<!-- SIDEBAR -->
<div class="sidebar" id="sidebar">
    <div class="logo">
        <h2>IRON SCALPER</h2>
        <p>Gold Never Rusts</p>
    </div>
    <nav>
        <a href="#" class="active" onclick="showPage('dashboard', this)">Dashboard</a>
        <a href="#" onclick="showPage('signals', this)">Signals</a>
        <a href="#" onclick="showPage('history', this)">History</a>
        <a href="#" onclick="showPage('settings', this)">Settings</a>
        <a href="#" onclick="showPage('share', this)">Share</a>
    </nav>
    <div class="warning">
        <strong>RISK WARNING:</strong> Forex and CFD trading involves substantial risk of loss. Past performance does not guarantee future results. Only trade with money you can afford to lose. This bot is for educational purposes.
    </div>
</div>

<!-- MAIN -->
<div class="main">
    <div class="topbar">
        <div style="display:flex;align-items:center;gap:12px;">
            <button class="menu-toggle" onclick="document.getElementById('sidebar').classList.toggle('open')">&#9776;</button>
            <h1>IRON <span>SCALPER</span></h1>
        </div>
        <span id="badge" class="badge badge-stopped">Stopped</span>
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
            <div class="card"><div class="label">Total Return</div><div class="value" id="ret">0%</div></div>
            <div class="card"><div class="label">Win Rate</div><div class="value gold-text" id="wr">0%</div></div>
            <div class="card"><div class="label">Total Trades</div><div class="value" id="tt">0</div></div>
            <div class="card"><div class="label">Today</div><div class="value" id="dt">0</div></div>
            <div class="card"><div class="label">XAUUSD</div><div class="value gold-text" id="px" style="font-size:20px;">0</div></div>
            <div class="card"><div class="label">Spread</div><div class="value" id="sp">0</div></div>
        </div>
        
        <div class="section">
            <h3>Open Positions</h3>
            <table><thead><tr><th>Type</th><th>Entry</th><th>SL</th><th>TP</th><th>Strategy</th></tr></thead>
            <tbody id="pos"><tr><td colspan="5" style="text-align:center;color:var(--muted);">No open positions</td></tr></tbody></table>
        </div>
    </div>
    
    <!-- SIGNALS PAGE -->
    <div class="page" id="page-signals">
        <div class="section">
            <h3>Upcoming Trade Signals</h3>
            <table><thead><tr><th>Strategy</th><th>Direction</th><th>Entry Price</th><th>SL</th><th>TP</th></tr></thead>
            <tbody id="upcoming"><tr><td colspan="5" style="text-align:center;color:var(--muted);">Scanning market...</td></tr></tbody></table>
        </div>
    </div>
    
    <!-- HISTORY PAGE -->
    <div class="page" id="page-history">
        <div class="section">
            <h3>Trade History</h3>
            <table><thead><tr><th>Time</th><th>Type</th><th>Entry</th><th>Exit</th><th>Pips</th><th>Result</th></tr></thead>
            <tbody id="hist"><tr><td colspan="6" style="text-align:center;color:var(--muted);">No trades yet</td></tr></tbody></table>
        </div>
    </div>
    
    <!-- SETTINGS PAGE -->
    <div class="page" id="page-settings">
        <div class="section">
            <h3>Bot Settings</h3>
            <table>
                <tr><td>Risk Per Trade</td><td>1%</td></tr>
                <tr><td>Max Daily Loss</td><td>3%</td></tr>
                <tr><td>Max Daily Trades</td><td>10</td></tr>
                <tr><td>Breakout Strategy</td><td>Enabled</td></tr>
                <tr><td>Scalper Strategy</td><td>Enabled</td></tr>
                <tr><td>Trading Hours</td><td>08:00 - 16:00 GMT</td></tr>
                <tr><td>Symbol</td><td>XAUUSD</td></tr>
            </table>
        </div>
    </div>
    
    <!-- SHARE PAGE -->
    <div class="page" id="page-share">
        <div class="section">
            <h3>How to Share This Bot</h3>
            <table>
                <tr><td style="color:var(--gold);font-weight:600;">1</td><td>Copy the entire <code>Iron Scalper</code> folder to your friend's computer</td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">2</td><td>They must have Python 3.10+ and MetaTrader 5 installed</td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">3</td><td>Run: <code>pip install -r requirements.txt</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">4</td><td>Run: <code>python src/engine/web_dashboard.py</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">5</td><td>Open browser: <code>http://localhost:5000</code></td></tr>
                <tr><td style="color:var(--gold);font-weight:600;">6</td><td>Log into MT5 demo account and click Start Bot</td></tr>
            </table>
        </div>
    </div>
</div>

<script>
const s = io();

function showPage(name, el) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
    if (el) el.classList.add('active');
    // Close sidebar on mobile
    if (window.innerWidth < 768) document.getElementById('sidebar').classList.remove('open');
}

s.on('state_update', d => {
    document.getElementById('badge').textContent = d.status || 'Stopped';
    document.getElementById('badge').className = 'badge ' + (d.running ? 'badge-running' : 'badge-stopped');
    document.getElementById('go').disabled = d.running;
    document.getElementById('st').disabled = !d.running;
    
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
    
    // Upcoming signals
    var up = d.upcoming || [];
    var uh = '';
    if (up.length === 0) uh = '<tr><td colspan="5" style="text-align:center;color:var(--muted);">No signals forming</td></tr>';
    else up.forEach(function(u) {
        uh += '<tr><td>' + (u.strategy||'') + '</td><td class="tag-' + (u.type === 'BUY' ? 'buy' : 'sell') + '">' + u.type + '</td><td>' + (u.entry||0).toFixed(2) + '</td><td>' + (u.sl||0).toFixed(2) + '</td><td>' + (u.tp||0).toFixed(2) + '</td></tr>';
    });
    document.getElementById('upcoming').innerHTML = uh;
    
    // Trade history
    var hist = d.trade_history || [];
    var hh = '';
    if (hist.length === 0) hh = '<tr><td colspan="6" style="text-align:center;color:var(--muted);">No trades yet</td></tr>';
    else hist.slice(-30).reverse().forEach(function(h) {
        hh += '<tr><td>' + (h.time||'') + '</td><td class="tag-' + (h.type === 'BUY' ? 'buy' : 'sell') + '">' + (h.type||'') + '</td><td>' + (h.entry||0).toFixed(2) + '</td><td>' + (h.exit||0).toFixed(2) + '</td><td>' + (h.pips||0).toFixed(0) + '</td><td><span class="tag-' + (h.win ? 'win' : 'loss') + '">' + (h.win ? 'WIN' : 'LOSS') + '</span></td></tr>';
    });
    document.getElementById('hist').innerHTML = hh;
});

setInterval(function() {
    fetch('/state').then(r => r.json()).then(d => {
        document.getElementById('badge').textContent = d.status || 'Stopped';
        document.getElementById('badge').className = 'badge ' + (d.running ? 'badge-running' : 'badge-stopped');
        document.getElementById('go').disabled = d.running;
        document.getElementById('st').disabled = !d.running;
    });
}, 10000);
</script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start():
    global trader_thread, trader_instance, state
    if state["running"]:
        return jsonify({"status": "already_running"})
    state["running"] = True
    state["status"] = "Running"
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.engine.live_trader import LiveTrader
    trader_instance = LiveTrader(web_state=state, socketio=socketio)
    trader_thread = threading.Thread(target=trader_instance.run_web, daemon=True)
    trader_thread.start()
    return jsonify({"status": "started"})

@app.route('/stop', methods=['POST'])
def stop():
    global state, trader_instance
    state["running"] = False
    state["status"] = "Stopped"
    if trader_instance:
        trader_instance._stop = True
    socketio.emit('state_update', state)
    return jsonify({"status": "stopped"})

@app.route('/state')
def get():
    return jsonify(state)

def run():
    print("\n" + "=" * 50)
    print("IRON SCALPER")
    print("=" * 50)
    print("\nhttp://localhost:5000\n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    run()