"""Live model monitoring from the database.

Every gated signal is written to ``ml_predictions`` with the probability, the
decision and the feature values that produced them. When a trade that came
through the gate closes, its R multiple is attached to that row. This module
turns those rows into the numbers that matter for trading:

* how accurate the live predictions have been (precision/recall/F1 against
  realised outcomes, not against the model's own training labels),
* the realised win rate and expectancy of taken versus skipped signals,
* the same split by confidence band, strategy and session hour.

Skipped signals have no trade and therefore no realised outcome; they are
reported as a count, never as if they had been traded.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from src.engine.database import _connect, _ensure_ready


def rows(days=30):
    """Predictions from the last ``days`` days, oldest first."""
    # A database created before this feature existed has no table yet.
    _ensure_ready()
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            SELECT signal_key, model_version, strategy, signal_type, signal_time,
                   probability, threshold, decision, features, trade_ticket,
                   outcome_r, outcome_usd, outcome_result
            FROM ml_predictions
            WHERE created_at >= ?
            ORDER BY signal_time
            """,
            (cutoff,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _binary_metrics(labels, predicted):
    labels = np.asarray(labels, dtype=np.int64)
    predicted = np.asarray(predicted, dtype=np.int64)
    tp = int(((predicted == 1) & (labels == 1)).sum())
    fp = int(((predicted == 1) & (labels == 0)).sum())
    fn = int(((predicted == 0) & (labels == 1)).sum())
    tn = int(((predicted == 0) & (labels == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    total = len(labels)
    return {
        "samples": total,
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _economics(outcomes):
    outcomes = np.asarray(outcomes, dtype=np.float64)
    if not len(outcomes):
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = int((outcomes > 0).sum())
    return {
        "trades": len(outcomes),
        "wins": wins,
        "win_rate": round(wins / len(outcomes), 4),
        "avg_r": round(float(outcomes.mean()), 4),
        "total_r": round(float(outcomes.sum()), 3),
    }


def summary(days=30):
    """Everything the dashboard and the CLI need to judge the live model."""
    predictions = rows(days)
    resolved = [row for row in predictions if row["outcome_r"] is not None]

    taken = [row for row in predictions if row["decision"] == "taken"]
    skipped = [row for row in predictions if row["decision"] == "skipped"]
    decided_with_outcome = [
        row for row in resolved if row["outcome_result"] in ("WIN", "LOSS")
    ]

    bands = [
        (0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01),
    ]
    by_band = []
    for low, high in bands:
        group = [row for row in resolved if low <= row["probability"] < high]
        by_band.append({
            "band": f"{low:.1f}-{min(high, 1.0):.1f}",
            **_economics([row["outcome_r"] for row in group]),
        })

    by_strategy = {}
    for name in sorted({row["strategy"] for row in predictions}):
        group = [row for row in resolved if row["strategy"] == name]
        by_strategy[name] = _economics([row["outcome_r"] for row in group])

    hours = defaultdict(list)
    for row in resolved:
        try:
            hour = int(str(row["signal_time"])[11:13])
        except (ValueError, TypeError):
            continue
        hours[hour].append(row["outcome_r"])
    by_hour = {
        str(hour): _economics(values) for hour, values in sorted(hours.items())
    }

    labels = [1 if row["outcome_r"] > 0 else 0 for row in resolved]
    predicted = [1 if row["probability"] >= row["threshold"] else 0 for row in resolved]

    versions = defaultdict(int)
    for row in predictions:
        versions[row["model_version"]] += 1

    return {
        "window_days": int(days),
        "predictions": len(predictions),
        "taken": len(taken),
        "skipped": len(skipped),
        "resolved": len(resolved),
        "model_versions": dict(versions),
        "mean_probability": round(
            float(np.mean([row["probability"] for row in predictions])), 4
        ) if predictions else 0.0,
        "live_classification": _binary_metrics(labels, predicted),
        "taken_economics": _economics([row["outcome_r"] for row in taken if row["outcome_r"] is not None]),
        "all_resolved_economics": _economics([row["outcome_r"] for row in resolved]),
        "by_confidence": by_band,
        "by_strategy": by_strategy,
        "by_hour": by_hour,
        "settled_trades": len(decided_with_outcome),
    }


def latest_model(registry_path="models/registry.json"):
    """The newest saved version's metadata, for display when the engine is off."""
    try:
        payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    versions = payload.get("versions") or []
    return versions[0] if versions else {}
