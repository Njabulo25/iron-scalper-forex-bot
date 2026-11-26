"""Dataset assembly, walk-forward validation and model versioning.

The dataset is built from the strategy's *own* signals on MT5 bars: for every
bar where the scalper or the breakout would have entered, the features at that
bar are paired with the label produced by replaying that trade's stop and
target. Timeouts never enter training.

Walk-forward is the only validation used, because a random split would leak:
adjacent 5-minute signals share most of their feature window and their outcome
windows overlap, so shuffling rows puts the same price path in train and test.
Each fold trains on everything up to a point, drops an embargo of bars around
the boundary, then scores the strictly later block. The reported numbers are
therefore always out-of-sample in time.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.ml.features import FEATURE_NAMES, SIGNAL_FEATURES, build_features
from src.ml.labels import TIMEOUT, build_labels
from src.ml.model import LogisticModel, classification_metrics
from src.ml.signals import breakout_signals, scalper_signals, signal_geometry

MODEL_FAMILY = "ml-v1"


# ── dataset ───────────────────────────────────────────────────────


def build_dataset(bars, config):
    """Return aligned features, labels and trade outcomes for both strategies."""
    if len(bars) < 300:
        raise ValueError("need at least 300 bars to build a dataset")

    features = build_features(bars, config)
    names = list(FEATURE_NAMES) + list(SIGNAL_FEATURES)

    samples = []
    for strategy, generator, settings in (
        ("SCALPER", scalper_signals, config.scalper),
        ("BREAKOUT", breakout_signals, config.breakout),
    ):
        if not settings.enabled:
            continue
        side = generator(bars, features, config)
        stop_loss, take_profit = signal_geometry(bars, side, config, strategy)
        labels, outcomes, exits = build_labels(
            bars, side, stop_loss, take_profit, config.ml.horizon_bars
        )
        for index in np.where(side != 0)[0]:
            samples.append({
                "index": int(index),
                "time": int(bars.time[index]),
                "strategy": strategy,
                "side": int(side[index]),
                "label": int(labels[index]),
                "outcome_r": float(outcomes[index]),
                "exit_index": int(exits[index]),
            })

    samples.sort(key=lambda item: (item["index"], item["strategy"]))
    if not samples:
        raise ValueError("the configured strategies produced no signals on this history")

    matrix = np.zeros((len(samples), len(names)), dtype=np.float64)
    for row, sample in enumerate(samples):
        index = sample["index"]
        for column, name in enumerate(FEATURE_NAMES):
            matrix[row, column] = features[name][index]
        matrix[row, -2] = 1.0 if sample["strategy"] == "SCALPER" else 0.0
        matrix[row, -1] = 1.0 if sample["side"] > 0 else 0.0

    labels = np.array([sample["label"] for sample in samples], dtype=np.int64)
    outcomes = np.array([sample["outcome_r"] for sample in samples], dtype=np.float64)
    indices = np.array([sample["index"] for sample in samples], dtype=np.int64)
    times = np.array([sample["time"] for sample in samples], dtype=np.int64)

    finite = np.all(np.isfinite(matrix), axis=1)
    trainable = finite & (labels != TIMEOUT)

    return {
        "names": names,
        "matrix": matrix,
        "labels": labels,
        "outcomes": outcomes,
        "indices": indices,
        "times": times,
        "strategies": np.array([sample["strategy"] for sample in samples]),
        "sides": np.array([sample["side"] for sample in samples], dtype=np.int64),
        "finite": finite,
        "trainable": trainable,
        "bars": len(bars),
        "counts": {
            "signals": len(samples),
            "wins": int(((labels == 1)).sum()),
            "losses": int((labels == 0).sum()),
            "timeouts": int((labels == TIMEOUT).sum()),
            "incomplete_features": int((~finite).sum()),
            "scalper": int(sum(1 for sample in samples if sample["strategy"] == "SCALPER")),
            "breakout": int(sum(1 for sample in samples if sample["strategy"] == "BREAKOUT")),
        },
    }


# ── trading metrics ───────────────────────────────────────────────


def trading_metrics(outcomes, labels):
    """Economics of a selected subset, not classification accuracy."""
    outcomes = np.asarray(outcomes, dtype=np.float64)
    labels = np.asarray(labels, dtype=np.int64)
    count = len(outcomes)
    if not count:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0, "avg_r": 0.0,
            "total_r": 0.0, "profit_factor": 0.0, "max_drawdown_r": 0.0,
        }

    wins = int((outcomes > 0).sum())
    equity = np.cumsum(outcomes)
    peak = np.maximum.accumulate(equity)
    drawdown = float(np.min(equity - peak)) if count else 0.0
    gains = float(outcomes[outcomes > 0].sum())
    losses = float(-outcomes[outcomes < 0].sum())

    return {
        "trades": count,
        "wins": wins,
        "win_rate": round(wins / count, 4),
        "avg_r": round(float(outcomes.mean()), 4),
        "total_r": round(float(equity[-1]), 3),
        "profit_factor": round(gains / losses, 3) if losses else float("inf"),
        "max_drawdown_r": round(drawdown, 3),
    }


def confidence_bands(probabilities, outcomes, labels, edges=(0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01)):
    """Win rate and expectancy per predicted-confidence band."""
    bands = []
    for low, high in zip(edges, edges[1:]):
        mask = (probabilities >= low) & (probabilities < high)
        if not mask.any():
            bands.append({
                "band": f"{low:.1f}-{min(high, 1.0):.1f}", "trades": 0,
                "win_rate": 0.0, "avg_r": 0.0,
            })
            continue
        metrics = trading_metrics(outcomes[mask], labels[mask])
        bands.append({
            "band": f"{low:.1f}-{min(high, 1.0):.1f}",
            "trades": metrics["trades"],
            "win_rate": metrics["win_rate"],
            "avg_r": metrics["avg_r"],
        })
    return bands


# ── walk-forward ──────────────────────────────────────────────────


def fold_bounds(count, folds):
    """Contiguous, non-overlapping test blocks covering the tail of the set."""
    if folds < 2:
        raise ValueError("walk-forward needs at least two folds")
    size = max(count // (folds + 1), 1)
    bounds = []
    for fold in range(folds):
        end = count - (folds - 1 - fold) * size
        start = end - size
        if start <= 0:
            continue
        bounds.append((start, end))
    return bounds


def walk_forward(dataset, config):
    """Train/predict fold by fold, then score the concatenated out-of-sample set."""
    labels = dataset["labels"]
    outcomes = dataset["outcomes"]
    matrix = dataset["matrix"]
    indices = dataset["indices"]
    threshold = config.ml.confidence_min

    folds = []
    all_probabilities = []
    all_rows = []
    for number, (start, end) in enumerate(fold_bounds(len(labels), config.ml.folds), start=1):
        test_first_bar = int(indices[start])
        train_mask = (indices <= test_first_bar - config.ml.embargo_bars) & dataset["trainable"]
        # Every finite test signal is scored; timeouts still count in the
        # trading economics even though they were never trained on.
        test_mask = np.zeros(len(labels), dtype=bool)
        test_mask[start:end] = True
        test_mask &= dataset["finite"]

        train_labels = labels[train_mask]
        if len(train_labels) < config.ml.min_train_signals:
            folds.append({
                "fold": number, "skipped": f"only {len(train_labels)} training signals",
                "test_signals": int(test_mask.sum()),
            })
            continue
        if len(np.unique(train_labels)) < 2:
            folds.append({
                "fold": number, "skipped": "training labels contain a single class",
                "test_signals": int(test_mask.sum()),
            })
            continue

        model = LogisticModel(dataset["names"], threshold=threshold)
        model.fit(
            matrix[train_mask], train_labels, balance=False,
            l2=config.ml.l2, learning_rate=config.ml.learning_rate, epochs=config.ml.epochs,
        )
        probabilities = model.predict_proba(matrix[test_mask])
        test_labels = labels[test_mask]
        test_outcomes = outcomes[test_mask]
        known = test_labels != TIMEOUT

        classification = classification_metrics(
            test_labels[known], probabilities[known], threshold
        )
        taken = probabilities >= threshold
        folds.append({
            "fold": number,
            "train_signals": int(train_mask.sum()),
            "test_signals": int(test_mask.sum()),
            "train_first": _stamp(dataset["times"][train_mask][0]) if train_mask.any() else "",
            "train_last": _stamp(dataset["times"][train_mask][-1]) if train_mask.any() else "",
            "test_first": _stamp(dataset["times"][start]),
            "test_last": _stamp(dataset["times"][end - 1]),
            "classification": classification,
            "baseline": trading_metrics(test_outcomes, test_labels),
            "gated": trading_metrics(test_outcomes[taken], test_labels[taken]),
            "take_rate": round(float(taken.mean()), 4),
        })
        all_probabilities.append(probabilities)
        all_rows.append((test_outcomes, test_labels, taken))

    report = {"folds": folds}
    scored = [fold for fold in folds if "classification" in fold]
    if not scored:
        report["error"] = "no fold had enough history to train"
        report["aggregate"] = {}
        return report

    probabilities = np.concatenate(all_probabilities)
    outcomes = np.concatenate([row[0] for row in all_rows])
    labels_oos = np.concatenate([row[1] for row in all_rows])
    taken = np.concatenate([row[2] for row in all_rows])
    known = labels_oos != TIMEOUT

    report["aggregate"] = {
        "classification": classification_metrics(
            labels_oos[known], probabilities[known], threshold
        ),
        "baseline": trading_metrics(outcomes, labels_oos),
        "gated": trading_metrics(outcomes[taken], labels_oos[taken]),
        "take_rate": round(float(taken.mean()), 4),
        "skipped": trading_metrics(outcomes[~taken], labels_oos[~taken]),
        "confidence_bands": confidence_bands(probabilities, outcomes, labels_oos),
        "folds_scored": len(scored),
    }
    report["aggregate"]["uplift"] = _uplift(
        report["aggregate"]["baseline"], report["aggregate"]["gated"]
    )
    return report


def _uplift(baseline, gated):
    """How the filter changed expectancy, and how much of the sample it kept."""
    return {
        "avg_r_delta": round(gated["avg_r"] - baseline["avg_r"], 4),
        "win_rate_delta": round(gated["win_rate"] - baseline["win_rate"], 4),
        "total_r_delta": round(gated["total_r"] - baseline["total_r"], 3),
        "trades_kept": gated["trades"],
        "trades_baseline": baseline["trades"],
    }


def _stamp(epoch_seconds):
    return datetime.fromtimestamp(int(epoch_seconds), timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── versioning ────────────────────────────────────────────────────


def fingerprint(bars):
    digest = hashlib.sha256()
    for name in ("time", "open", "high", "low", "close"):
        digest.update(np.ascontiguousarray(getattr(bars, name)).tobytes())
    return digest.hexdigest()[:16]


def version_name(data_fingerprint):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{MODEL_FAMILY}-{stamp}-{data_fingerprint}"


def train_and_save(bars, config, store_path=None, source=None, timeframe=None):
    """Build the dataset, run walk-forward, fit on all history, save one version."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dataset = build_dataset(bars, config)
    report = walk_forward(dataset, config)
    if report.get("aggregate", {}).get("classification", {}).get("samples", 0) == 0:
        raise RuntimeError(
            "walk-forward produced no out-of-sample predictions; "
            "collect more history or lower ml.min_train_signals"
        )

    trainable = dataset["trainable"]
    model = LogisticModel(dataset["names"], threshold=config.ml.confidence_min)
    model.fit(
        dataset["matrix"][trainable], dataset["labels"][trainable], balance=False,
        l2=config.ml.l2, learning_rate=config.ml.learning_rate, epochs=config.ml.epochs,
    )

    data_fingerprint = fingerprint(bars)
    version = version_name(data_fingerprint)
    aggregate = report["aggregate"]
    model.metadata = {
        "version": version,
        "family": MODEL_FAMILY,
        "timeframe": timeframe or config.ml.timeframe,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {
            "source": source or "data/raw history",
            "fingerprint": data_fingerprint,
            "bars": len(bars),
            "first": datetime.fromtimestamp(int(bars.time[0]), timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(int(bars.time[-1]), timezone.utc).isoformat(),
        },
        "samples": {
            "signals": dataset["counts"]["signals"],
            "trainable": int(trainable.sum()),
            "wins": dataset["counts"]["wins"],
            "losses": dataset["counts"]["losses"],
            "timeouts": dataset["counts"]["timeouts"],
        },
        "out_of_sample": aggregate,
        "threshold": config.ml.confidence_min,
        "horizon_bars": config.ml.horizon_bars,
        "folds": config.ml.folds,
        "l2": config.ml.l2,
        "note": (
            "Logistic filter over causal features. Labels replay the strategy's "
            "own stop/target; timeouts excluded from training. Out-of-sample "
            "metrics come from contiguous walk-forward folds with an embargo."
        ),
    }

    store = Path(store_path or config.ml.model_path)
    model.save(store)
    registry_path = store.parent / "registry.json"
    entry = {
        "version": version,
        "path": str(store).replace("\\", "/"),
        "trained_at": model.metadata["trained_at"],
        "data": model.metadata["data"],
        "samples": model.metadata["samples"],
        "out_of_sample": aggregate,
        "threshold": config.ml.confidence_min,
    }
    _update_registry(registry_path, entry)

    # Queryable history of the model and the run that produced it. The JSON
    # file stays the artefact the engine loads; the database is the archive.
    try:
        from src.engine import database

        database.record_model_training(
            version=version,
            family=MODEL_FAMILY,
            path=str(store).replace("\\", "/"),
            feature_names=dataset["names"],
            threshold=config.ml.confidence_min,
            data=model.metadata["data"],
            samples=model.metadata["samples"],
            out_of_sample=aggregate,
            run={
                "status": "completed",
                "folds": config.ml.folds,
                "embargo_bars": config.ml.embargo_bars,
                "horizon_bars": config.ml.horizon_bars,
                "l2": config.ml.l2,
                "epochs": config.ml.epochs,
                "learning_rate": config.ml.learning_rate,
                "signals": dataset["counts"]["signals"],
                "trainable": int(trainable.sum()),
                "wins": dataset["counts"]["wins"],
                "losses": dataset["counts"]["losses"],
                "timeouts": dataset["counts"]["timeouts"],
                "metrics": {"aggregate": aggregate, "folds": report["folds"]},
                "started_at": started_at,
                "finished_at": None,
            },
            active=True,
        )
    except Exception as error:
        # Training must not fail because the database is unavailable; the
        # registry file has already been written.
        import logging

        logging.getLogger("iron.ml").warning(
            "Model saved but not recorded in the database: %s", error
        )

    return model, report, {"model": str(store), "registry": str(registry_path)}


def _update_registry(path, entry, limit=20):
    versions = []
    if path.exists():
        try:
            versions = json.loads(path.read_text(encoding="utf-8")).get("versions", [])
        except Exception:
            versions = []
    versions = [item for item in versions if item.get("version") != entry["version"]]
    versions.insert(0, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"versions": versions[:limit]}, indent=2), encoding="utf-8"
    )


def load_registry(path):
    path = Path(path)
    if not path.exists():
        return {"versions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"versions": []}
