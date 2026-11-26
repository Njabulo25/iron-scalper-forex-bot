# Iron Scalper

A MetaTrader 5 trading engine for XAUUSD with a responsive quantitative trading dashboard.

## Strategies

- **Daily breakout:** trades breaks of the configured Asian session range.
- **London scalper:** looks for EMA pullbacks during its configured session, with RSI and ATR filters.

Dashboard performance comes from the local trade database. Model figures come from the saved model registry and recorded live predictions. Empty records are shown as empty; the dashboard does not substitute sample statistics.

## Quick start on Windows

### Prerequisites

- Python 3.12
- MetaTrader 5 installed and signed in to the account you intend to use

### Create an operator account and start the dashboard

Run setup once, create an administrator account, then start the dashboard:

```powershell
setup.bat
.venv\Scripts\python.exe -m src.engine.auth add-user --username operator --role admin
run.bat
```

Choose a passphrase of at least 12 characters when prompted. Visit <http://127.0.0.1:5000> and sign in. Add read-only operators with `--role viewer`. Reset a passphrase with:

```powershell
.venv\Scripts\python.exe -m src.engine.auth reset-passphrase --username operator
```

There are no built-in dashboard credentials. Existing database PINs are converted to salted scrypt hashes during migration, and short PINs are no longer accepted. Reset an existing database user's passphrase with the command above; create an account if the database has no users. The migration preserves trade history and other records.

Starting the dashboard does not start live trading. The engine refuses real MT5 accounts by default. Use a demo account, confirm the account and symbol in MT5, then press **Start engine** in the dashboard. Real-account trading requires the explicit `IRON_SCALPER_ALLOW_LIVE_TRADING=1` setting. Set `IRON_SCALPER_MT5_LOGIN`, `IRON_SCALPER_MT5_SERVER`, and optionally `IRON_SCALPER_MT5_PATH` to pin the intended trading account and terminal. If MT5 cannot connect, the account identity changes, or a readiness check fails, the engine refuses new orders and reports the reason.

For a local network deployment, use a TLS reverse proxy with a stable `IRON_SCALPER_SECRET_KEY` and `IRON_SCALPER_COOKIE_SECURE=1`, then set `IRON_SCALPER_ALLOW_REMOTE=1` before binding to a non-local address. The app rejects remote binding without those settings. Do not expose Flask's development server directly to the internet.

## Data and research

- Trades, risk decisions, model runs, events and collected market bars are stored in `data/iron_scalper.db`.
- Model files and their registry are stored in `models/`.
- Use `python train_ml.py --help` to see the supported model training and history collection commands.
- Use the dashboard's **Backtesting** page to run a walk-forward evaluation over history already stored in the database or in the configured CSV path.
- Use `python -m src.research.web_search --symbol XAUUSD --days 3` to pull recent Exa web results with the sentences that matched. It is read-only, never becomes a trading signal, and needs `EXA_API_KEY` in the environment (see `.env.example`) plus `exa-py` from `requirements.txt`.
- Use `python -m src.engine.auth --help` to see dashboard account commands.

## Trading controls and limits

- The scalper reads M1 bars with a five-bar spacing; the daily breakout stays on M5. The entry ceiling is 50 per day. The signal rules and risk checks can produce fewer entries, so this is not a guaranteed fill quota.
- Position size uses MT5's account-currency stop-loss estimate, rounds down to the broker's volume step, and skips the trade if the minimum volume would exceed the configured risk budget.
- A market-data outage, stale signal, stale quote, account/server switch, broker restriction, or failed order pre-check blocks new orders. The loop reconnects automatically and backs off up to 15 seconds after repeated exceptions.
- **Stop engine** stops new entries; it does not close open MT5 positions. Existing positions remain at the broker with their attached SL/TP. Review or close them in MT5.
- The dashboard refreshes engine state every 4 seconds. The trading loop checks every 2 seconds by default and generates strategy signals once per newly closed bar.
- The health page reports the last completed loop heartbeat and duration. A stale heartbeat is shown as stale; the page does not attempt to kill a thread blocked inside the MT5 library.
- The chart upload tool validates PNG/JPEG content and reports image brightness/contrast only. It does not read price direction or create a trading signal.

## Hardware audit scope

This project controls MetaTrader 5 through its Python API. It has no motors, physical sensors, GPIO/I2C/SPI/UART buses, robot battery, or robot firmware. Hardware wiring, calibration, motion control, physical E-stop, and OTA firmware checks do not apply. The software safety controls are account pinning, stale-data rejection, risk limits, connection recovery, order validation, and authenticated dashboard controls.

## Tests

Run the offline checks with the project environment:

```powershell
.venv\Scripts\python.exe tests\test_live_pipeline.py
.venv\Scripts\python.exe tests\test_database_schema.py
.venv\Scripts\python.exe tests\test_project_audit.py
```

The trading checks stub MT5 and use temporary SQLite databases; they do not connect to a broker or place real orders.

## Risk warning

Forex and CFD trading involves substantial risk of loss. Past performance does not guarantee future results. Start with a demo account and only trade with money you can afford to lose.
