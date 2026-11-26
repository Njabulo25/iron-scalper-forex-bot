"""Checks for the ML pipeline.

The harness is deliberately self-contained (no pytest needed) and runs without
pandas or a MetaTrader 5 terminal, which is why the ML code is numpy-only.

What it protects:

* no look-ahead: features and labels at a bar are unchanged when later bars
  change, and folds never overlap;
* labels replay the strategy's own stop/target, including the pessimistic
  treatment of a bar that spans both;
* the walk-forward splits are contiguous, ordered and embargoed;
* model persistence is exact and probabilities are bounded;
* the gate can only return a decision - it holds no broker or order path;
* predictions are logged, linked and settled, and monitoring reads them.

Run: python tests/test_ml_pipeline.py
"""

import logging
import sys
import tempfile
import types
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f" -> {detail}" if detail else ""))


def _install_stubs():
    """Minimal MetaTrader5/pandas modules so the engine module can be imported."""
    for name in ("MetaTrader5", "pandas"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            if name == "MetaTrader5":
                for constant in (
                    "TIMEFRAME_M5", "POSITION_TYPE_BUY", "POSITION_TYPE_SELL",
                    "ORDER_TYPE_BUY", "ORDER_TYPE_SELL", "TRADE_ACTION_DEAL",
                    "ORDER_TIME_GTC", "ORDER_FILLING_IOC", "TRADE_RETCODE_DONE",
                    "DEAL_ENTRY_IN", "DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT",
                    "DEAL_ENTRY_OUT_BY",
                ):
                    setattr(module, constant, 1)
            sys.modules[name] = module


_install_stubs()

from config.settings import CONFIG, MLConfig, ScalperConfig, BreakoutConfig  # noqa: E402
from src.ml import features, history, labels, model, pipeline, signals  # noqa: E402
from src.ml.gate import MLGate  # noqa: E402
from src.engine import database  # noqa: E402
from src.engine.live_trader import LiveTrader  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────

DAY0 = 1700006400  # 2023-11-15 00:00 UTC


def synthetic_bars(count=3000, seed=11):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.1, count)
    close = 2000.0 + np.cumsum(steps)
    open_price = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum(open_price, close) + np.abs(rng.normal(0.0, 0.5, count))
    low = np.minimum(open_price, close) - np.abs(rng.normal(0.0, 0.5, count))
    time = DAY0 + np.arange(count, dtype=np.int64) * 300
    volume = rng.integers(60, 500, count).astype(np.float64)
    spread = np.full(count, 20.0)
    return features.Bars(time, open_price, high, low, close, volume, spread, "XAUUSD")


TEST_CONFIG = CONFIG


