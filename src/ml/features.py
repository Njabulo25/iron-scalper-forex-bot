"""Causal market features for the signal-filter model.

Everything here is computed from a bar and the bars before it. Nothing reads a
future bar, which is the property that keeps the walk-forward numbers in
:mod:`src.ml.pipeline` honest: a feature at index *i* is reproducible from the
data that existed when the strategy produced its signal at *i*.

The inputs are plain numpy arrays (see :class:`Bars`), because pandas is not
installed on this machine and the same feature code must run inside the live
loop, the training script and the tests.
"""

from datetime import datetime, timezone

import numpy as np

BAR_KEYS = ("time", "open", "high", "low", "close", "tick_volume", "spread")

# Bar features, in a fixed order. ``strategy_scalper`` and ``side_buy`` are
# added per signal by the pipeline, so the model sees which rule fired.
FEATURE_NAMES = (
    "atr_pct",
    "atr_ratio",
    "rsi",
    "macd_hist",
    "ema_fast_gap",
    "ema_slow_gap",
    "ema_spread",
    "ema_slope",
    "mom1",
    "mom3",
    "mom12",
    "body",
    "upper_wick",
    "lower_wick",
    "bar_range",
    "gap",
    "volume_ratio",
    "spread_points",
    "hour_sin",
    "hour_cos",
    "session_london",
    "session_asia",
    "day_pos",
    "prev_day_range",
    "prev_day_close_pos",
    "asian_dist_high",
    "asian_dist_low",
    "adx",
)

SIGNAL_FEATURES = ("strategy_scalper", "side_buy")


class Bars:
    """OHLCV bars as numpy arrays, sorted by time.

    ``time`` is broker epoch seconds. ``tick_volume`` and ``spread`` default to
    zeros so a caller that only has candles can still build features.
    """

    __slots__ = ("time", "open", "high", "low", "close", "tick_volume", "spread", "symbol")

    def __init__(self, time, open, high, low, close, tick_volume=None, spread=None, symbol=""):
        self.time = _as_epoch(time)
        self.open = np.asarray(open, dtype=np.float64)
        self.high = np.asarray(high, dtype=np.float64)
        self.low = np.asarray(low, dtype=np.float64)
        self.close = np.asarray(close, dtype=np.float64)
        count = len(self.close)
        self.tick_volume = _column(tick_volume, count, 0.0)
        self.spread = _column(spread, count, 0.0)
        self.symbol = symbol
        if len(self.open) != count or len(self.high) != count or len(self.low) != count:
            raise ValueError("OHLC arrays must have the same length")

    def __len__(self):
        return len(self.close)

    @classmethod
    def from_columns(cls, columns, symbol=""):
        """Build from a mapping of column name -> sequence (pandas Series work)."""
        values = {}
        for key in BAR_KEYS:
            if key not in columns:
                continue
            values[key] = columns[key]
        if "time" not in values:
            raise ValueError("bars need a 'time' column")
        return cls(
            time=values["time"],
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            tick_volume=values.get("tick_volume"),
            spread=values.get("spread"),
            symbol=symbol,
        )

    def subset(self, start, end=None):
        end = len(self) if end is None else end
        return Bars(
            self.time[start:end], self.open[start:end], self.high[start:end],
            self.low[start:end], self.close[start:end],
            self.tick_volume[start:end], self.spread[start:end], self.symbol,
        )

    @property
    def minute_of_day(self):
        """Minute of the broker day, matching the strategy session comparisons."""
        seconds = np.mod(self.time, 86400)
        return (seconds // 60).astype(np.int64)

    @property
    def hour(self):
        return self.minute_of_day // 60

    @property
    def day_number(self):
        return self.time // 86400

    def timestamps(self):
        return [
            datetime.fromtimestamp(int(second), timezone.utc).replace(tzinfo=None)
            for second in self.time
        ]


def _as_epoch(values):
    """Epoch seconds from ints, floats or a pandas datetime64 column."""
    array = np.asarray(values)
    if np.issubdtype(array.dtype, np.datetime64):
        return array.astype("datetime64[s]").astype(np.int64)
    return array.astype(np.int64)


def _column(values, count, default):
    if values is None:
        return np.full(count, float(default))
    array = np.asarray(values, dtype=np.float64)
    if len(array) != count:
        raise ValueError("column length does not match the bar count")
    return array


# ── rolling helpers ────────────────────────────────────────────────


def rolling_mean(values, period):
    """Trailing mean with ``NaN`` until ``period`` finite observations exist.

    A window that touches a NaN stays NaN, which is what pandas' rolling mean
    does and therefore what the strategies' indicators do.
    """
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, np.nan)
    if period < 1 or len(values) < period:
        return out

    finite = np.isfinite(values)
    filled = np.where(finite, values, 0.0)
    totals = np.cumsum(np.insert(filled, 0, 0.0))
    counts = np.cumsum(np.insert(finite.astype(np.int64), 0, 0))
    window_totals = totals[period:] - totals[:-period]
    window_counts = counts[period:] - counts[:-period]

    means = np.full(len(window_totals), np.nan)
    complete = window_counts == period
    means[complete] = window_totals[complete] / period
    out[period - 1:] = means
    return out


