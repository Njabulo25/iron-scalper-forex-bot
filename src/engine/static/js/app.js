/* Iron Scalper terminal interactions.
   All trading values are read from the engine or SQLite; empty records remain empty. */
(function () {
    'use strict';

    var state = { live: {}, account: {}, positions: {}, performance: null, config: null, ml: null, models: null };
    var historyPage = 0;
    var historyPageSize = 50;
    var toastTimer = null;
    var backtestTimer = null;
    var lastPerformanceRange = { start: null, end: null, key: '90d' };
    var titles = {
        overview: ['Dashboard', 'Live account, execution and risk state'],
        performance: ['Performance', 'Realised results from closed trades'],
        positions: ['Open positions', 'Exposure and protective orders reported by MT5'],
        history: ['Trade history', 'Closed trades recorded in the database'],
        strategies: ['Strategies', 'The exact settings loaded by the trading engine'],
        risk: ['Risk and ATR', 'Sizing, loss limits and stop geometry'],
        ml: ['Machine learning', 'Model evidence and live prediction outcomes'],
        training: ['Training and models', 'Recorded model versions and walk-forward runs'],
        backtesting: ['Backtesting', 'Walk-forward evaluation on stored market bars'],
        connection: ['MT5 connection', 'Terminal identity, quote freshness and readiness'],
        logs: ['Logs and health', 'Database state and recorded engine events'],
        graph: ['Chart analysis', 'Image upload utility'],
        share: ['Quick start', 'Launch the dashboard and connect MetaTrader 5']
    };

    function $(selector, root) { return (root || document).querySelector(selector); }
    function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
    function esc(value) {
        return String(value === undefined || value === null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function isNum(value) { return value !== null && value !== undefined && value !== '' && isFinite(Number(value)); }
    function number(value, digits) {
        return isNum(value) ? Number(value).toLocaleString(undefined, {
            minimumFractionDigits: digits === undefined ? 0 : digits,
            maximumFractionDigits: digits === undefined ? 2 : digits
        }) : '—';
    }
    function money(value, currency) {
        if (!isNum(value)) return '—';
        try {
            return new Intl.NumberFormat(undefined, {
                style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2
            }).format(Number(value));
        } catch (_) { return (currency || '') + ' ' + number(value, 2); }
    }
    function pct(value, fraction) {
        if (!isNum(value)) return '—';
        return number(Number(value) * (fraction ? 100 : 1), 1) + '%';
    }
    function dateText(value, withTime) {
        if (!value) return '—';
        var parsed = new Date(String(value).replace(' ', 'T') + (String(value).endsWith('Z') ? '' : ''));
        if (isNaN(parsed.getTime())) return String(value);
        return parsed.toLocaleString([], withTime
            ? { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }
            : { year: 'numeric', month: 'short', day: '2-digit' });
    }
    function setText(id, value) {
        var node = typeof id === 'string' ? document.getElementById(id) : id;
        if (node) node.textContent = value === undefined || value === null || value === '' ? '—' : String(value);
    }
    function setHTML(id, html) {
        var node = typeof id === 'string' ? document.getElementById(id) : id;
        if (node) node.innerHTML = html;
    }
    function detailList(id, rows) {
        var node = typeof id === 'string' ? document.getElementById(id) : id;
        if (!node) return;
        node.innerHTML = rows.length ? rows.map(function (row) {
            return '<div><dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1]) + '</dd></div>';
        }).join('') : '<div><dt>Status</dt><dd>No recorded data</dd></div>';
    }
    function tableRows(id, rows, emptyText, colspan) {
        var body = $("#" + id + " tbody");
        if (!body) return;
        body.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="' + (colspan || 5) + '" class="empty">' + esc(emptyText) + '</td></tr>';
    }
    function toast(message, isError) {
        var node = $('#toast');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', !!isError);
        node.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { node.hidden = true; }, 3600);
    }
    function showError(error) {
        var message = error && error.message ? error.message : String(error || 'Request failed');
        toast(message, true);
    }
    async function api(url, options) {
        options = options || {};
        var headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, options.headers || {});
        if (typeof options.body === 'string' && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
        var response = await fetch(url, Object.assign({ credentials: 'same-origin', headers: headers }, options));
        if (response.status === 401) {
            window.location.assign('/login');
            throw new Error('Your session ended. Sign in again.');
        }
        var payload;
        try { payload = await response.json(); }
        catch (_) { throw new Error('The server returned an unreadable response.'); }
        if (!response.ok) throw new Error(payload.error || ('Request failed (' + response.status + ')'));
        return payload;
    }
    function getRange(key) {
        if (key === 'all') return { start: null, end: null, key: key };
        var days = { '7d': 7, '30d': 30, '90d': 90, '1y': 365 }[key] || 90;
        var end = new Date();
        var start = new Date(end.getTime());
        start.setDate(start.getDate() - days + 1);
        function iso(date) {
            return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
        }
        return { start: iso(start), end: iso(end), key: key };
    }
    function queryRange(range) {
        var params = new URLSearchParams();
        if (range.start) params.set('start', range.start);
        if (range.end) params.set('end', range.end);
        return params.toString();
    }
    function chartLine(id, labels, series, options) {
        var node = document.getElementById(id);
        if (!node || !window.ISChart) return;
        window.ISChart.line(node, Object.assign({ labels: labels || [], series: series || [], height: 250 }, options || {}));
    }
    function chartBars(id, labels, series, options) {
        var node = document.getElementById(id);
        if (!node || !window.ISChart) return;
        window.ISChart.bars(node, Object.assign({ labels: labels || [], series: series || [], height: 220 }, options || {}));
    }
    function signedClass(value) { return Number(value) > 0 ? 'green' : Number(value) < 0 ? 'red' : ''; }
    function price(value, digits) { return isNum(value) && Number(value) !== 0 ? number(value, digits === undefined ? 2 : digits) : '—'; }
    function tag(text, kind) { return '<span class="tag tag-' + esc(kind || 'off') + '">' + esc(text) + '</span>'; }
    function setBanner(id, message, visible, kind) {
        var node = document.getElementById(id);
        if (!node) return;
        node.textContent = message || '';
        node.hidden = !visible;
        if (kind) node.className = 'banner banner-' + kind;
    }

    function renderHeader(live) {
        state.live = live || state.live || {};
        var running = !!state.live.running;
        var connected = !!state.live.mt5_connected;
        var engine = $('#engine-badge');
        var chip = $('#mt5-chip');
        if (engine) {
            engine.textContent = state.live.status || (running ? 'Running' : 'Stopped');
            engine.className = 'badge ' + (running ? 'badge-running' : 'badge-stopped');
        }
        if (chip) chip.className = 'chip ' + (connected ? 'chip-live' : (running ? 'chip-lost' : ''));
        setText('mt5-chip-text', connected ? 'MT5 connected' : (running ? 'MT5 disconnected' : 'MT5 idle'));
        var start = $('#btn-start'), stop = $('#btn-stop');
        if (start) start.disabled = running;
        if (stop) stop.disabled = !running;
        var stopped = $('#ov-stopped-banner');
        if (stopped) {
            stopped.hidden = running;
            stopped.textContent = running
                ? ''
                : 'Engine stopped. Account and quote values appear after you start the engine and connect MT5.';
        }
        renderRiskUsage();
        renderConnection();
        renderMLStatus();
    }

    function renderAccount(account) {
        state.account = account || {};
        var a = state.account;
        var ccy = a.currency || (a.account && a.account.currency) || 'USD';
        setText('kpi-balance', money(a.balance, ccy));
        setText('kpi-equity', money(a.equity, ccy));
        setText('kpi-free-margin', money(a.margin_free, ccy));
        setText('kpi-used-margin', money(a.margin_used, ccy));
        setText('kpi-floating', money(a.floating_pl, ccy));
        setText('kpi-daily', a.engine_running || Number(a.daily_trades || 0) > 0 ? money(a.daily_pnl, ccy) : '—');
        setText('kpi-balance-note', a.live ? 'Live MT5 account' : 'No live account session');
        setText('kpi-equity-note', a.live ? 'Live MT5 account' : 'No live account session');
        setText('kpi-margin-level', isNum(a.margin_level) ? number(a.margin_level, 1) + '% margin level' : 'No live margin data');
        setText('kpi-used-note', isNum(a.open_positions) ? number(a.open_positions, 0) + ' open positions' : 'No live position data');
        var floatingNote = $('#kpi-floating-note');
        if (floatingNote) floatingNote.textContent = a.live ? 'Unrealised account P/L' : 'No live account session';
        var dailyNote = $('#kpi-daily-note');
        if (dailyNote) dailyNote.textContent = Number(a.daily_trades || 0) > 0
            ? number(a.daily_trades, 0) + ' closed trades today'
            : a.engine_running ? 'No realised closes today' : 'No closes recorded today';
        var mode = $('#account-mode');
        if (mode) {
            mode.textContent = a.live ? 'live' : (a.engine_running ? 'disconnected' : 'offline');
            mode.className = 'tag ' + (a.live ? 'tag-on' : 'tag-off');
        }
        var info = a.account || {};
        detailList('account-details', [
            ['Login', info.login], ['Server', info.server], ['Company', info.company],
            ['Currency', info.currency], ['Leverage', isNum(info.leverage) ? '1:' + info.leverage : null],
            ['Free margin', money(a.margin_free, ccy)], ['Used margin', money(a.margin_used, ccy)]
        ].filter(function (item) { return item[1] !== undefined && item[1] !== null && item[1] !== ''; }));
        if (!a.live && !info.login) detailList('account-details', [['Status', 'MT5 is not connected']]);
        renderPositions(state.positions);
    }

    function renderPositions(payload) {
        state.positions = payload || state.positions || {};
        var p = state.positions;
        var positions = p.positions || [];
        var pending = p.pending || [];
        var openTable = positions.map(function (row) {
            var pnl = Number(row.profit || 0);
            return '<tr><td>' + esc(row.type || '—') + '</td><td class="mono">' + esc(row.ticket || '—') + '</td>' +
                '<td class="right mono">' + number(row.volume, 2) + '</td><td class="right mono">' + price(row.entry) + '</td>' +
                '<td class="right mono">' + price(row.sl) + '</td><td class="right mono">' + price(row.tp) + '</td>' +
                '<td>' + esc(row.strategy || '—') + '</td><td class="right mono ' + signedClass(pnl) + '">' + money(pnl, (state.account || {}).currency) + '</td></tr>';
        });
        tableRows('positions-table', openTable, p.running ? 'No open positions reported by MT5' : 'Start the engine to read live positions', 8);
        var pendingTable = pending.map(function (row) {
            return '<tr><td>' + esc(row.type || '—') + '</td><td class="mono">' + esc(row.ticket || '—') + '</td>' +
                '<td class="right mono">' + number(row.volume, 2) + '</td><td class="right mono">' + price(row.price) + '</td>' +
                '<td class="right mono">' + price(row.sl) + '</td><td class="right mono">' + price(row.tp) + '</td><td>' + esc(row.state || 'Pending') + '</td></tr>';
        });
        tableRows('pending-table', pendingTable, p.running ? 'No pending orders reported by MT5' : 'Start the engine to read pending orders', 7);
        setText('positions-count', positions.length + (positions.length === 1 ? ' position' : ' positions'));
        setText('nav-positions', positions.length);
        var summaryRows = positions.slice(0, 5).map(function (row) {
            return '<tr><td>' + esc(row.type || '—') + '</td><td class="mono">' + price(row.entry) + '</td>' +
                '<td class="mono">' + price(row.sl) + '</td><td class="mono">' + price(row.tp) + '</td>' +
                '<td class="right mono ' + signedClass(row.profit) + '">' + money(row.profit, (state.account || {}).currency) + '</td></tr>';
        });
        tableRows('overview-positions', summaryRows, p.running ? 'No open positions' : 'Live positions are unavailable while stopped', 5);
        var bannerMessage = !p.running ? 'Live positions are unavailable while the engine is stopped.'
            : !p.connected ? 'The engine is running but MT5 is disconnected; positions may be stale.'
                : (p.manual_positions ? p.manual_positions + ' manual or other-bot position(s) are also open on this symbol.' : '');
        setBanner('positions-banner', bannerMessage, !!bannerMessage, p.connected ? 'info' : 'warn');
        var config = state.config && state.config.config || {};
        var mt5 = config.mt5 || {};
        var ccy = (state.account || {}).currency || 'USD';
        var marketMode = $('#market-mode');
        if (marketMode) {
            marketMode.textContent = p.connected ? 'live' : (p.running ? 'disconnected' : 'idle');
            marketMode.className = 'tag ' + (p.connected ? 'tag-on' : 'tag-off');
        }
        setText('live-bid', isNum(p.bid) && Number(p.bid) > 0 ? price(p.bid, (state.live.mt5_symbol || {}).digits) : '—');
        setText('live-ask', isNum(p.ask) && Number(p.ask) > 0 ? price(p.ask, (state.live.mt5_symbol || {}).digits) : '—');
        setText('live-spread', isNum(p.bid) && isNum(p.ask) && p.bid > 0 && p.ask > 0 ? number(p.ask - p.bid, 2) : '—');
        var selectedStrategies = [];
        if (config.scalper && config.scalper.enabled) selectedStrategies.push('Scalper');
        if (config.breakout && config.breakout.enabled) selectedStrategies.push('Breakout');
        detailList('market-details', [
            ['Symbol', p.symbol || mt5.symbol || '—'],
            ['Active strategies', selectedStrategies.length ? selectedStrategies.join(', ') : 'None enabled'],
            ['Open positions', positions.length],
            ['Pending orders', pending.length],
            ['Trading gate', p.trading_allowed === true ? 'Allowed' : (p.trading_allowed === false ? p.risk_reason || 'Blocked' : 'Not running')]
        ]);
        if (!ccy) return;
    }

    function renderRiskUsage() {
        var live = state.live || {};
        var pctUsed = Number(live.risk_used_pct || 0);
        var budget = live.risk_budget;
        var fill = $('#risk-meter-fill');
        var pageFill = $('#risk-page-meter');
        var width = isNum(budget) ? Math.max(0, Math.min(pctUsed, 100)) : 0;
        if (fill) fill.style.width = width + '%';
        if (pageFill) pageFill.style.width = width + '%';
        setText('risk-usage-text', isNum(budget) ? number(pctUsed, 1) + '% used' : '—');
        setText('risk-usage-note', isNum(budget)
            ? money(live.risk_used_pct ? budget * pctUsed / 100 : 0, (state.account || {}).currency) + ' of ' + money(budget, (state.account || {}).currency) + ' daily limit'
            : 'Start the engine to read today’s risk budget.');
        var reason = live.risk_reason || (live.trading_allowed === true ? 'Trading is within configured risk limits.' : 'Risk state unavailable while stopped.');
        setText('risk-page-note', isNum(budget) ? number(pctUsed, 1) + '% of the daily loss limit used. ' + reason : reason);
    }

    function renderConnection() {
        var live = state.live || {};
        var account = live.mt5_account || {};
        var terminal = live.mt5_terminal || {};
        var symbol = live.mt5_symbol || {};
        detailList('connection-account', [
            ['Engine', live.running ? (live.status || 'Running') : 'Stopped'],
            ['Terminal', terminal.name], ['Company', terminal.company], ['Terminal path', terminal.path],
            ['Login', account.login], ['Server', account.server], ['Account currency', account.currency],
            ['Algo trading', terminal.trade_allowed === true ? 'Allowed' : terminal.trade_allowed === false ? 'Disabled' : null]
        ].filter(function (item) { return item[1] !== undefined && item[1] !== null && item[1] !== ''; }));
        detailList('connection-feed', [
            ['Connected', live.mt5_connected ? 'Yes' : 'No'],
            ['Feed age', isNum(live.feed_age_seconds) ? number(live.feed_age_seconds, 1) + ' sec' : '—'],
            ['Feed status', live.feed_stale ? 'Stale' : live.mt5_connected ? 'Fresh' : 'Unavailable'],
            ['Reconnects', number(live.mt5_reconnects, 0)], ['Error', live.mt5_error || '—']
        ]);
        detailList('connection-symbol', [
            ['Symbol', symbol.name || (state.config && state.config.config && state.config.config.mt5.symbol)],
            ['Digits', symbol.digits], ['Point', symbol.point], ['Contract size', symbol.contract_size],
            ['Volume min / max', isNum(symbol.volume_min) || isNum(symbol.volume_max) ? number(symbol.volume_min, 2) + ' / ' + number(symbol.volume_max, 2) : '—'],
            ['Volume step', symbol.volume_step], ['Stops level', symbol.stops_level], ['Trade mode', symbol.trade_mode_text]
        ]);
        var issues = live.mt5_issues || [];
        setHTML('connection-issues', issues.length
            ? issues.map(function (issue) { return '<div class="issue">' + esc(issue) + '</div>'; }).join('')
            : '<div class="issue ok">' + esc(live.mt5_ready ? 'All MT5 readiness checks passed.' : 'Start the engine to run MT5 readiness checks.') + '</div>');
        setBanner('connection-banner', live.mt5_error || (live.running && !live.mt5_connected ? 'MT5 is disconnected. The engine is waiting for a terminal connection.' : ''), !!(live.mt5_error || (live.running && !live.mt5_connected)), 'warn');
    }

    function renderPerformance(payload, target) {
        state.performance = payload;
        var summary = payload.summary || {};
        var equity = payload.equity || [];
        var daily = equity;
        var monthly = payload.monthly || [];
        var currency = (state.account || {}).currency || 'USD';
        if (target === 'overview') {
            setText('overview-equity-note', equity.length ? 'Cumulative realised P/L · ' + equity.length + ' close day(s)' : 'No closed trades in this range');
            chartLine('overview-equity', equity.map(function (row) { return row.day; }), [
                { name: 'Cumulative realised P/L', values: equity.map(function (row) { return Number(row.cumulative || 0); }), color: '#57d5ca' }
            ], { format: function (value) { return money(value, currency); }, emptyText: 'No closed trades in this range' });
            chartBars('overview-daily', daily.map(function (row) { return row.day; }), [
                { name: 'Daily net', values: daily.map(function (row) { return Number(row.daily_profit || 0); }) }
            ], { format: function (value) { return money(value, currency); }, emptyText: 'No daily results in this range' });
            return;
        }
        setText('perf-winrate', summary.trades ? pct(summary.win_rate) : '—');
        setText('perf-winrate-note', summary.trades ? number(summary.wins, 0) + ' wins from ' + number(summary.trades, 0) + ' closed trades' : 'No closed trades in this range');
        setText('perf-pf', summary.profit_factor === null ? (summary.trades && summary.gross_profit > 0 ? '∞' : '—') : number(summary.profit_factor, 2));
        setText('perf-pf-note', summary.profit_factor === null ? (summary.trades ? 'No gross losses recorded' : 'No closed trades') : 'Gross profit ÷ gross loss');
        setText('perf-trades', number(summary.trades, 0));
        setText('perf-trades-note', 'Closed trades in selected range');
        setText('perf-wl', number(summary.wins, 0) + ' / ' + number(summary.losses, 0));
        setText('perf-wl-note', number(summary.breakeven, 0) + ' breakeven');
        setText('perf-net', summary.trades ? money(summary.net_profit, currency) : '—');
        setText('perf-net-note', summary.trades ? number(summary.total_pips, 1) + ' pips' : 'No closed trades in this range');
        setText('perf-dd', summary.trades && isNum(summary.max_drawdown) ? '−' + money(Math.abs(summary.max_drawdown), currency) : '—');
        setText('perf-dd-note', 'Peak to trough, realised equity');
        setText('perf-expectancy', isNum(summary.expectancy_r) ? number(summary.expectancy_r, 3) + ' R' : '—');
        setText('perf-expectancy-note', number(summary.r_samples, 0) + ' trades with recorded R');
        setText('perf-avg', summary.trades ? money(summary.avg_trade, currency) : '—');
        setText('perf-avg-note', 'Average net per closed trade');
        setText('perf-equity-note', equity.length ? 'Cumulative realised P/L · ' + equity.length + ' close day(s)' : 'No closed trades in this range');
        chartLine('perf-equity', equity.map(function (row) { return row.day; }), [
            { name: 'Cumulative realised P/L', values: equity.map(function (row) { return Number(row.cumulative || 0); }), color: '#57d5ca' }
        ], { format: function (value) { return money(value, currency); }, emptyText: 'No closed trades in this range' });
        chartBars('perf-daily', daily.map(function (row) { return row.day; }), [
            { name: 'Daily net', values: daily.map(function (row) { return Number(row.daily_profit || 0); }) }
        ], { format: function (value) { return money(value, currency); }, emptyText: 'No daily results in this range' });
        chartBars('perf-monthly', monthly.map(function (row) { return row.month; }), [
            { name: 'Monthly net', values: monthly.map(function (row) { return Number(row.net || 0); }) }
        ], { format: function (value) { return money(value, currency); }, emptyText: 'No monthly results in this range' });
        renderStrategyPerformance(payload.strategies || []);
        renderSessionPerformance(payload.sessions || []);
    }
    function renderStrategyPerformance(rows) {
        tableRows('perf-strategies', rows.map(function (row) {
            return '<tr><td>' + esc(row.strategy) + '</td><td class="right mono">' + number(row.total_trades, 0) + '</td>' +
                '<td class="right mono">' + pct(row.win_rate) + '</td><td class="right mono">' + number(row.avg_r, 3) + '</td>' +
                '<td class="right mono ' + signedClass(row.total_profit) + '">' + money(row.total_profit, (state.account || {}).currency) + '</td></tr>';
        }), 'No closed trades in this window', 5);
    }
    function renderSessionPerformance(rows) {
        tableRows('perf-sessions', rows.map(function (row) {
            return '<tr><td class="mono">' + String(row.hour).padStart(2, '0') + ':00</td><td class="right mono">' + number(row.trades, 0) + '</td>' +
                '<td class="right mono">' + pct(row.win_rate) + '</td><td class="right mono ' + signedClass(row.profit) + '">' + money(row.profit, (state.account || {}).currency) + '</td></tr>';
        }), 'No close-hour data in this window', 4);
    }
    async function loadPerformance(target, range) {
        range = range || getRange(target === 'overview' ? '90d' : '30d');
        lastPerformanceRange = range;
        var query = queryRange(range);
        if (state.account && isNum(state.account.starting_balance)) {
            query += (query ? '&' : '') + 'balance=' + encodeURIComponent(state.account.starting_balance);
        }
        var payload = await api('/api/performance' + (query ? '?' + query : ''));
        renderPerformance(payload, target);
    }

    function renderRiskEvents(rows) {
        tableRows('risk-events-table', (rows || []).map(function (event) {
            return '<tr><td class="mono">' + esc(dateText(event.event_time, true)) + '</td><td>' + esc(event.event_type || '—') + '</td>' +
                '<td>' + tag(event.severity || 'info', event.severity === 'error' ? 'off' : event.severity === 'warning' ? 'warn' : 'on') + '</td>' +
                '<td>' + esc(event.strategy || '—') + '</td><td>' + esc(event.message || '—') + '</td></tr>';
        }), 'No risk events recorded', 5);
    }
    function configRows(obj, mapping) {
        return mapping.filter(function (entry) { return obj && obj[entry[0]] !== undefined; }).map(function (entry) {
            var value = obj[entry[0]];
            if (entry[2]) value = entry[2](value);
            else if (typeof value === 'boolean') value = value ? 'Enabled' : 'Disabled';
            else if (typeof value === 'number') value = number(value, Number.isInteger(value) ? 0 : 3);
            return [entry[1], value];
        });
    }
    function renderConfig(payload) {
        state.config = payload || {};
        var config = state.config.config || {};
        var source = $('#config-source');
        if (source) source.textContent = state.config.running
            ? 'Showing the exact snapshot published by the running engine.'
            : 'Showing configured settings from config/settings.py. Start the engine to publish its active snapshot.';
        function strategyState(id, item) {
            var node = document.getElementById(id);
            if (!node) return;
            node.textContent = item && item.enabled ? 'Enabled' : 'Disabled';
            node.className = 'tag ' + (item && item.enabled ? 'tag-on' : 'tag-off');
        }
        strategyState('scalper-state', config.scalper);
        strategyState('breakout-state', config.breakout);
        detailList('scalper-details', configRows(config.scalper, [
            ['symbol', 'Symbol'], ['timeframe', 'Timeframe'], ['session', 'Session'], ['atr_period', 'ATR period'],
            ['rsi_period', 'RSI period'], ['ema_fast', 'Fast EMA'], ['ema_slow', 'Slow EMA'],
            ['pullback_atr', 'Entry distance (ATR)'], ['sl_atr', 'Stop loss (ATR)'], ['tp_atr', 'Take profit (ATR)'],
            ['rsi_long_min', 'Long RSI min'], ['rsi_long_max', 'Long RSI max'],
            ['rsi_short_min', 'Short RSI min'], ['rsi_short_max', 'Short RSI max'], ['min_bar_spacing', 'Bars between entries']
        ]));
        detailList('breakout-details', configRows(config.breakout, [
            ['symbol', 'Symbol'], ['asian_session', 'Asian range'], ['session', 'Entry session'], ['min_asian_bars', 'Minimum range bars'],
            ['atr_period', 'ATR period'], ['atr_multiplier', 'Entry distance (ATR)'], ['min_range', 'Minimum range'],
            ['sl_range_pct', 'Stop distance (% of range)', function (v) { return pct(v, true); }], ['tp_rr', 'Target (R multiple)']
        ]));
        detailList('execution-details', configRows(config.execution, [
            ['loop_seconds', 'Loop interval (sec)'], ['max_signal_age_seconds', 'Max signal age (sec)'],
            ['history_scan_seconds', 'History scan (sec)'], ['position_sync_seconds', 'Position sync (sec)'],
            ['max_spread_points', 'Maximum spread (points)']
        ]).concat(configRows(config.mt5, [
            ['symbol', 'MT5 symbol'], ['magic', 'Magic number'], ['deviation_points', 'Deviation (points)'],
            ['retry_attempts', 'Order attempts'], ['feed_stale_seconds', 'Stale feed after (sec)']
        ])));
        detailList('risk-details', configRows(config.risk, [
            ['risk_per_trade', 'Risk per trade', function (v) { return pct(v, true); }],
            ['max_daily_loss_pct', 'Maximum daily loss', function (v) { return pct(v, true); }],
            ['max_daily_trades', 'Maximum daily trades'], ['max_open_positions', 'Maximum open positions'],
            ['max_consecutive_losses', 'Consecutive loss stop'], ['same_direction_guard_usd', 'Duplicate entry guard (USD)']
        ]));
        detailList('atr-details', configRows(config.scalper, [
            ['atr_period', 'Scalper ATR period'], ['sl_atr', 'Scalper stop (ATR)'], ['tp_atr', 'Scalper target (ATR)'],
            ['pullback_atr', 'Scalper entry distance (ATR)']
        ]).concat(configRows(config.breakout, [
            ['atr_period', 'Breakout ATR period'], ['atr_multiplier', 'Breakout entry distance (ATR)'],
            ['sl_range_pct', 'Breakout stop (% of range)', function (v) { return pct(v, true); }],
            ['tp_rr', 'Breakout target (R multiple)'], ['min_range', 'Minimum Asian range']
        ])));
        var enabled = !!(config.ml && config.ml.enabled);
        detailList('ml-summary', configRows(config.ml, [
            ['enabled', 'Gate enabled'], ['confidence_min', 'Minimum confidence', function (v) { return pct(v, true); }],
            ['model_path', 'Model path'], ['data_path', 'Training data path'], ['horizon_bars', 'Outcome horizon (bars)'],
            ['embargo_bars', 'Embargo (bars)'], ['folds', 'Walk-forward folds'], ['monitor_days', 'Live monitor window (days)']
        ]));
        return enabled;
    }

    async function loadConfig() {
        var payload = await api('/api/config');
        renderConfig(payload);
        detailList('atr-details', (function () {
            var c = payload.config || {};
            return configRows(c.scalper, [
                ['atr_period', 'Scalper ATR period'], ['sl_atr', 'Scalper stop (ATR)'], ['tp_atr', 'Scalper target (ATR)'],
                ['pullback_atr', 'Scalper entry distance (ATR)']
            ]).concat(configRows(c.breakout, [
                ['atr_period', 'Breakout ATR period'], ['atr_multiplier', 'Breakout entry distance (ATR)'],
                ['sl_range_pct', 'Breakout stop (% of range)', function (v) { return pct(v, true); }],
                ['tp_rr', 'Breakout target (R multiple)'], ['min_range', 'Minimum Asian range']
            ]));
        })());
        renderPositions(state.positions);
    }

    function renderHistory(payload) {
        var rows = payload.trades || [];
        setText('hist-total', number(payload.total_count, 0) + ' trades');
        var pages = Math.max(1, Math.ceil((payload.total_count || 0) / historyPageSize));
        setText('hist-page', 'Page ' + (historyPage + 1) + ' of ' + pages);
        var prev = $('#hist-prev'), next = $('#hist-next');
        if (prev) prev.disabled = historyPage <= 0;
        if (next) next.disabled = historyPage + 1 >= pages;
        tableRows('history-table', rows.map(function (row) {
            var result = Number(row.profit_usd || 0) > 0 ? 'WIN' : Number(row.profit_usd || 0) < 0 ? 'LOSS' : 'FLAT';
            var resultKind = result === 'WIN' ? 'on' : result === 'LOSS' ? 'off' : 'warn';
            return '<tr class="clickable-row" data-trade-id="' + esc(row.id) + '">' +
                '<td class="mono">' + esc(dateText(row.close_time, true)) + '</td><td class="mono">' + esc(row.ticket) + '</td>' +
                '<td>' + esc(row.trade_type || '—') + '</td><td>' + esc(row.strategy || '—') + '</td>' +
                '<td class="right mono">' + number(row.lots, 2) + '</td><td class="right mono">' + price(row.entry_price) + '</td>' +
                '<td class="right mono">' + price(row.exit_price) + '</td><td class="right mono">' + number(row.profit_pips, 1) + '</td>' +
                '<td class="right mono ' + signedClass(row.profit_usd) + '">' + money(row.profit_usd, (state.account || {}).currency) + '</td>' +
                '<td class="right mono">' + (isNum(row.r_multiple) ? number(row.r_multiple, 2) + 'R' : '—') + '</td><td>' + tag(result, resultKind) + '</td></tr>';
        }), 'No closed trades match these filters', 11);
    }
    function historyRangeValues() {
        var startInput = $('#hist-start'), endInput = $('#hist-end'), key = ($('#hist-range') || {}).value || '30d';
        var range = getRange(key);
        return {
            start: startInput && startInput.value ? startInput.value : range.start,
            end: endInput && endInput.value ? endInput.value : range.end
        };
    }
    async function loadHistory() {
        var range = historyRangeValues();
        var params = new URLSearchParams({ limit: String(historyPageSize), offset: String(historyPage * historyPageSize) });
        if (range.start) params.set('start', range.start);
        if (range.end) params.set('end', range.end);
        var strategy = ($('#hist-strategy') || {}).value || 'all';
        var result = ($('#hist-result') || {}).value || 'all';
        if (strategy !== 'all') params.set('strategy', strategy);
        if (result !== 'all') params.set('result', result);
        renderHistory(await api('/api/trades?' + params.toString()));
    }
    function populateStrategies(strategies) {
        var select = $('#hist-strategy');
        if (!select) return;
        var current = select.value || 'all';
        var names = (strategies || []).map(function (row) { return row.strategy; }).filter(Boolean);
        select.innerHTML = '<option value="all">All strategies</option>' + Array.from(new Set(names)).sort().map(function (name) {
            return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
        }).join('');
        if (Array.from(select.options).some(function (option) { return option.value === current; })) select.value = current;
    }

    async function openTrade(id) {
        try {
            var payload = await api('/api/trades/' + encodeURIComponent(id));
            var trade = payload.trade || {};
            setText('drawer-title', (trade.trade_type || 'Trade') + ' · ' + (trade.ticket || '—'));
            setText('drawer-sub', (trade.strategy || '—') + ' · ' + (trade.symbol || '—') + ' · ' + (trade.status || '—'));
            var items = [
                ['Open time', dateText(trade.open_time, true)], ['Close time', dateText(trade.close_time, true)],
                ['Entry', price(trade.entry_price)], ['Exit', price(trade.exit_price)], ['Stop loss', price(trade.sl)],
                ['Take profit', price(trade.tp)], ['Volume', number(trade.lots, 2)], ['Pips', number(trade.profit_pips, 1)],
                ['Net P/L', money(trade.profit_usd, (state.account || {}).currency)],
                ['R multiple', isNum(trade.r_multiple) ? number(trade.r_multiple, 3) + 'R' : '—'],
                ['Maximum adverse excursion', isNum(trade.mae_r) ? number(trade.mae_r, 3) + 'R' : '—'],
                ['Maximum favourable excursion', isNum(trade.mfe_r) ? number(trade.mfe_r, 3) + 'R' : '—']
            ];
            var html = '<dl class="detail-list detail-grid">' + items.map(function (item) {
                return '<div><dt>' + esc(item[0]) + '</dt><dd>' + esc(item[1]) + '</dd></div>';
            }).join('') + '</dl>';
            if (payload.executions && payload.executions.length) {
                html += '<h3>Execution records</h3><div class="table-wrap"><table class="table"><thead><tr><th>Order</th><th>Deal</th><th>Position</th><th>Requested</th><th>Filled</th><th>Volume</th></tr></thead><tbody>' +
                    payload.executions.map(function (row) {
                        return '<tr><td>' + esc(row.order_ticket || '—') + '</td><td>' + esc(row.deal_ticket || '—') + '</td><td>' + esc(row.position_id || '—') + '</td>' +
                            '<td>' + price(row.requested_price) + '</td><td>' + price(row.fill_price) + '</td><td>' + number(row.filled_volume, 2) + '</td></tr>';
                    }).join('') + '</tbody></table></div>';
            }
            if (payload.prediction) html += '<h3>Model prediction</h3><p class="panel-note">' + esc(payload.prediction.model_version || '—') + ' · confidence ' + pct(payload.prediction.probability, true) + ' · ' + esc(payload.prediction.decision || '—') + '</p>';
            if (payload.signal) html += '<h3>Signal</h3><p class="panel-note">' + esc(payload.signal.strategy || '—') + ' ' + esc(payload.signal.signal_type || '—') + ' at ' + esc(dateText(payload.signal.signal_time, true)) + '</p>';
            setHTML('drawer-body', html);
            var drawer = $('#drawer'), scrim = $('#drawer-scrim');
            if (drawer) drawer.hidden = false;
            if (scrim) scrim.hidden = false;
        } catch (error) { showError(error); }
    }
    function closeDrawer() {
        var drawer = $('#drawer'), scrim = $('#drawer-scrim');
        if (drawer) drawer.hidden = true;
        if (scrim) scrim.hidden = true;
    }

    function renderMLStatus() {
        if (!state.ml) return;
        var status = state.ml.status || {};
        var last = status.ml_last || {};
        if (last && Object.keys(last).length) {
            var p = isNum(last.probability) ? ' · latest confidence ' + pct(last.probability, true) : '';
            setText('ml-gate-state', (status.active ? 'Active' : 'Inactive') + p);
        }
    }
    function renderML(payload) {
        state.ml = payload || {};
        var registry = state.ml.registry || {};
        var oos = registry.out_of_sample || {};
        var classification = oos.classification || {};
        var baseline = oos.baseline || {};
        var gated = oos.gated || {};
        var configured = state.ml.configured || {};
        var status = state.ml.status || {};
        var version = registry.version || 'No model file recorded';
        setText('ml-version', version);
        setText('ml-trained', registry.trained_at ? 'Trained ' + dateText(registry.trained_at, true) : 'No training timestamp');
        setText('ml-accuracy', Number(classification.samples || 0) > 0 ? pct(classification.accuracy, true) : '—');
        setText('ml-accuracy-note', isNum(classification.samples) ? number(classification.samples, 0) + ' out-of-sample labels' : 'No recorded evaluation');
        setText('ml-auc', number(classification.auc, 3));
        setText('ml-auc-note', 'Out-of-sample ranking quality');
        setText('ml-threshold', isNum(configured.confidence_min) ? pct(configured.confidence_min, true) : pct(registry.threshold, true));
        setText('ml-gate-state', status.active ? 'Gate active in engine' : configured.enabled ? 'Configured; engine not active' : 'Disabled in configuration');
        var banner = $('#ml-banner');
        if (banner) banner.textContent = registry.version
            ? 'Out-of-sample results come from the saved model registry. Live outcomes below come from resolved database predictions.'
            : 'No model registry entry is available. Train a model to record accuracy, confidence and comparison results.';
        var bands = oos.confidence_bands || [];
        chartBars('ml-bands', bands.map(function (row) { return row.band; }), [
            { name: 'Win rate', values: bands.map(function (row) { return Number(row.win_rate || 0) * 100; }), color: '#57d5ca' }
        ], { format: function (v) { return number(v, 0) + '%'; }, emptyText: 'No out-of-sample confidence bands recorded' });
        chartBars('ml-comparison', ['Rule-based', 'ML gated'], [
            { name: 'Average R / trade', values: [baseline.avg_r, gated.avg_r], color: '#7ab6ff' }
        ], { format: function (v) { return number(v, 3) + 'R'; }, emptyText: 'No model comparison recorded' });
        detailList('ml-classifier', [
            ['Samples', number(classification.samples, 0)], ['Precision', pct(classification.precision, true)],
            ['Recall', pct(classification.recall, true)], ['F1', number(classification.f1, 3)],
            ['Brier score', number(classification.brier, 3)], ['Base win rate', pct(classification.base_rate, true)]
        ].filter(function (item) { return item[1] !== '—'; }));
        var monitor = state.ml.monitor || {};
        var liveMetrics = monitor.live_classification || {};
        var taken = monitor.taken_economics || {};
        setText('ml-monitor-window', monitor.window_days ? 'Last ' + number(monitor.window_days, 0) + ' days · observed database predictions' : 'No live prediction window recorded');
        detailList('ml-monitor', [
            ['Predictions', number(monitor.predictions, 0)], ['Taken', number(monitor.taken, 0)],
            ['Resolved', number(monitor.resolved, 0)], ['Live accuracy', Number(liveMetrics.samples || 0) > 0 ? pct(liveMetrics.accuracy, true) : '—'],
            ['Taken win rate', Number(taken.trades || 0) > 0 ? pct(taken.win_rate, true) : '—'],
            ['Taken expectancy', isNum(taken.avg_r) ? number(taken.avg_r, 3) + 'R' : '—'],
            ['Latest confidence', status.ml_last && isNum(status.ml_last.probability) ? pct(status.ml_last.probability, true) : '—']
        ]);
        detailList('ml-summary', [
            ['Version', registry.version], ['Training source', registry.data && registry.data.source],
            ['Bars', number(registry.data && registry.data.bars, 0)], ['Signals', number(registry.samples && registry.samples.signals, 0)],
            ['Trainable signals', number(registry.samples && registry.samples.trainable, 0)],
            ['Walk-forward folds', number(registry.out_of_sample && registry.out_of_sample.folds_scored, 0)],
            ['Threshold', isNum(configured.confidence_min) ? pct(configured.confidence_min, true) : pct(registry.threshold, true)],
            ['Model file', configured.model_path || registry.path]
        ].filter(function (item) { return item[1] !== undefined && item[1] !== null && item[1] !== ''; }));
        renderMLStatus();
    }
    function renderModels(payload) {
        state.models = payload || {};
        var versions = payload.versions || [];
        setText('training-active', payload.active ? 'Active · ' + payload.active : 'No database model marked active');
        tableRows('training-versions', versions.map(function (row) {
            var oos = row.out_of_sample || {};
            var classification = oos.classification || {};
            var samples = row.samples || {};
            return '<tr><td class="mono">' + esc(row.version || '—') + '</td><td>' + esc(dateText(row.created_at || row.trained_at, true)) + '</td>' +
                '<td class="right mono">' + number((row.data || {}).bars, 0) + '</td><td class="right mono">' + number(samples.signals, 0) + '</td>' +
                '<td class="right mono">' + (isNum(classification.accuracy) ? pct(classification.accuracy, true) : '—') + '</td>' +
                '<td class="right mono">' + number(classification.auc, 3) + '</td><td>' +
                tag(row.is_active ? 'Active' : row.registry_only ? 'Registry' : 'Archived', row.is_active ? 'on' : row.registry_only ? 'warn' : 'off') + '</td></tr>';
        }), 'No model versions recorded in the database', 7);
        var runs = payload.runs || [];
        tableRows('training-runs', runs.map(function (row) {
            var metrics = row.metrics || {};
            var aggregate = metrics.aggregate || {};
            var gated = aggregate.gated || {};
            var samples = row.trainable || (row.samples || {}).trainable;
            return '<tr><td class="mono">' + esc(dateText(row.started_at || row.finished_at, true)) + '</td><td>' + esc(row.status || '—') + '</td>' +
                '<td class="right mono">' + number(row.folds, 0) + '</td><td class="right mono">' + number(samples, 0) + '</td>' +
                '<td class="right mono">' + (isNum(gated.total_r) ? number(gated.total_r, 2) + 'R' : '—') + '</td></tr>';
        }), 'No training runs recorded', 5);
        var latest = runs[0] && runs[0].metrics || {};
        var folds = latest.folds || [];
        chartLine('training-folds', folds.filter(function (row) { return row.gated; }).map(function (row) { return 'Fold ' + row.fold; }), [
            { name: 'Rule-based total R', values: folds.filter(function (row) { return row.gated; }).map(function (row) { return Number((row.baseline || {}).total_r || 0); }), color: '#7ab6ff', fill: false },
            { name: 'ML gated total R', values: folds.filter(function (row) { return row.gated; }).map(function (row) { return Number((row.gated || {}).total_r || 0); }), color: '#57d5ca', fill: false }
        ], { format: function (v) { return number(v, 1) + 'R'; }, includeZero: true, emptyText: 'Per-fold data is not recorded in the database' });
        var monitor = payload.monitor || {};
        detailList('training-live', [
            ['Model version', payload.active || (state.ml && state.ml.registry && state.ml.registry.version) || '—'],
            ['Live predictions', number(monitor.predictions, 0)], ['Resolved outcomes', number(monitor.resolved, 0)],
            ['Live accuracy', Number((monitor.live_classification || {}).samples || 0) > 0 ? pct(monitor.live_classification.accuracy, true) : '—'],
            ['Live expectancy', Number((monitor.taken_economics || {}).trades || 0) > 0 ? number(monitor.taken_economics.avg_r, 3) + 'R' : '—'],
            ['Out-of-sample expectancy', isNum((((state.ml || {}).registry || {}).out_of_sample || {}).gated && ((state.ml.registry.out_of_sample.gated).avg_r)) ? number(state.ml.registry.out_of_sample.gated.avg_r, 3) + 'R' : '—']
        ]);
    }

    function renderBacktests(payload) {
        var runs = payload.runs || [];
        tableRows('bt-table', runs.map(function (row) {
            var notes = row.notes || {};
            var gated = notes.gated || {};
            return '<tr><td>' + esc(row.run_label || 'Walk-forward') + '</td><td class="mono">' + esc(row.data_start || '—') + ' → ' + esc(row.data_end || '—') + '</td>' +
                '<td class="right mono">' + number(row.trades, 0) + '</td><td class="right mono">' + (isNum(row.win_rate) ? pct(row.win_rate, true) : '—') + '</td>' +
                '<td class="right mono">' + (isNum(row.total_r) ? number(row.total_r, 2) + 'R' : '—') + '</td>' +
                '<td class="right mono">' + (isNum(row.max_drawdown_r) ? number(row.max_drawdown_r, 2) + 'R' : '—') + '</td>' +
                '<td class="mono">' + esc(dateText(row.created_at, true)) + '</td></tr>';
        }), 'No backtest results stored yet', 7);
        setText('bt-count', number(payload.count, 0) + ' recorded runs');
        var job = payload.job || {};
        var status = $('#bt-status');
        if (status) status.textContent = job.status === 'running' ? 'Backtest running. The page will update when the walk-forward finishes.'
            : job.status === 'failed' ? 'Backtest failed: ' + (job.error || 'See system events for details.')
                : job.status === 'done' ? 'Completed ' + dateText(job.finished_at, true) + ' · results saved in the database.' : 'No run started in this session.';
        var runButton = $('#bt-run');
        if (runButton) runButton.disabled = job.status === 'running';
        var result = job.result;
        var box = $('#bt-result');
        if (box) {
            box.hidden = !result;
            if (result) {
                var base = result.baseline || {}, gated = result.gated || {};
                box.innerHTML = '<div class="grid grid-2"><div><h3>Rule-based</h3><p>' + number(base.trades, 0) + ' trades · ' +
                    (isNum(base.win_rate) ? pct(base.win_rate, true) : '—') + ' win rate · ' + number(base.total_r, 2) + 'R</p></div>' +
                    '<div><h3>ML gated</h3><p>' + number(gated.trades, 0) + ' trades · ' +
                    (isNum(gated.win_rate) ? pct(gated.win_rate, true) : '—') + ' win rate · ' + number(gated.total_r, 2) + 'R</p></div></div>' +
                    '<p class="panel-note">Data: ' + esc(result.source || '—') + ' · ' + number(result.bars, 0) + ' bars</p>';
            }
        }
        if (job.status === 'running' && !backtestTimer) backtestTimer = setInterval(function () {
            api('/api/backtests').then(renderBacktests).catch(showError);
        }, 2500);
        if (job.status !== 'running' && backtestTimer) { clearInterval(backtestTimer); backtestTimer = null; }
    }

    function renderHealth(payload) {
        var database = payload.database || {};
        var file = payload.file || {};
        var engine = payload.engine || {};
        detailList('health-db', [
            ['Health', database.error ? 'Error' : database.schema_version ? 'Healthy' : (payload.error || 'Available')],
            ['Schema version', payload.schema_version], ['Journal mode', database.journal_mode],
            ['Foreign keys', database.foreign_keys], ['Busy timeout', isNum(database.busy_timeout) ? number(database.busy_timeout, 0) + ' ms' : '—'],
            ['File size', isNum(file.size_bytes) ? number(file.size_bytes, 0) + ' bytes' : '—'], ['Modified', dateText(file.modified_at, true)]
        ]);
        var counts = payload.counts || database.counts || {};
        detailList('health-counts', Object.keys(counts).sort().map(function (key) { return [key.replace(/_/g, ' '), number(counts[key], 0)]; }));
        detailList('health-engine', [
            ['Status', engine.status || (engine.running ? 'Running' : 'Stopped')], ['MT5', engine.mt5_connected ? 'Connected' : 'Disconnected'],
            ['Readiness', engine.mt5_ready ? 'Ready' : 'Not ready'], ['Feed age', isNum(engine.feed_age_seconds) ? number(engine.feed_age_seconds, 1) + ' sec' : '—'],
            ['Reconnects', number(engine.reconnects, 0)], ['Trading allowed', engine.trading_allowed === true ? 'Yes' : engine.trading_allowed === false ? 'No' : '—'],
            ['Risk gate', engine.risk_reason || '—'], ['ML gate', engine.ml && engine.ml.active ? 'Active' : 'Inactive'],
            ['Last error', engine.error || '—']
        ]);
    }
    async function loadLogs() {
        var kind = ($('#log-kind') || {}).value || 'system';
        var level = ($('#log-level') || {}).value || '';
        var qs = new URLSearchParams({ kind: kind, limit: '100' });
        if (level) qs.set('level', level);
        var payload = await api('/api/logs?' + qs.toString());
        var rows = payload.events || [];
        tableRows('logs-table', rows.map(function (event) {
            return '<tr><td class="mono">' + esc(dateText(event.event_time || event.created_at, true)) + '</td>' +
                '<td>' + tag(event.level || event.severity || 'info', event.level === 'error' ? 'off' : event.level === 'warning' || event.severity === 'warning' ? 'warn' : 'on') + '</td>' +
                '<td>' + esc(event.category || event.event_type || '—') + '</td><td>' + esc(event.source || '—') + '</td>' +
                '<td>' + esc(event.message || '—') + '</td></tr>';
        }), 'No events recorded for these filters', 5);
    }
    async function loadRiskEvents() {
        var payload = await api('/api/logs?kind=risk&limit=50');
        renderRiskEvents(payload.events || []);
    }
    async function loadBacktests() { renderBacktests(await api('/api/backtests')); }

    async function runBacktest() {
        var payload = {
            start: ($('#bt-start') || {}).value || null,
            end: ($('#bt-end') || {}).value || null,
            label: ($('#bt-label') || {}).value || ''
        };
        if (payload.start && payload.end && payload.start > payload.end) {
            toast('The start date must be before the end date.', true); return;
        }
        try {
            await api('/api/backtests/run', { method: 'POST', body: JSON.stringify(payload) });
            toast('Walk-forward backtest started.');
            await loadBacktests();
        } catch (error) { showError(error); }
    }

    async function loadTradeDetails() {
        await loadHistory();
    }
    async function loadPageData(page) {
        try {
            if (page === 'overview') await Promise.all([loadAccount(), loadPerformance('overview', lastPerformanceRange.key ? getRange(lastPerformanceRange.key) : getRange('90d'))]);
            if (page === 'performance') await loadPerformance('performance', lastPerformanceRange.key ? getRange(lastPerformanceRange.key) : getRange('30d'));
            if (page === 'positions') await loadPositions();
            if (page === 'history') await loadTradeDetails();
            if (page === 'strategies' || page === 'risk') await loadConfig();
            if (page === 'risk') await loadRiskEvents();
            if (page === 'ml') await loadML();
            if (page === 'training') await loadModels();
            if (page === 'backtesting') await loadBacktests();
            if (page === 'connection') await loadState();
            if (page === 'logs') await Promise.all([loadHealth(), loadLogs()]);
        } catch (error) { showError(error); }
    }
    async function loadState() {
        var payload = await api('/state');
        renderHeader(payload);
        var positions = Object.assign({}, state.positions, {
            running: !!payload.running, connected: !!payload.mt5_connected,
            positions: payload.open_positions || [], pending: payload.pending_orders || [],
            symbol: (payload.mt5_symbol || {}).name, bid: payload.bid, ask: payload.ask,
            manual_positions: payload.manual_positions, trading_allowed: payload.trading_allowed,
            risk_reason: payload.risk_reason
        });
        renderPositions(positions);
        return payload;
    }
    async function loadAccount() {
        var result = await api('/api/account');
        renderAccount(result);
        return result;
    }
    async function loadPositions() {
        var result = await api('/api/positions');
        renderPositions(result);
        return result;
    }
    async function loadHealth() {
        var result = await api('/api/health');
        renderHealth(result);
        return result;
    }
    async function loadML() {
        var result = await api('/api/ml');
        renderML(result);
        return result;
    }
    async function loadModels() {
        var result = await api('/api/models');
        renderModels(result);
        return result;
    }

    function navigate() {
        var page = (window.location.hash || '#overview').slice(1);
        if (!titles[page]) page = 'overview';
        $$('.page').forEach(function (node) { node.classList.toggle('is-active', node.id === 'page-' + page); });
        $$('.nav-link').forEach(function (node) { node.classList.toggle('is-active', node.getAttribute('data-page') === page); });
        setText('page-title', titles[page][0]);
        setText('page-sub', titles[page][1]);
        closeNavigation();
        loadPageData(page);
    }
    function closeNavigation() {
        var sidebar = $('#sidebar'), scrim = $('#scrim');
        if (sidebar) sidebar.classList.remove('is-open');
        if (scrim) scrim.classList.remove('is-open');
    }
    function openNavigation() {
        var sidebar = $('#sidebar'), scrim = $('#scrim');
        if (sidebar) sidebar.classList.add('is-open');
        if (scrim) scrim.classList.add('is-open');
    }
    function setRangeButtons(group, range) {
        $$('[data-range-group="' + group + '"] [data-range]').forEach(function (button) {
            button.classList.toggle('is-active', button.getAttribute('data-range') === range);
        });
    }
    function bindRangeButtons() {
        $$('[data-range-group]').forEach(function (group) {
            group.addEventListener('click', function (event) {
                var button = event.target.closest('[data-range]');
                if (!button) return;
                var range = getRange(button.getAttribute('data-range'));
                setRangeButtons(group.getAttribute('data-range-group'), range.key);
                var target = group.getAttribute('data-range-group') === 'overview' ? 'overview' : 'performance';
                loadPerformance(target, range).catch(showError);
            });
        });
    }
    async function control(path, successMessage) {
        var button = path === '/start' ? $('#btn-start') : $('#btn-stop');
        if (button) button.disabled = true;
        try {
            var result = await api(path, { method: 'POST' });
            if (result.status === 'error') throw new Error(result.error || 'Engine could not start');
            toast(result.status === 'already_running' ? 'Engine is already running.' : successMessage);
            await Promise.all([loadState(), loadAccount(), loadPositions()]);
        } catch (error) {
            showError(error);
            try { await loadState(); } catch (_) { /* keep the original startup error visible */ }
        } finally {
            if (button) button.disabled = false;
            renderHeader(state.live);
        }
    }
    function bindEvents() {
        window.addEventListener('hashchange', navigate);
        var start = $('#btn-start'), stop = $('#btn-stop');
        if (start) start.addEventListener('click', function () { control('/start', 'Engine started.'); });
        if (stop) stop.addEventListener('click', function () { control('/stop', 'Engine stopped.'); });
        var menu = $('[data-open-nav]');
        if (menu) menu.addEventListener('click', openNavigation);
        var scrim = $('[data-close-nav]');
        if (scrim) scrim.addEventListener('click', closeNavigation);
        var apply = $('#perf-apply');
        if (apply) apply.addEventListener('click', function () {
            var startDate = ($('#perf-start') || {}).value || null, endDate = ($('#perf-end') || {}).value || null;
            if (startDate && endDate && startDate > endDate) { toast('The start date must be before the end date.', true); return; }
            setRangeButtons('performance', 'custom');
            loadPerformance('performance', { start: startDate, end: endDate, key: 'custom' }).catch(showError);
        });
        bindRangeButtons();
        var historyRefresh = $('#hist-refresh');
        if (historyRefresh) historyRefresh.addEventListener('click', function () { historyPage = 0; loadHistory().catch(showError); });
        ['hist-range', 'hist-start', 'hist-end', 'hist-strategy', 'hist-result'].forEach(function (id) {
            var input = document.getElementById(id);
            if (input) input.addEventListener('change', function () { historyPage = 0; loadHistory().catch(showError); });
        });
        var prev = $('#hist-prev'), next = $('#hist-next');
        if (prev) prev.addEventListener('click', function () { historyPage = Math.max(0, historyPage - 1); loadHistory().catch(showError); });
        if (next) next.addEventListener('click', function () { historyPage += 1; loadHistory().catch(showError); });
        var historyTable = $('#history-table');
        if (historyTable) historyTable.addEventListener('click', function (event) {
            var row = event.target.closest('[data-trade-id]');
            if (row) openTrade(row.getAttribute('data-trade-id'));
        });
        var drawerClose = $('#drawer-close'), drawerScrim = $('#drawer-scrim');
        if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
        if (drawerScrim) drawerScrim.addEventListener('click', closeDrawer);
        var logsRefresh = $('#log-refresh');
        if (logsRefresh) logsRefresh.addEventListener('click', function () { Promise.all([loadHealth(), loadLogs()]).catch(showError); });
        ['log-kind', 'log-level'].forEach(function (id) {
            var input = document.getElementById(id);
            if (input) input.addEventListener('change', function () { loadLogs().catch(showError); });
        });
        var backtest = $('#bt-run');
        if (backtest) backtest.addEventListener('click', runBacktest);
        var graph = $('#graph-form');
        if (graph) graph.addEventListener('submit', async function (event) {
            event.preventDefault();
            var fileInput = $('#graph-file');
            if (!fileInput || !fileInput.files || !fileInput.files[0]) { toast('Choose a chart image first.', true); return; }
            var body = new FormData();
            body.append('file', fileInput.files[0]);
            try {
                var result = await api('/upload_graph', { method: 'POST', body: body, headers: { 'X-Requested-With': 'XMLHttpRequest' } });
                detailList('graph-result', Object.keys(result).map(function (key) {
                    var value = result[key];
                    return [key.replace(/_/g, ' '), typeof value === 'object' ? JSON.stringify(value) : String(value)];
                }));
                var preview = $('#graph-preview');
                if (preview) { preview.src = URL.createObjectURL(fileInput.files[0]); preview.hidden = false; }
                if (result.error) toast(result.error, true);
                else toast('Chart image analysed.');
            } catch (error) { showError(error); }
        });
        document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeDrawer(); });
    }

    async function initialLoad() {
        try {
            await loadState();
            var initialPerformance = getRange('90d');
            lastPerformanceRange = initialPerformance;
            await Promise.all([
                loadAccount().catch(showError), loadPositions().catch(showError),
                loadPerformance('overview', initialPerformance).catch(showError),
                api('/api/analytics/strategies').then(populateStrategies).catch(function () {}),
                loadConfig().catch(showError), loadML().catch(showError), loadModels().catch(showError),
                loadHealth().catch(showError), loadLogs().catch(showError)
            ]);
            navigate();
        } catch (error) { showError(error); }
        setInterval(function () {
            Promise.all([loadState(), loadAccount(), loadPositions()]).catch(function () { /* a later poll can recover */ });
        }, 4000);
        setInterval(function () {
            if (window.location.hash === '#overview' || window.location.hash === '#performance') {
                loadPerformance(window.location.hash === '#overview' ? 'overview' : 'performance', lastPerformanceRange).catch(function () {});
            }
        }, 30000);
    }

    bindEvents();
    if (window.io) {
        try {
            var socket = window.io({ transports: ['websocket', 'polling'] });
            socket.on('state_update', function (payload) {
                renderHeader(payload);
                var positions = Object.assign({}, state.positions, {
                    running: !!payload.running, connected: !!payload.mt5_connected,
                    positions: payload.open_positions || [], pending: payload.pending_orders || [],
                    symbol: (payload.mt5_symbol || {}).name, bid: payload.bid, ask: payload.ask,
                    manual_positions: payload.manual_positions, trading_allowed: payload.trading_allowed,
                    risk_reason: payload.risk_reason
                });
                renderPositions(positions);
                if (payload.ml) { state.ml = Object.assign({}, state.ml || {}, { status: payload.ml }); renderMLStatus(); }
                if (payload.mt5_account && payload.mt5_account.balance !== undefined) {
                    var account = Object.assign({}, state.account || {}, {
                        account: payload.mt5_account,
                        balance: payload.mt5_account.balance,
                        equity: payload.mt5_account.equity,
                        margin_free: payload.mt5_account.margin_free,
                        margin_used: payload.mt5_account.margin,
                        currency: payload.mt5_account.currency,
                        live: !!payload.mt5_connected,
                        engine_running: !!payload.running,
                        daily_pnl: payload.daily_pnl
                    });
                    renderAccount(account);
                }
            });
        } catch (_) { /* HTTP polling remains available if the socket client is offline. */ }
    }
    initialLoad();
})();
