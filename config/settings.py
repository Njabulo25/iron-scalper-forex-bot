"""Central configuration for Iron Scalper.

One module, one source of truth. Risk, execution, the MT5 session, each
strategy's geometry and the (inactive) machine-learning gate are defined here
and read from here by the strategies, the live loop, the backtest and the
dashboard. The dashboard renders this object, so it cannot drift from what the
engine actually trades.

Defaults reproduce the values that were hardcoded inside the strategy modules
before this consolidation - i.e. the numbers the recorded backtest was measured
with. The previous module-level constants were partly ignored by the engine
(for example ``BREAKOUT_MIN_RANGE = 0.20`` sat next to a hardcoded ``0.15``),
which is exactly the kind of contradiction this module removes.

Nothing else in the codebase should define a trading parameter.
"""

from dataclasses import asdict, dataclass
from datetime import time
import os

TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


# ── Risk ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskConfig:
    """Sizing, exposure and the daily brakes."""

    risk_per_trade: float = 0.01        # Fraction of balance risked per trade
    max_daily_loss_pct: float = 0.03    # Daily loss limit, of the day's opening balance
    max_daily_trades: int = 50
    max_open_positions: int = 3
    max_consecutive_losses: int = 3     # Stop for the rest of the day at this streak
    same_direction_guard_usd: float = 5.0   # Ignore a duplicate this close to an open entry
    starting_balance: float = 10000.0   # Only used before MT5 reports the real balance

    def validate(self):
        if not 0 < self.risk_per_trade < 1:
            raise ValueError("risk.risk_per_trade must be between 0 and 1")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("risk.max_daily_loss_pct must be between 0 and 1")
        if self.max_daily_trades < 1:
            raise ValueError("risk.max_daily_trades must be at least 1")
        if self.max_open_positions < 1:
            raise ValueError("risk.max_open_positions must be at least 1")
        if self.max_consecutive_losses < 1:
            raise ValueError("risk.max_consecutive_losses must be at least 1")
        if self.same_direction_guard_usd < 0:
            raise ValueError("risk.same_direction_guard_usd cannot be negative")
        if self.starting_balance <= 0:
            raise ValueError("risk.starting_balance must be positive")


# ── Execution ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionConfig:
    """How often the loop runs and when it refuses to act."""

    loop_seconds: float = 2.0
    max_signal_age_seconds: float = 120.0    # Execute a signal younger than this
    history_scan_seconds: float = 15.0       # Closed-deal poll interval
    position_sync_seconds: float = 30.0      # MT5 <-> database reconciliation interval
    max_spread_points: float = 50.0          # Skip entries wider than this, in price x 100

    def validate(self):
        if self.loop_seconds <= 0:
            raise ValueError("execution.loop_seconds must be positive")
        if self.max_signal_age_seconds < 0:
            raise ValueError("execution.max_signal_age_seconds cannot be negative")
        if self.history_scan_seconds <= 0:
            raise ValueError("execution.history_scan_seconds must be positive")
        if self.position_sync_seconds <= 0:
            raise ValueError("execution.position_sync_seconds must be positive")
        if self.max_spread_points <= 0:
            raise ValueError("execution.max_spread_points must be positive")


# ── MetaTrader 5 ───────────────────────────────────────────────────


@dataclass(frozen=True)
class MT5Config:
    """Which terminal/account to trade and how to behave when it misbehaves."""

    symbol: str = "XAUUSD"
    magic: int = 123456                     # Marks this bot's orders in MT5
    deviation_points: int = 20              # Max price deviation on a market order
    retry_attempts: int = 2                 # Attempts on a requote or fill-mode rejection
    reconnect_seconds: float = 5.0          # First backoff between reconnect attempts
    max_backoff_seconds: float = 60.0       # Cap for the exponential reconnect backoff
    feed_stale_seconds: float = 30.0        # Refuse entries on quotes older than this

    # Terminal identity. Empty/0 means "accept whatever the terminal is signed
    # into"; set them to pin one account so a mis-launched terminal cannot trade.
    terminal_path: str = os.environ.get("IRON_SCALPER_MT5_PATH", "")
    expected_login: int = int(os.environ.get("IRON_SCALPER_MT5_LOGIN", "0") or 0)
    expected_server: str = os.environ.get("IRON_SCALPER_MT5_SERVER", "")
    require_algo_trading: bool = True
    allow_live_trading: bool = os.environ.get("IRON_SCALPER_ALLOW_LIVE_TRADING", "").lower() in {
        "1", "true", "yes",
    }

    def validate(self):
        if not self.symbol:
            raise ValueError("mt5.symbol cannot be empty")
        if self.magic <= 0:
            raise ValueError("mt5.magic must be positive")
        if self.deviation_points < 0:
            raise ValueError("mt5.deviation_points cannot be negative")
        if self.retry_attempts < 1:
            raise ValueError("mt5.retry_attempts must be at least 1")
        if self.reconnect_seconds <= 0:
            raise ValueError("mt5.reconnect_seconds must be positive")
        if self.max_backoff_seconds < self.reconnect_seconds:
            raise ValueError("mt5.max_backoff_seconds cannot be below reconnect_seconds")
        if self.feed_stale_seconds <= 0:
            raise ValueError("mt5.feed_stale_seconds must be positive")
        if self.expected_login < 0:
            raise ValueError("mt5.expected_login cannot be negative")


