# config/settings.py
import datetime as _dt

# === ACCOUNT ===
ACCOUNT_BALANCE = 10000
RISK_PER_TRADE = 0.01          # 1% per trade
MAX_DAILY_RISK = 0.03          # 3% max daily loss
MAX_CONCURRENT_TRADES = 3
MAX_DAILY_TRADES = 10

# === DAILY BREAKOUT ===
BREAKOUT_ENABLED = True
BREAKOUT_SYMBOL = "XAUUSD"
BREAKOUT_ASIAN_START = _dt.time(0, 0)
BREAKOUT_ASIAN_END = _dt.time(7, 0)
BREAKOUT_SESSION_START = _dt.time(7, 0)
BREAKOUT_SESSION_END = _dt.time(12, 0)
BREAKOUT_MIN_RANGE = 0.20       # Minimum Asian range in dollars
BREAKOUT_ENTRY_ATR_MULT = 1.0   # Max distance from breakout level (ATR)
BREAKOUT_SL_PCT = 0.50          # SL = 50% of Asian range
BREAKOUT_TP_RR = 1.5            # TP = 1.5x risk
BREAKOUT_RSI_LONG_MIN = 45
BREAKOUT_RSI_SHORT_MAX = 55

# === LONDON SCALPER ===
SCALPER_ENABLED = True
SCALPER_SYMBOL = "XAUUSD"
SCALPER_TIMEFRAME = "M5"        # M1 or M5
SCALPER_SESSION_START = _dt.time(8, 0)
SCALPER_SESSION_END = _dt.time(16, 0)
SCALPER_EMA_FAST = 20
SCALPER_EMA_SLOW = 50
SCALPER_ADX_MIN = 25
SCALPER_SL_ATR = 1.5            # SL = 1.5 ATR
SCALPER_TP_ATR = 2.5            # TP = 2.5 ATR (1.67:1 RR)
SCALPER_RSI_OVERSOLD = 35
SCALPER_RSI_OVERBOUGHT = 65
SCALPER_PULLBACK_ATR = 0.8      # Pullback zone from EMA
SCALPER_MIN_BAR_SPACING = 3     # Minimum bars between trades