def ema(values, span):
    """Exponential mean, seeded at the first finite run of ``span`` values.

    The seed skips leading NaNs so an EMA of a series that itself starts with
    NaNs (the MACD line) still produces a number instead of propagating them.
    """
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, np.nan)
    finite = np.flatnonzero(np.isfinite(values))
    if span < 1 or len(finite) < span:
        return out

    alpha = 2.0 / (span + 1.0)
    seed = int(finite[0])
    window = values[seed:seed + span]
    if not np.all(np.isfinite(window)):
        return out
    out[seed + span - 1] = float(np.mean(window))

    for index in range(seed + span, len(values)):
        previous = out[index - 1]
        if not np.isfinite(values[index]) or not np.isfinite(previous):
            continue
        out[index] = alpha * values[index] + (1.0 - alpha) * previous
    return out


def wilder_mean(values, period):
    """Wilder smoothing: simple mean seed, then recursive decay."""
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, np.nan)
    if len(values) < period or period < 1:
        return out
    out[period - 1] = np.nanmean(values[:period])
    for index in range(period, len(values)):
        out[index] = ((period - 1) * out[index - 1] + values[index]) / period
    return out


# ── indicators ─────────────────────────────────────────────────────


def true_range(bars):
    previous = np.concatenate(([np.nan], bars.close[:-1]))
    return np.maximum(
        bars.high - bars.low,
        np.maximum(np.abs(bars.high - previous), np.abs(bars.low - previous)),
    )


def rsi(close, period):
    delta = np.diff(close, prepend=np.nan)
    gain = rolling_mean(np.where(delta > 0, delta, 0.0), period)
    loss = rolling_mean(np.where(delta < 0, -delta, 0.0), period)
    with np.errstate(invalid="ignore", divide="ignore"):
        strength = gain / loss
    output = 100.0 - 100.0 / (1.0 + strength)
    output = np.where((loss == 0) & (gain > 0), 100.0, output)
    output = np.where((loss == 0) & (gain == 0), 50.0, output)
    return output