# ── Strategies ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class BreakoutConfig:
    """Asian-range breakout: geometry measured over 187 days of M5 gold."""

    enabled: bool = True
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    asian_start: time = time(0, 0)
    asian_end: time = time(7, 0)
    session_start: time = time(7, 0)
    session_end: time = time(12, 0)
    min_asian_bars: int = 10
    atr_period: int = 14
    atr_multiplier: float = 1.5     # Max distance from the level, in ATR
    min_range: float = 0.15         # Minimum Asian range, in price
    sl_range_pct: float = 0.50      # Stop = this share of the Asian range
    tp_rr: float = 1.5              # Target = tp_rr x stop distance

    @property
    def session(self):
        return f"{self.session_start:%H:%M}-{self.session_end:%H:%M}"

    @property
    def asian_session(self):
        return f"{self.asian_start:%H:%M}-{self.asian_end:%H:%M}"

    def validate(self):
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"breakout.timeframe must be one of {', '.join(TIMEFRAMES)}")
        if self.asian_end <= self.asian_start:
            raise ValueError("breakout.asian_end must be after asian_start")
        if self.session_end <= self.session_start:
            raise ValueError("breakout.session_end must be after session_start")
        if self.min_asian_bars < 1:
            raise ValueError("breakout.min_asian_bars must be at least 1")
        if self.atr_period < 2:
            raise ValueError("breakout.atr_period must be at least 2")
        if self.atr_multiplier <= 0:
            raise ValueError("breakout.atr_multiplier must be positive")
        if self.min_range < 0:
            raise ValueError("breakout.min_range cannot be negative")
        if not 0 < self.sl_range_pct <= 1:
            raise ValueError("breakout.sl_range_pct must be in (0, 1]")
        if self.tp_rr <= 0:
            raise ValueError("breakout.tp_rr must be positive")


@dataclass(frozen=True)
class ScalperConfig:
    """London-session EMA pullback scalper, configured for one-minute bars."""

    enabled: bool = True
    symbol: str = "XAUUSD"
    timeframe: str = "M1"
    session_start: time = time(8, 0)
    session_end: time = time(16, 0)
    atr_period: int = 14
    rsi_period: int = 7
    ema_fast: int = 20
    ema_slow: int = 50
    pullback_atr: float = 0.8       # Entry only within this distance of the fast EMA
    sl_atr: float = 1.5
    tp_atr: float = 2.5
    rsi_long_min: float = 40.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 60.0
    min_bar_spacing: int = 5        # Five M1 bars between scalper entries

    @property
    def session(self):
        return f"{self.session_start:%H:%M}-{self.session_end:%H:%M}"

    def validate(self):
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"scalper.timeframe must be one of {', '.join(TIMEFRAMES)}")
        if self.session_end <= self.session_start:
            raise ValueError("scalper.session_end must be after session_start")
        if self.atr_period < 2 or self.rsi_period < 2:
            raise ValueError("scalper ATR/RSI periods must be at least 2")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("scalper.ema_fast must be below ema_slow")
        if self.pullback_atr <= 0 or self.sl_atr <= 0 or self.tp_atr <= 0:
            raise ValueError("scalper pullback/SL/TP ATR multiples must be positive")
        if not 0 <= self.rsi_long_min < self.rsi_long_max <= 100:
            raise ValueError("scalper RSI long bounds must satisfy 0 <= min < max <= 100")
        if not 0 <= self.rsi_short_min < self.rsi_short_max <= 100:
            raise ValueError("scalper RSI short bounds must satisfy 0 <= min < max <= 100")
        if self.min_bar_spacing < 1:
            raise ValueError("scalper.min_bar_spacing must be at least 1")


