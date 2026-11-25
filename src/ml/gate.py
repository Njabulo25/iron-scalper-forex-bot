"""The live confidence gate.

One job: given the feature row for a signal the strategy has already produced,
return a probability and whether it clears ``ml.confidence_min``. It holds no
broker, no risk manager and no order functions, so it cannot trade. The engine
calls it before ``place_trade``; a signal that clears the gate still goes
through spread checks, the risk manager, volume validation and MT5 exactly as
before.
"""

from dataclasses import dataclass

from src.ml.model import LogisticModel


@dataclass(frozen=True)
class Prediction:
    taken: bool
    probability: float
    threshold: float
    reason: str
    model_version: str


class MLGate:
    """Confidence filter around a persisted logistic model."""

    def __init__(self, model, threshold=None):
        if not isinstance(model, LogisticModel):
            raise TypeError("MLGate needs a LogisticModel")
        self.model = model
        self.threshold = float(threshold if threshold is not None else model.threshold)
        self.version = str((model.metadata or {}).get("version", "unknown"))

    @classmethod
    def load(cls, path, threshold=None):
        return cls(LogisticModel.load(path), threshold)

    def decide(self, values):
        """Return a :class:`Prediction`; never raises on a missing feature."""
        try:
            probability = self.model.predict_one(values)
        except (KeyError, TypeError, ValueError) as error:
            return Prediction(False, 0.0, self.threshold, f"features unavailable: {error}", self.version)

        if probability != probability:  # NaN
            return Prediction(
                False, 0.0, self.threshold,
                "features incomplete for this bar", self.version,
            )
        if probability >= self.threshold:
            return Prediction(
                True, round(probability, 4), self.threshold,
                f"confidence {probability:.2f} >= {self.threshold:.2f}", self.version,
            )
        return Prediction(
            False, round(probability, 4), self.threshold,
            f"confidence {probability:.2f} < {self.threshold:.2f}", self.version,
        )

    def summary(self):
        return {
            "version": self.version,
            "threshold": self.threshold,
            "features": list(self.model.feature_names),
            "trained_at": (self.model.metadata or {}).get("trained_at", ""),
            "data": (self.model.metadata or {}).get("data", {}),
            "samples": (self.model.metadata or {}).get("samples", {}),
            "out_of_sample": (self.model.metadata or {}).get("out_of_sample", {}),
        }