def macd(close, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def adx(bars, period=14):
    """Wilder's directional index, 0-100."""
    up = np.diff(bars.high, prepend=np.nan)
    down = -np.diff(bars.low, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = np.nan
    minus_dm[0] = np.nan

    average_range = wilder_mean(np.nan_to_num(true_range(bars)), period)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = 100.0 * wilder_mean(plus_dm, period) / average_range
        minus_di = 100.0 * wilder_mean(minus_dm, period) / average_range
        summed = plus_di + minus_di
        dx = np.where(summed > 0, 100.0 * np.abs(plus_di - minus_di) / summed, np.nan)
    return wilder_mean(dx, period)


# ── per-day context ────────────────────────────────────────────────


def _day_groups(day_number):
    """Return ``(start, end)`` index pairs, one per broker day."""
    groups = []
    start = 0
    for index in range(1, len(day_number)):
        if day_number[index] != day_number[index - 1]:
            groups.append((start, index))
            start = index
    if len(day_number):
        groups.append((start, len(day_number)))
    return groups


def _asian_extremes(bars, asian_end_minutes, minimum_bars):
    """Per-bar Asian range: this day's once complete, else the previous day's.

    Bars inside the Asian window cannot know their own day's range yet, so they
    carry the last completed range instead of a NaN. That keeps the feature
    defined for every bar (the live gate refuses incomplete rows) while staying
    causal.
    """
    high = np.full(len(bars), np.nan)
    low = np.full(len(bars), np.nan)
    minutes = bars.minute_of_day
    carried_high = np.nan
    carried_low = np.nan

    for start, end in _day_groups(bars.day_number):
        window = np.arange(start, end)
        inside = window[minutes[window] < asian_end_minutes]
        after = window[minutes[window] >= asian_end_minutes]

        if len(inside) >= minimum_bars:
            day_high = float(np.max(bars.high[inside]))
            day_low = float(np.min(bars.low[inside]))
        else:
            day_high, day_low = carried_high, carried_low

        high[inside] = carried_high
        low[inside] = carried_low
        high[after] = day_high
        low[after] = day_low
        carried_high, carried_low = day_high, day_low
    return high, low


def _previous_day(bars):
    """Previous day's high/low/close mapped onto this day's bars."""
    previous_high = np.full(len(bars), np.nan)
    previous_low = np.full(len(bars), np.nan)
    previous_close = np.full(len(bars), np.nan)

    groups = _day_groups(bars.day_number)
    for position in range(1, len(groups)):
        start, end = groups[position]
        _, prior_end = groups[position - 1]
        prior_start = groups[position - 1][0]
        previous_high[start:end] = np.max(bars.high[prior_start:prior_end])
        previous_low[start:end] = np.min(bars.low[prior_start:prior_end])
        previous_close[start:end] = bars.close[prior_end - 1]
    return previous_high, previous_low, previous_close


def _running_day_extremes(bars):
    """High/low from the start of the current day up to and including this bar."""
    day_high = np.full(len(bars), np.nan)
    day_low = np.full(len(bars), np.nan)
    for start, end in _day_groups(bars.day_number):
        day_high[start:end] = np.maximum.accumulate(bars.high[start:end])
        day_low[start:end] = np.minimum.accumulate(bars.low[start:end])
    return day_high, day_low


# ── feature assembly ───────────────────────────────────────────────


def build_features(bars, config=None):
    """Return ``{name: array}`` of causal features plus the per-session flags.

    ``config`` supplies the indicator periods and the Asian-range window so
    the features describe the strategy that is actually configured.
    """
    if len(bars) < 3:
        raise ValueError("need at least 3 bars to build features")

    scalper = config.scalper if config is not None else None
    breakout = config.breakout if config is not None else None
    atr_period = scalper.atr_period if scalper is not None else 14
    rsi_period = scalper.rsi_period if scalper is not None else 14
    fast = scalper.ema_fast if scalper is not None else 20
    slow = scalper.ema_slow if scalper is not None else 50

    close = bars.close
    high = bars.high
    low = bars.low
    open_price = bars.open

    average_range = rolling_mean(true_range(bars), atr_period)
    atr_long = rolling_mean(true_range(bars), max(slow, atr_period))
    safe_range = np.where(np.isfinite(average_range) & (average_range > 0), average_range, np.nan)

    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line, _, histogram = macd(close)
    minutes = bars.minute_of_day

    day_high, day_low = _running_day_extremes(bars)
    previous_high, previous_low, previous_close = _previous_day(bars)
    asian_end = (
        breakout.asian_start.hour * 60 + breakout.asian_start.minute
        if breakout is not None else 0
    )
    # The Asian window closes when the entry session starts.
    asian_close = (
        breakout.asian_end.hour * 60 + breakout.asian_end.minute
        if breakout is not None else 420
    )
    asian_high, asian_low = _asian_extremes(
        bars, asian_close,
        breakout.min_asian_bars if breakout is not None else 1,
    )
    del asian_end

    body = close - open_price
    span = np.maximum(high - low, 1e-9)
    previous_close_bar = np.concatenate(([np.nan], close[:-1]))
    volume_baseline = rolling_mean(bars.tick_volume, 20)
    spread_baseline = np.nanmedian(bars.spread) if np.any(bars.spread) else 0.0

    hours = bars.hour
    radians = 2.0 * np.pi * hours / 24.0

    features = {
        "atr_pct": safe_range / close * 100.0,
        "atr_ratio": safe_range / np.where(atr_long > 0, atr_long, np.nan) - 1.0,
        "rsi": rsi(close, rsi_period) / 100.0,
        "macd_hist": histogram / safe_range,
        "ema_fast_gap": (close - ema_fast) / safe_range,
        "ema_slow_gap": (close - ema_slow) / safe_range,
        "ema_spread": (ema_fast - ema_slow) / safe_range,
        "ema_slope": _shifted(ema_fast, 5, subtract=True) / safe_range,
        "mom1": (_momentum(close, 1)) / safe_range,
        "mom3": (_momentum(close, 3)) / safe_range,
        "mom12": (_momentum(close, 12)) / safe_range,
        "body": body / span,
        "upper_wick": (high - np.maximum(close, open_price)) / span,
        "lower_wick": (np.minimum(close, open_price) - low) / span,
        "bar_range": (high - low) / safe_range,
        "gap": (open_price - previous_close_bar) / safe_range,
        "volume_ratio": np.where(
            volume_baseline > 0, bars.tick_volume / volume_baseline - 1.0, 0.0
        ),
        "spread_points": bars.spread,
        "hour_sin": np.sin(radians),
        "hour_cos": np.cos(radians),
        "session_london": (
            (minutes >= _minutes(scalper.session_start)) & (minutes <= _minutes(scalper.session_end))
        ).astype(np.float64) if scalper is not None else np.zeros(len(bars)),
        "session_asia": (minutes < asian_close).astype(np.float64),
        "day_pos": _position_in_range(close, day_high, day_low),
        "prev_day_range": (previous_high - previous_low) / safe_range,
        "prev_day_close_pos": _position_in_range(previous_close, previous_high, previous_low),
        "asian_dist_high": (close - asian_high) / safe_range,
        "asian_dist_low": (close - asian_low) / safe_range,
        "adx": adx(bars, atr_period) / 100.0,
    }
    return features


def _minutes(value):
    return value.hour * 60 + value.minute


def _shifted(values, bars_back, subtract=False):
    shifted = np.full(len(values), np.nan)
    if len(values) > bars_back:
        shifted[bars_back:] = values[:-bars_back]
    return values - shifted if subtract else shifted


def _momentum(close, bars_back):
    shifted = _shifted(close, bars_back)
    return close - shifted


def _position_in_range(value, high, low):
    width = high - low
    with np.errstate(invalid="ignore", divide="ignore"):
        position = (value - low) / width
    return np.where(width > 0, position, np.nan)


def feature_matrix(bars, config=None):
    """Return ``(names, matrix)`` where matrix is ``len(bars) x len(names)``."""
    features = build_features(bars, config)
    names = FEATURE_NAMES
    matrix = np.column_stack([features[name] for name in names])
    return names, matrix