@dataclass(frozen=True)
class MLConfig:
    """The signal-confidence gate and the pipeline that produces its model.

    ``enabled`` is False by default: the model is trained and validated
    off-line (``python train_ml.py --train``) and switching the gate on is a
    deliberate trading decision, not a side effect of shipping code. The gate
    never places an order - it only decides whether a strategy signal is
    allowed to continue into the risk manager and the execution path.

    Labels are generated with the *strategy's own* stop and target geometry,
    so the model learns "would this particular signal have won", not a generic
    up/down direction. ``horizon_bars`` bounds that simulation; signals that
    hit neither barrier are excluded from training (their realised R is still
    measured during evaluation).
    """

    enabled: bool = False
    timeframe: str = "M5"
    # A calibrated probability of reaching the target before the stop. The
    # model is fitted without class re-weighting, so 0.50 is the natural
    # decision boundary and 0.60 means what it says.
    confidence_min: float = 0.50
    model_path: str = "models/signal_filter.json"
    data_path: str = "data/ml/XAUUSD_M5.csv"
    horizon_bars: int = 24          # Max bars a signal is given to reach SL/TP
    embargo_bars: int = 24          # Bars dropped between train and test folds
    folds: int = 5                  # Walk-forward folds
    min_train_signals: int = 200    # Refuse to fit a fold with fewer labels
    l2: float = 1.0                 # Ridge penalty on the logistic weights
    learning_rate: float = 0.15
    epochs: int = 600
    monitor_days: int = 30          # Window for live prediction monitoring

    def validate(self):
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"ml.timeframe must be one of {', '.join(TIMEFRAMES)}")
        if not 0 <= self.confidence_min <= 1:
            raise ValueError("ml.confidence_min must be between 0 and 1")
        if self.horizon_bars < 1:
            raise ValueError("ml.horizon_bars must be at least 1")
        if self.embargo_bars < 0:
            raise ValueError("ml.embargo_bars cannot be negative")
        if self.folds < 2:
            raise ValueError("ml.folds must be at least 2")
        if self.min_train_signals < 50:
            raise ValueError("ml.min_train_signals must be at least 50")
        if self.l2 < 0:
            raise ValueError("ml.l2 cannot be negative")
        if self.learning_rate <= 0:
            raise ValueError("ml.learning_rate must be positive")
        if self.epochs < 1:
            raise ValueError("ml.epochs must be at least 1")
        if self.monitor_days < 1:
            raise ValueError("ml.monitor_days must be at least 1")


# ── Root ───────────────────────────────────────────────────────────


def _plain(value):
    """Convert dataclass field values into JSON-safe primitives."""
    if isinstance(value, time):
        return f"{value:%H:%M}"
    return value


@dataclass(frozen=True)
class Config:
    """Every tunable in one object, with a snapshot for the dashboard."""

    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    mt5: MT5Config = MT5Config()
    breakout: BreakoutConfig = BreakoutConfig()
    scalper: ScalperConfig = ScalperConfig()
    ml: MLConfig = MLConfig()

    def validate(self):
        for name in ("risk", "execution", "mt5", "breakout", "scalper", "ml"):
            getattr(self, name).validate()
        return self

    def snapshot(self):
        """A JSON-safe view of exactly the values the engine trades with."""
        breakout = {key: _plain(value) for key, value in asdict(self.breakout).items()}
        breakout["session"] = self.breakout.session
        breakout["asian_session"] = self.breakout.asian_session
        scalper = {key: _plain(value) for key, value in asdict(self.scalper).items()}
        scalper["session"] = self.scalper.session
        return {
            "risk": asdict(self.risk),
            "execution": asdict(self.execution),
            "mt5": asdict(self.mt5),
            "breakout": breakout,
            "scalper": scalper,
            "ml": asdict(self.ml),
        }


CONFIG = Config().validate()


# Backwards-compatible names for scripts that import the module directly
# (``backtest_final.py`` uses ``from config.settings import *``). Every one of
# these is derived from CONFIG above, so there is still a single definition.
ACCOUNT_BALANCE = CONFIG.risk.starting_balance
RISK_PER_TRADE = CONFIG.risk.risk_per_trade
MAX_DAILY_RISK = CONFIG.risk.max_daily_loss_pct
MAX_DAILY_TRADES = CONFIG.risk.max_daily_trades
MAX_CONCURRENT_TRADES = CONFIG.risk.max_open_positions
MAX_CONSECUTIVE_LOSSES = CONFIG.risk.max_consecutive_losses
SAME_DIRECTION_GUARD_USD = CONFIG.risk.same_direction_guard_usd

LOOP_SECONDS = CONFIG.execution.loop_seconds
MAX_SIGNAL_AGE_SECONDS = CONFIG.execution.max_signal_age_seconds
HISTORY_SCAN_SECONDS = CONFIG.execution.history_scan_seconds
POSITION_SYNC_SECONDS = CONFIG.execution.position_sync_seconds
MAX_SPREAD_POINTS = CONFIG.execution.max_spread_points

MAGIC = CONFIG.mt5.magic
ORDER_DEVIATION_POINTS = CONFIG.mt5.deviation_points
ORDER_RETRY_ATTEMPTS = CONFIG.mt5.retry_attempts
MT5_RECONNECT_SECONDS = CONFIG.mt5.reconnect_seconds
MT5_MAX_BACKOFF_SECONDS = CONFIG.mt5.max_backoff_seconds

BREAKOUT_ENABLED = CONFIG.breakout.enabled
BREAKOUT_SYMBOL = CONFIG.breakout.symbol
SCALPER_ENABLED = CONFIG.scalper.enabled
SCALPER_SYMBOL = CONFIG.scalper.symbol
