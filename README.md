# Iron Scalper

A MetaTrader 5 trading engine for XAUUSD with a responsive quantitative trading dashboard.

## Strategies

- **Daily breakout:** trades breaks of the configured Asian session range.
- **London scalper:** looks for EMA pullbacks during its configured session, with RSI and ATR filters.

Performance in the dashboard comes from the local trade database. Model figures come from the saved model registry and recorded live predictions. Empty records are shown as empty; the dashboard does not substitute sample statistics.

## Quick start on Windows

### Prerequisites

- Python 3.12
- MetaTrader 5 installed and signed in to the account you intend to use

### Start the terminal

Double-click `run.bat`. On the first run, it creates `.venv` and installs the runtime dependencies. You can run `setup.bat` separately if you prefer to install them before opening the dashboard.

Or use a terminal:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.venv\Scripts\python.exe main.py
```

Open <http://127.0.0.1:5000> and sign in. Starting the dashboard does not start live trading. Sign in to MT5, confirm the account and symbol in the terminal, then press **Start engine** in the dashboard. If MT5 cannot connect or fails a readiness check, the dashboard reports the reason and leaves the engine stopped.

If more than one MT5 account or terminal is installed, you can select the intended saved trading account before starting the dashboard by setting `IRON_SCALPER_MT5_LOGIN`, `IRON_SCALPER_MT5_SERVER`, and optionally `IRON_SCALPER_MT5_PATH` to the full `terminal64.exe` path. The trading login and broker server must match the account saved in MT5; an MQL5 Community login is not a trading account.

The current local login accounts are listed in `src/engine/auth.py`.

To access the dashboard from another device on a trusted local network, start it with `run.bat --host 0.0.0.0`, then open the host computer's LAN address on the other device.

## Data and research

- Trades, risk decisions, model runs, events and collected market bars are stored in `data/iron_scalper.db`.
- Model files and their registry are stored in `models/`.
- Use `python train_ml.py --help` to see the supported model training and history collection commands.
- Use the dashboard's **Backtesting** page to run a walk-forward evaluation over history already stored in the database or in the configured CSV path.

## Risk warning

Forex and CFD trading involves substantial risk of loss. Past performance does not guarantee future results. Start with a demo account and only trade with money you can afford to lose.