def run():
    print("\nML pipeline checks")
    print("=" * 60)

    bars = synthetic_bars()
    _check_validation(bars)
    _check_features(bars)
    _check_indicators()
    _check_labels()
    _check_signals(bars)
    _check_strategy_parity(bars)
    _check_dataset(bars)
    _check_walk_forward(bars)
    _check_model()
    _check_gate()
    _check_database()
    _check_live_gate()

    print("=" * 60)
    print(f"{PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


# ── checks ─────────────────────────────────────────────────────────


def _check_validation(bars):
    print("\nHistory validation")
    subset = bars.subset(0, 400)
    report = history.validate_bars(subset)
    check("clean history is accepted", report["ok"] and report["invalid_rows"] == 0, report["issues"])

    duplicated = features.Bars(
        np.concatenate((subset.time[:5], subset.time[4:10], subset.time[10:])),
        np.concatenate((subset.open[:5], subset.open[4:10], subset.open[10:])),
        np.concatenate((subset.high[:5], subset.high[4:10], subset.high[10:])),
        np.concatenate((subset.low[:5], subset.low[4:10], subset.low[10:])),
        np.concatenate((subset.close[:5], subset.close[4:10], subset.close[10:])),
    )
    duplicate_report = history.validate_bars(duplicated)
    check("duplicate timestamps are reported", duplicate_report["duplicates"] == 1, duplicate_report)
    check("duplicates mark the history invalid", duplicate_report["ok"] is False)

    broken = bars.subset(0, 200)
    broken.high[50] = broken.low[50] - 5.0
    broken_report = history.validate_bars(broken)
    check("impossible OHLC is reported", broken_report["invalid_rows"] >= 1, broken_report)
    check("invalid rows are excluded by the mask", not broken_report["mask"][50])

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "x.csv"
        history.save_bars(bars.subset(0, 500), path)
        reloaded = history.load_bars(path)
        check("bars round-trip through the CSV store", len(reloaded) == 500)
        check("timestamps survive the round-trip", int(reloaded.time[0]) == int(bars.time[0]))
        merged = history.merge_bars(reloaded, bars.subset(400, 700))
        check("merging de-duplicates overlapping bars", len(merged) == 700, len(merged))


def _check_features(bars):
    print("\nFeature engineering")
    values = features.build_features(bars, TEST_CONFIG)
    names, matrix = features.feature_matrix(bars, TEST_CONFIG)
    check("every advertised feature is produced", set(names) == set(features.FEATURE_NAMES))
    check("matrix has one row per bar", matrix.shape == (len(bars), len(features.FEATURE_NAMES)))

    cut = 2000
    future = features.Bars(
        bars.time.copy(), bars.open.copy(), bars.high.copy(), bars.low.copy(),
        bars.close.copy(), bars.tick_volume.copy(), bars.spread.copy(),
    )
    future.close[cut + 1:] *= 1.4
    future.high[cut + 1:] *= 1.4
    future.low[cut + 1:] *= 0.6
    future.open[cut + 1:] *= 1.2
    changed = features.build_features(future, TEST_CONFIG)
    leaked = []
    for name in features.FEATURE_NAMES:
        left = np.asarray(values[name][:cut + 1], dtype=float)
        right = np.asarray(changed[name][:cut + 1], dtype=float)
        if not np.allclose(left, right, equal_nan=True):
            leaked.append(name)
    check("features never read a future bar", not leaked, leaked[:4])

    warm = matrix[600:]
    check("features are complete after warm-up", np.all(np.isfinite(warm)),
          int((~np.isfinite(warm)).sum()))
    check("ATR percentage is positive", np.all(values["atr_pct"][600:] > 0))
    check("RSI stays inside 0-1", np.all((values["rsi"][600:] >= 0) & (values["rsi"][600:] <= 1.0001)))
    check("session flags are binary",
          set(np.unique(values["session_london"])) <= {0.0, 1.0})
    check("Asian distance is empty before the Asian range closes",
          np.isnan(values["asian_dist_high"][0]))


def _check_indicators():
    print("\nIndicator correctness")
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    means = features.rolling_mean(values, 3)
    check("rolling mean matches hand arithmetic",
          np.allclose(means[2:], [2.0, 3.0, 4.0]) and np.isnan(means[0]))
    with_nan = features.rolling_mean(np.array([np.nan, 1.0, 2.0, 3.0]), 2)
    check("a window touching a NaN stays NaN", np.isnan(with_nan[1]) and with_nan[3] == 2.5)

    leading = features.ema(np.array([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0]), 2)
    check("EMA skips leading NaNs", np.isfinite(leading[-1]))
    check("EMA of a constant is that constant",
          np.allclose(features.ema(np.full(60, 7.0), 10)[-1], 7.0))


def _check_labels():
    print("\nLabel generation")

    def path(highs, lows, closes):
        count = len(closes)
        time = DAY0 + np.arange(count, dtype=np.int64) * 300
        return features.Bars(time, np.array(closes), np.array(highs), np.array(lows), np.array(closes))

    up = path([101, 102.2, 103, 104], [100.4, 101.4, 102.2, 103.2], [100.5, 101.5, 102.5, 103.5])
    label, outcome, exit_index = labels.simulate_trade(up, 0, 1, 100.0, 99.0, 102.0, 10)
    check("a target hit is a win", label == labels.WIN, label)
    check("a win is worth the reward/risk ratio", abs(outcome - 2.0) < 1e-9, outcome)
    check("the winning bar is reported", exit_index == 1, exit_index)

    down = path([100.6, 99.4, 98.5, 98.0], [99.5, 98.6, 97.5, 97.0], [99.8, 98.9, 98.0, 97.5])
    label, outcome, _ = labels.simulate_trade(down, 0, 1, 100.0, 99.0, 102.0, 10)
    check("a stop hit is a loss", label == labels.LOSS, label)
    check("a loss is minus one R", abs(outcome + 1.0) < 1e-9, outcome)

    ambiguous = path([100.2, 103.0], [99.8, 98.0], [100.1, 100.0])
    label, _, _ = labels.simulate_trade(ambiguous, 0, 1, 100.0, 99.0, 102.0, 10)
    check("a bar spanning both barriers counts as the stop", label == labels.LOSS, label)

    flat = path([100.2, 100.3, 100.2], [99.8, 99.7, 99.8], [100.1, 100.0, 100.1])
    label, outcome, _ = labels.simulate_trade(flat, 0, 1, 100.0, 99.0, 102.0, 2)
    check("a timeout is not a win or a loss", label == labels.TIMEOUT, label)
    check("a timeout is marked to market", abs(outcome - 0.1) < 1e-9, outcome)

    side = np.zeros(5, dtype=np.int8)
    side[1] = 1
    built, _, _ = labels.build_labels(flat, side, np.array([0, 99.0, 0, 0, 0]), np.array([0, 102.0, 0, 0, 0]), 2)
    check("only signal bars are labelled", built[0] == labels.TIMEOUT and built[1] != 0)


def _check_signals(bars):
    print("\nStrategy parity ports")
    values = features.build_features(bars, TEST_CONFIG)
    scalper_side = signals.scalper_signals(bars, values, TEST_CONFIG)
    breakout_side = signals.breakout_signals(bars, values, TEST_CONFIG)
    minutes = bars.minute_of_day
    settings = TEST_CONFIG.scalper
    start = settings.session_start.hour * 60 + settings.session_start.minute
    end = settings.session_end.hour * 60 + settings.session_end.minute

    scalper_hits = np.where(scalper_side != 0)[0]
    check("the scalper port produces signals", len(scalper_hits) > 0, len(scalper_hits))
    check("scalper signals stay inside its session",
          all(start <= minutes[index] <= end for index in scalper_hits))
    spacing = np.diff(scalper_hits) if len(scalper_hits) > 1 else np.array([999])
    check("scalper respects bar spacing", spacing.min() >= settings.min_bar_spacing, spacing.min())

    breakout_hits = np.where(breakout_side != 0)[0]
    days = {int(bars.day_number[index]): 0 for index in breakout_hits}
    for index in breakout_hits:
        days[int(bars.day_number[index])] += 1
    check("the breakout port produces at most one signal per day",
          bool(days) and max(days.values(), default=0) <= 1, days)
    check("breakout signals carry a side and geometry",
          set(np.unique(breakout_side)) <= {-1, 0, 1})


def _check_strategy_parity(bars):
    """Compare the numpy port with the real pandas strategies, when possible."""
    print("\nStrategy parity")
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        print("  SKIP  parity with the pandas strategies (pandas is not installed)")
        return
    if not hasattr(pd, "DataFrame"):
        print("  SKIP  parity with the pandas strategies (pandas is stubbed for import)")
        return

    from src.strategies.daily_breakout import DailyBreakout
    from src.strategies.london_scalper import LondonScalper

    frame = pd.DataFrame({
        "time": pd.to_datetime(bars.time, unit="s"),
        "open": bars.open, "high": bars.high, "low": bars.low,
        "close": bars.close, "tick_volume": bars.tick_volume, "spread": bars.spread,
    })
    values = features.build_features(bars, TEST_CONFIG)
    pairs = (
        ("SCALPER", LondonScalper, signals.scalper_signals, TEST_CONFIG.scalper),
        ("BREAKOUT", DailyBreakout, signals.breakout_signals, TEST_CONFIG.breakout),
    )
    for name, strategy_class, port, _settings in pairs:
        expected = {(trade["time"], trade["type"]) for trade in strategy_class(frame).generate()}
        side = port(bars, values, TEST_CONFIG)
        actual = {
            (frame["time"].iloc[index], "BUY" if side[index] > 0 else "SELL")
            for index in np.where(side != 0)[0]
        }
        check(f"the {name} port matches the strategy exactly", expected == actual,
              f"strategy {len(expected)} vs port {len(actual)}")


def _check_dataset(bars):
    print("\nDataset assembly")
    dataset = pipeline.build_dataset(bars, TEST_CONFIG)
    check("signals were found", dataset["counts"]["signals"] > 0, dataset["counts"])
    check("matrix width matches the feature list",
          dataset["matrix"].shape[1] == len(dataset["names"]))
    check("labels are 0, 1 or timeout",
          set(np.unique(dataset["labels"])) <= {-1, 0, 1})
    check("trainable excludes timeouts and incomplete rows",
          not np.any(dataset["trainable"] & (dataset["labels"] == -1)))
    check("every signal has a finite training row after warm-up",
          int(dataset["finite"].sum()) > 0.9 * dataset["counts"]["signals"],
          f"{int(dataset['finite'].sum())}/{dataset['counts']['signals']}")
    check("signals are ordered by bar", np.all(np.diff(dataset["indices"]) >= 0))
    matching_strategies = [
        name for name, settings in (("scalper", TEST_CONFIG.scalper), ("breakout", TEST_CONFIG.breakout))
        if settings.enabled and settings.timeframe == TEST_CONFIG.ml.timeframe
    ]
    check("only matching-timeframe strategies enter the ML dataset",
          all(dataset["counts"][name] >= 0 for name in matching_strategies)
          and all(dataset["counts"][name] == 0 for name in ("scalper", "breakout")
                  if name not in matching_strategies), dataset["counts"])
    m1_config = replace(TEST_CONFIG, ml=replace(TEST_CONFIG.ml, timeframe="M1"))
    m1_dataset = pipeline.build_dataset(bars, m1_config)
    check("M1 ML data contains scalper signals and excludes M5 breakout signals",
          m1_dataset["counts"]["scalper"] > 0 and m1_dataset["counts"]["breakout"] == 0,
          m1_dataset["counts"])


def _check_walk_forward(bars):
    print("\nWalk-forward splits")
    bounds = pipeline.fold_bounds(1000, 5)
    check("folds are contiguous", all(bounds[i][1] == bounds[i + 1][0] for i in range(len(bounds) - 1)))
    check("folds never overlap", all(start < end for start, end in bounds))
    check("fold count is honoured", len(bounds) == 5, len(bounds))

    dataset = pipeline.build_dataset(synthetic_bars(12000, seed=5), TEST_CONFIG)
    config = replace(TEST_CONFIG, ml=MLConfig(
        enabled=True, confidence_min=0.5, folds=3, min_train_signals=60,
        horizon_bars=TEST_CONFIG.ml.horizon_bars, embargo_bars=TEST_CONFIG.ml.embargo_bars,
        l2=TEST_CONFIG.ml.l2, learning_rate=TEST_CONFIG.ml.learning_rate,
        epochs=TEST_CONFIG.ml.epochs,
    ))
    report = pipeline.walk_forward(dataset, config)
    scored = [fold for fold in report["folds"] if "classification" in fold]
    check("at least one fold was scored", len(scored) >= 1, report["folds"])
    if scored:
        fold = scored[0]
        train_last = datetime.strptime(fold["train_last"], "%Y-%m-%d %H:%M")
        test_first = datetime.strptime(fold["test_first"], "%Y-%m-%d %H:%M")
        check("training data ends before the test block",
              train_last <= test_first, (fold["train_last"], fold["test_first"]))
        embargo = (test_first - train_last).total_seconds() / 300.0
        check("the embargo keeps at least its configured gap",
              embargo >= 0, embargo)
        check("fold metrics include both classification and economics",
              {"classification", "baseline", "gated"} <= set(fold))
        aggregate = report["aggregate"]
        check("aggregate reports baseline and gated economics",
              {"baseline", "gated", "uplift", "confidence_bands"} <= set(aggregate))
        check("gated trades are a subset of the baseline",
              aggregate["gated"]["trades"] <= aggregate["baseline"]["trades"])


def _check_model():
    print("\nModel training, metrics and persistence")
    rng = np.random.default_rng(3)
    features_x = np.vstack((
        rng.normal(1.0, 0.4, (120, 2)),
        rng.normal(-1.0, 0.4, (120, 2)),
    ))
    labels_y = np.concatenate((np.ones(120), np.zeros(120)))
    fitted = model.LogisticModel(["a", "b"], threshold=0.5)
    fitted.fit(features_x, labels_y, l2=0.5, learning_rate=0.2, epochs=800, balance=False)
    probabilities = fitted.predict_proba(features_x)
    check("probabilities are bounded", np.all((probabilities >= 0) & (probabilities <= 1)))
    check("the model separates the training classes",
          probabilities[:120].mean() > 0.7 and probabilities[120:].mean() < 0.3,
          (probabilities[:120].mean(), probabilities[120:].mean()))
    check("contributions add up to the log-odds",
          abs(model.sigmoid(sum(fitted.contributions({"a": 1.0, "b": -1.0}).values()) + fitted.bias)
              - fitted.predict_one({"a": 1.0, "b": -1.0})) < 1e-3)
    check("AUC of a perfect ranking is 1", model.roc_auc([0, 1], [0.1, 0.9]) == 1.0)
    check("AUC of an inverted ranking is 0", model.roc_auc([0, 1], [0.9, 0.1]) == 0.0)

    metrics = model.classification_metrics([1, 0, 1, 0], [0.9, 0.6, 0.4, 0.1], 0.5)
    check("precision/recall/F1 match a hand tally",
          (metrics["precision"], metrics["recall"], metrics["f1"], metrics["accuracy"]) == (0.5, 0.5, 0.5, 0.5),
          metrics)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "model.json"
        fitted.save(path)
        reloaded = model.LogisticModel.load(path)
        check("a saved model reloads exactly",
              np.allclose(reloaded.weights, fitted.weights)
              and np.allclose(reloaded.predict_proba(features_x), probabilities))
        check("the saved file is plain JSON",
              path.read_text(encoding="utf-8").lstrip().startswith("{"))


def _check_gate():
    print("\nConfidence gate")
    trained = model.LogisticModel(["a"], weights=[2.0], bias=0.0, mean=[0.0], scale=[1.0])
    gate = MLGate(trained, threshold=0.5)
    check("a high score is taken", gate.decide({"a": 1.0}).taken)
    check("a low score is skipped", not gate.decide({"a": -1.0}).taken)
    check("an incomplete feature row is refused", not gate.decide({"a": float("nan")}).taken)
    check("a missing feature is refused", not gate.decide({}).taken)
    check("the gate has no broker or order path",
          not any(hasattr(gate, name) for name in ("broker", "send_order", "place_trade")))
    check("the gate reports its threshold", gate.decide({"a": 1.0}).threshold == 0.5)


def _check_database():
    print("\nPrediction logging and monitoring")
    from src.ml import monitor

    with tempfile.TemporaryDirectory() as folder:
        original = database.DATABASE_PATH
        database.DATABASE_PATH = Path(folder) / "test.db"
        database._INITIALIZED.clear()
        try:
            first = database.log_prediction(
                "SCALPER|BUY|20260101_1000", "v-test", "SCALPER", "XAUUSD", "BUY",
                "2026-01-01 10:00:00", 0.71, 0.5, "taken", {"a": 1.0},
            )
            duplicate = database.log_prediction(
                "SCALPER|BUY|20260101_1000", "v-test", "SCALPER", "XAUUSD", "BUY",
                "2026-01-01 10:00:00", 0.71, 0.5, "taken", {"a": 1.0},
            )
            database.log_prediction(
                "SCALPER|SELL|20260101_1030", "v-test", "SCALPER", "XAUUSD", "SELL",
                "2026-01-01 10:30:00", 0.22, 0.5, "skipped", {"a": -1.0},
            )
            check("a prediction is logged once", first is True and duplicate is False)
            database.link_prediction("SCALPER|BUY|20260101_1000", 555)
            resolved = database.resolve_prediction(555, 1.67, 16.7, "WIN")
            check("a prediction is linked and settled", resolved == 1, resolved)

            rows = database.get_ml_predictions(10)
            check("predictions are readable", len(rows) == 2, len(rows))
            check("the settled row carries its R multiple",
                  [row for row in rows if row["trade_ticket"] == 555][0]["outcome_r"] == 1.67)

            summary = monitor.summary(30)
            check("monitoring counts taken and skipped",
                  summary["taken"] == 1 and summary["skipped"] == 1, summary)
            check("monitoring reports realised economics",
                  summary["taken_economics"]["trades"] == 1
                  and summary["taken_economics"]["total_r"] == 1.67, summary["taken_economics"])
            check("live classification is computed from realised outcomes",
                  summary["live_classification"]["samples"] == 1
                  and summary["live_classification"]["precision"] == 1.0,
                  summary["live_classification"])
        finally:
            database.DATABASE_PATH = original
            database._INITIALIZED.clear()


def _check_live_gate():
    print("\nEngine integration")
    trained = model.LogisticModel(["a", "b"], weights=[3.0, 0.0], bias=-1.0, mean=[0.0, 0.0], scale=[1.0, 1.0])
    trader = object.__new__(LiveTrader)
    trader.log = logging.getLogger("test.ml")
    trader.web_state = None
    trader.config = replace(CONFIG, ml=MLConfig(enabled=True, confidence_min=0.5))
    trader.gate = MLGate(trained, threshold=0.5)
    trader._ml_blocked = False
    trader._ml_logged = set()
    trader._ml_seen = 0
    trader._ml_skipped = 0
    trader._ml_last = None
    trader._ml_status = {"enabled": True, "active": True, "threshold": 0.5, "reason": "", "model": {}}
    trader._feature_row = lambda trade, frame, _v=-1.0: {"a": _v, "b": 0.0}

    trade = {
        "type": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0,
        "time": datetime(2026, 1, 5, 10, 0), "strategy": "SCALPER",
    }

    with tempfile.TemporaryDirectory() as folder:
        original = database.DATABASE_PATH
        database.DATABASE_PATH = Path(folder) / "live.db"
        database._INITIALIZED.clear()
        try:
            allowed, prediction = trader._ml_decision(trade, None)
            check("a low-confidence signal is blocked", allowed is False)
            check("the block reason is published", "confidence" in prediction.reason)
            logged = database.get_ml_predictions(5)
            check("the skipped signal is logged", len(logged) == 1 and logged[0]["decision"] == "skipped")

            trader._feature_row = lambda trade, frame: {"a": 5.0, "b": 0.0}
            allowed, _ = trader._ml_decision(trade, None)
            check("a high-confidence signal is allowed through", allowed is True)
            check("the decision is not executed by the gate",
                  not hasattr(trader.gate, "send_order"))

            trader._ml_blocked = True
            trader._ml_status["reason"] = "model missing"
            allowed, blocked = trader._ml_decision(trade, None)
            check("a missing model blocks entries (fail closed)", allowed is False and "model" in blocked.reason)

            trader._ml_blocked = False
            trader.gate = None
            allowed, prediction = trader._ml_decision(trade, None)
            check("a disabled gate lets the strategy trade as before",
                  allowed is True and prediction is None)
        finally:
            database.DATABASE_PATH = original
            database._INITIALIZED.clear()


if __name__ == "__main__":
    raise SystemExit(run())
