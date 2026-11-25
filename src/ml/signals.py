"""Numpy port of the two strategies' entry rules, used to label training data.

Why a port exists: the strategy classes build a pandas DataFrame, and pandas is
not available here, so the training script cannot import them. The rules below
copy the decisions in ``src/strategies`` exactly (same periods, same session
windows, same spacing, same geometry) and read the same config object, so a
change to the strategy geometry reaches the labels through the config.

The live engine does not use this module: it gates the signals the real
strategy classes produced, which removes any chance of the port drifting from
what is traded. ``tests/test_ml_pipeline.py`` compares both implementations
when pandas is installed and reports that it skipped otherwise.
"""

import numpy as np

from src.ml.features import _minutes, ema, rolling_mean, true_range


def scalper_signals(bars, features, config):
    """Reproduce :class:`src.strategies.london_scalper.LondonScalper`."""
    settings = config.scalper
    count = len(bars)
    side = np.zeros(count, dtype=np.int8)          # 1 buy, -1 sell, 0 none
    atr = features["atr_pct"] / 100.0 * bars.close
    rsi = features["rsi"] * 100.0

    # The strategy's own EMA columns, rebuilt from the close.
    ema_fast = ema(bars.close, settings.ema_fast)
    ema_slow = ema(bars.close, settings.ema_slow)

    minutes = bars.minute_of_day
    start = max(settings.ema_slow, settings.atr_period) + 10
    last_index = -10
    open_price = bars.open
    close = bars.close

    for index in range(start, count):
        if index - last_index < settings.min_bar_spacing:
            continue
        minute = minutes[index]
        if not (_minutes(settings.session_start) <= minute <= _minutes(settings.session_end)):
            continue
        if not (np.isfinite(atr[index]) and atr[index] > 0):
            continue
        if not (np.isfinite(ema_fast[index]) and np.isfinite(rsi[index])):
            continue
        if abs(close[index] - ema_fast[index]) > settings.pullback_atr * atr[index]:
            continue

        body = close[index] - open_price[index]
        bullish = ema_fast[index] > ema_slow[index]
        bearish = ema_fast[index] < ema_slow[index]

        if bullish and body > 0 and settings.rsi_long_min < rsi[index] < settings.rsi_long_max:
            side[index] = 1
            last_index = index
        elif bearish and body < 0 and settings.rsi_short_min < rsi[index] < settings.rsi_short_max:
            side[index] = -1
            last_index = index
    return side


def day_ranges(day_number):
    """Map each broker day to its ``(start, end)`` index range, in one pass."""
    ranges = {}
    start = 0
    for index in range(1, len(day_number)):
        if day_number[index] != day_number[index - 1]:
            ranges[int(day_number[index - 1])] = (start, index)
            start = index
    if len(day_number):
        ranges[int(day_number[-1])] = (start, len(day_number))
    return ranges


def breakout_signals(bars, features, config):
    """Reproduce :class:`src.strategies.daily_breakout.DailyBreakout`."""
    settings = config.breakout
    count = len(bars)
    side = np.zeros(count, dtype=np.int8)
    atr = features["atr_pct"] / 100.0 * bars.close
    minutes = bars.minute_of_day
    asian_close = _minutes(settings.asian_end)
    session_start = _minutes(settings.session_start)
    session_end = _minutes(settings.session_end)

    # One pass over the days instead of scanning every bar for each bar.
    day_number = bars.day_number
    asian = {}
    for day, (start, end) in day_ranges(day_number).items():
        window = np.arange(start, end)
        window = window[minutes[window] < asian_close]
        if len(window) >= settings.min_asian_bars:
            asian[day] = (
                float(np.max(bars.high[window])), float(np.min(bars.low[window])),
            )

    done = set()
    for index in range(settings.atr_period + 16, count):
        day = int(day_number[index])
        if day in done or day not in asian:
            continue
        minute = minutes[index]
        if not (session_start <= minute <= session_end):
            continue

        asian_high, asian_low = asian[day]
        price_range = asian_high - asian_low
        if not (np.isfinite(atr[index]) and atr[index] > 0):
            continue
        if price_range < settings.min_range:
            continue

        close = bars.close[index]
        if close > asian_high and close - asian_high <= settings.atr_multiplier * atr[index]:
            side[index] = 1
            done.add(day)
        elif close < asian_low and asian_low - close <= settings.atr_multiplier * atr[index]:
            side[index] = -1
            done.add(day)
    return side


def signal_geometry(bars, side, config, strategy):  # noqa: C901 - mirrors the strategy
    """Stop and target for each signal, from the strategy's own rules."""
    count = len(bars)
    stop_loss = np.full(count, np.nan)
    take_profit = np.full(count, np.nan)

    if strategy == "SCALPER":
        settings = config.scalper
        atr = features_atr(bars, settings.atr_period)
        stop_loss = np.where(side == 1, bars.close - settings.sl_atr * atr,
                             bars.close + settings.sl_atr * atr)
        take_profit = np.where(side == 1, bars.close + settings.tp_atr * atr,
                               bars.close - settings.tp_atr * atr)
    else:
        settings = config.breakout
        stop_distance = np.full(count, np.nan)
        minutes = bars.minute_of_day
        day_number = bars.day_number
        ranges = day_ranges(day_number)
        asian_close = _minutes(settings.asian_end)
        for index in np.where(side != 0)[0]:
            span = ranges.get(int(day_number[index]))
            if span is None:
                continue
            window = np.arange(span[0], span[1])
            asian = window[minutes[window] < asian_close]
            if not len(asian):
                continue
            price_range = float(np.max(bars.high[asian]) - np.min(bars.low[asian]))
            stop_distance[index] = price_range * settings.sl_range_pct
        stop_loss = np.where(side == 1, bars.close - stop_distance, bars.close + stop_distance)
        take_profit = np.where(
            side == 1,
            bars.close + stop_distance * settings.tp_rr,
            bars.close - stop_distance * settings.tp_rr,
        )

    zero = side == 0
    stop_loss[zero] = np.nan
    take_profit[zero] = np.nan
    return stop_loss, take_profit


def features_atr(bars, period):
    """ATR in price units, matching the strategy's simple rolling mean."""
    return rolling_mean(true_range(bars), period)
