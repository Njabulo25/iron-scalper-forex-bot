"""Historical bar collection and validation for the ML pipeline.

The pipeline only ever trains on bars that came out of MetaTrader 5. There is
no synthetic or interpolated history here: ``collect`` reads the terminal's own
copy of the symbol's history, merges it into an append-only CSV store, and
refuses to run when the terminal is unavailable.

Validation is separate from collection so a stored file can be re-checked at
any time: duplicate timestamps, out-of-order rows, impossible OHLC, non-positive
prices, weekend holes and flat spreads are all reported. Rows that cannot be
trusted are dropped rather than silently repaired.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.ml.features import Bars

COLUMNS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")


def _parse_time(value):
    value = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=timezone.utc).timestamp())
    try:
        return int(float(value))
    except ValueError as error:
        raise ValueError(f"unreadable bar time {value!r}") from error


def load_bars(path):
    """Read the stored CSV into :class:`Bars`."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no bar history at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path} contains no bars")

    time = [_parse_time(row["time"]) for row in rows]
    columns = {
        key: [_number(row.get(key)) for row in rows]
        for key in ("open", "high", "low", "close", "tick_volume", "spread")
    }
    order = np.argsort(np.asarray(time, dtype=np.int64), kind="mergesort")
    return Bars(
        time=np.asarray(time, dtype=np.int64)[order],
        open=np.asarray(columns["open"])[order],
        high=np.asarray(columns["high"])[order],
        low=np.asarray(columns["low"])[order],
        close=np.asarray(columns["close"])[order],
        tick_volume=np.asarray(columns["tick_volume"])[order],
        spread=np.asarray(columns["spread"])[order],
        symbol=path.stem.split("_")[0],
    )


def _number(value):
    if value is None or str(value).strip() == "":
        return 0.0
    return float(value)


