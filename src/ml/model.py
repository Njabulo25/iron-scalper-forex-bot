"""A small ridge-regularised logistic regression, persisted as JSON.

The project runs on Python 3.14 with neither scikit-learn nor pandas installed,
so the model is written here in numpy: about a hundred lines that are easy to
audit, deterministic, and store as plain JSON instead of a pickle. A pickle of
a third-party estimator would also be a poor thing to load into a trading
process.

The model predicts the probability that a signal reaches its target before its
stop. It never decides position size or direction; the engine's risk manager
and broker gateway remain the only path to an order.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def sigmoid(values):
    clipped = np.clip(values, -60.0, 60.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def roc_auc(labels, scores):
    """Rank-based AUC with average ranks for ties."""
    labels = np.asarray(labels, dtype=np.int64)
    scores = np.asarray(scores, dtype=np.float64)
    positives = int(labels.sum())
    negatives = int(len(labels) - positives)
    if not positives or not negatives:
        return float("nan")

    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=np.float64)
    sorted_scores = scores[order]
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and sorted_scores[end + 1] == sorted_scores[position]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        ranks[order[position:end + 1]] = average
        position = end + 1
    rank_sum = ranks[labels == 1].sum()
    return float((rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives))


def classification_metrics(labels, scores, threshold=0.5):
    """Accuracy, precision, recall, F1, Brier and AUC at one threshold."""
    labels = np.asarray(labels, dtype=np.int64)
    scores = np.asarray(scores, dtype=np.float64)
    predicted = (scores >= threshold).astype(np.int64)

    true_positive = int(((predicted == 1) & (labels == 1)).sum())
    false_positive = int(((predicted == 1) & (labels == 0)).sum())
    false_negative = int(((predicted == 0) & (labels == 1)).sum())
    true_negative = int(((predicted == 0) & (labels == 0)).sum())
    total = len(labels)

    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (true_positive + true_negative) / total if total else 0.0

    return {
        "samples": total,
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "auc": round(roc_auc(labels, scores), 4) if total else 0.0,
        "brier": round(float(np.mean((scores - labels) ** 2)), 4) if total else 0.0,
        "base_rate": round(float(labels.mean()), 4) if total else 0.0,
        "predicted_positive_rate": round(float(predicted.mean()), 4) if total else 0.0,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
    }


class LogisticModel:
    """Binary logistic filter over named, standardised features."""

    KIND = "iron-scalper-logistic"

    def __init__(self, feature_names, weights=None, bias=0.0, mean=None, scale=None,
                 threshold=0.5, metadata=None):
        self.feature_names = list(feature_names)
        width = len(self.feature_names)
        self.weights = np.zeros(width) if weights is None else np.asarray(weights, dtype=np.float64)
        self.bias = float(bias)
        self.mean = np.zeros(width) if mean is None else np.asarray(mean, dtype=np.float64)
        self.scale = np.ones(width) if scale is None else np.asarray(scale, dtype=np.float64)
        self.threshold = float(threshold)
        self.metadata = dict(metadata or {})
        if len(self.weights) != width or len(self.mean) != width or len(self.scale) != width:
            raise ValueError("weights, mean and scale must match the feature count")

    # ── fitting ────────────────────────────────────────────────────

    def fit(self, matrix, labels, l2=1.0, learning_rate=0.15, epochs=600, balance=True):
        """Full-batch gradient descent on the (optionally balanced) log-loss."""
        matrix = np.asarray(matrix, dtype=np.float64)
        labels = np.asarray(labels, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != len(self.feature_names):
            raise ValueError("training matrix does not match the feature names")
        if len(labels) != len(matrix):
            raise ValueError("labels and features disagree on the sample count")
        if labels.min() == labels.max():
            raise ValueError("training data contains a single class")

        self.mean = matrix.mean(axis=0)
        deviation = matrix.std(axis=0)
        self.scale = np.where(deviation > 1e-9, deviation, 1.0)
        standardised = (matrix - self.mean) / self.scale

        positives = float(labels.sum())
        negatives = float(len(labels) - positives)
        if balance:
            # Balanced class weights: a 38% win rate must not bias the fit.
            weight_positive = 0.5 * len(labels) / positives
            weight_negative = 0.5 * len(labels) / negatives
        else:
            weight_positive = weight_negative = 1.0
        sample_weights = np.where(labels > 0.5, weight_positive, weight_negative)

        weights = np.zeros(len(self.feature_names))
        bias = 0.0
        for _ in range(int(epochs)):
            probabilities = sigmoid(standardised @ weights + bias)
            residual = (probabilities - labels) * sample_weights
            gradient = standardised.T @ residual / len(labels) + l2 * weights / len(labels)
            weights -= learning_rate * gradient
            bias -= learning_rate * float(residual.mean())

        self.weights = weights
        self.bias = bias
        return self

    # ── inference ──────────────────────────────────────────────────

    def predict_proba(self, matrix):
        matrix = np.asarray(matrix, dtype=np.float64)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape[1] != len(self.feature_names):
            raise ValueError("prediction matrix does not match the feature names")
        standardised = (matrix - self.mean) / self.scale
        return sigmoid(standardised @ self.weights + self.bias)

    def predict_one(self, values):
        """Probability for one mapping of feature name -> value."""
        row = np.array([float(values[name]) for name in self.feature_names])
        if not np.all(np.isfinite(row)):
            return float("nan")
        return float(self.predict_proba(row)[0])

    def contributions(self, values):
        """Per-feature contribution to the log-odds, for explanation."""
        row = np.array([float(values[name]) for name in self.feature_names])
        standardised = (row - self.mean) / self.scale
        products = standardised * self.weights
        return {name: round(float(value), 4) for name, value in zip(self.feature_names, products)}

    # ── persistence ────────────────────────────────────────────────

    def to_dict(self):
        return {
            "kind": self.KIND,
            "feature_names": self.feature_names,
            "weights": [float(value) for value in self.weights],
            "bias": float(self.bias),
            "mean": [float(value) for value in self.mean],
            "scale": [float(value) for value in self.scale],
            "threshold": float(self.threshold),
            "metadata": self.metadata,
        }

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload):
        if payload.get("kind") != cls.KIND:
            raise ValueError(f"not a {cls.KIND} model")
        return cls(
            feature_names=payload["feature_names"],
            weights=payload["weights"],
            bias=payload["bias"],
            mean=payload["mean"],
            scale=payload["scale"],
            threshold=payload.get("threshold", 0.5),
            metadata=payload.get("metadata") or {},
        )

    @classmethod
    def load(cls, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)