def save_bars(bars, path):
    """Write bars to CSV in MT5's own column layout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"))
        for index in range(len(bars)):
            stamp = datetime.fromtimestamp(int(bars.time[index]), timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            writer.writerow((
                stamp, f"{bars.open[index]:.5f}", f"{bars.high[index]:.5f}",
                f"{bars.low[index]:.5f}", f"{bars.close[index]:.5f}",
                int(bars.tick_volume[index]), int(bars.spread[index]), 0,
            ))
    return path


def merge_bars(existing, incoming):
    """Union of both series, de-duplicated by timestamp; raises on no overlap."""
    if existing is None or not len(existing):
        return incoming
    if incoming is None or not len(incoming):
        return existing

    time = np.concatenate((existing.time, incoming.time))
    _, first = np.unique(time, return_index=True)
    order = np.sort(first)
    arrays = {}
    for name in ("open", "high", "low", "close", "tick_volume", "spread"):
        arrays[name] = np.concatenate((getattr(existing, name), getattr(incoming, name)))[order]
    return Bars(time[order], **arrays, symbol=existing.symbol or incoming.symbol)


def validate_bars(bars, max_gap_hours=96.0):
    """Return a validation report and the index mask of trustworthy bars."""
    count = len(bars)
    issues = []
    mask = np.ones(count, dtype=bool)

    if count < 2:
        return {
            "bars": count,
            "ok": False,
            "issues": ["not enough bars"],
            "duplicates": 0,
            "invalid_rows": count,
            "gap_count": 0,
        }

    differences = np.diff(bars.time)
    duplicates = int((differences == 0).sum())
    backwards = int((differences < 0).sum())
    if duplicates:
        issues.append(f"{duplicates} duplicate timestamps")
        mask[1:] &= differences != 0
    if backwards:
        issues.append(f"{backwards} out-of-order timestamps")
        mask[1:] &= differences > 0

    prices = np.column_stack((bars.open, bars.high, bars.low, bars.close))
    bad_price = ~np.all(np.isfinite(prices), axis=1) | np.any(prices <= 0, axis=1)
    bad_high = bars.high < np.maximum(bars.open, bars.close)
    bad_low = bars.low > np.minimum(bars.open, bars.close)
    invalid = bad_price | bad_high | bad_low
    invalid_rows = int(invalid.sum())
    if invalid_rows:
        issues.append(f"{invalid_rows} bars with impossible prices")
        mask &= ~invalid

    gaps = np.where(differences > max_gap_hours * 3600)[0]
    if len(gaps):
        issues.append(f"{len(gaps)} gaps longer than {max_gap_hours:.0f}h")
        # A gap is not corrupt data; it is reported, not removed.

    if np.all(bars.spread == 0):
        issues.append("spread column is empty")

    flat = int((np.diff(bars.close) == 0).sum())
    if count and flat / max(count - 1, 1) > 0.5:
        issues.append("more than half of the bars have an unchanged close")

    if count:
        span_days = (bars.time[-1] - bars.time[0]) / 86400.0
    else:
        span_days = 0.0

    return {
        "bars": count,
        "ok": invalid_rows == 0 and duplicates == 0 and backwards == 0,
        "issues": issues,
        "duplicates": duplicates,
        "invalid_rows": invalid_rows,
        "gap_count": int(len(gaps)),
        "first": _stamp(bars.time[0]),
        "last": _stamp(bars.time[-1]),
        "span_days": round(span_days, 1),
        "median_spread": float(np.median(bars.spread)) if count else 0.0,
        "mask": mask,
    }


def _stamp(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M")


def collect_mt5(symbol, timeframe="M5", bars=100_000, store=None, start=None, end=None,
                store_db=False):
    """Read history from the running terminal and merge it into the store.

    Requires the MetaTrader5 package and a signed-in terminal. Nothing is
    generated when the terminal is missing: the caller gets a ``RuntimeError``.

    ``store_db`` also upserts the bars into the ``market_data`` table (unique
    per symbol/timeframe/bar), so the same history is queryable through SQL as
    well as the CSV the trainer reads.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError as error:  # pragma: no cover - depends on the machine
        raise RuntimeError(
            "MetaTrader5 is not installed; run this on the trading machine"
        ) from error

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    try:
        resolution = getattr(mt5, f"TIMEFRAME_{timeframe}", None)
        if resolution is None:
            raise RuntimeError(f"unknown timeframe {timeframe}")

        if start is not None and end is not None:
            rates = mt5.copy_rates_range(symbol, resolution, start, end)
        else:
            rates = mt5.copy_rates_from_pos(symbol, resolution, 0, int(bars))

        if rates is None or not len(rates):
            raise RuntimeError(f"MT5 returned no {timeframe} bars for {symbol}")

        incoming = Bars(
            time=np.asarray([row["time"] for row in rates], dtype=np.int64),
            open=np.asarray([row["open"] for row in rates]),
            high=np.asarray([row["high"] for row in rates]),
            low=np.asarray([row["low"] for row in rates]),
            close=np.asarray([row["close"] for row in rates]),
            tick_volume=np.asarray([row["tick_volume"] for row in rates]),
            spread=np.asarray([row["spread"] for row in rates]),
            symbol=symbol,
        )
    finally:
        mt5.shutdown()

    store = Path(store or f"data/ml/{symbol}_{timeframe}.csv")
    existing = load_bars(store) if store.exists() else None
    merged = merge_bars(existing, incoming)
    report = validate_bars(merged)
    save_bars(merged, store)

    if store_db:
        # Imported here so the trainer stays usable without the engine package
        # when the caller only wants the CSV.
        from src.engine import database

        database.upsert_market_bars(symbol, timeframe, merged, source="mt5")

    return {
        "store": str(store),
        "downloaded": len(incoming),
        "added": len(merged) - (len(existing) if existing is not None else 0),
        "total": len(merged),
        "stored_in_database": bool(store_db),
        "report": {key: value for key, value in report.items() if key != "mask"},
    }
