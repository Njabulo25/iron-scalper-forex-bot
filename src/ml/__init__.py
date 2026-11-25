"""Machine-learning pipeline for Iron Scalper.

Modules, and what each one owns:

    history     MT5 bar collection, storage and validation
    features    causal per-bar market features (no look-ahead)
    signals     numpy port of the two strategies' entry rules
    labels      stop/target label generation from those signals
    model       logistic-regression model with JSON persistence
    pipeline    dataset building, walk-forward validation, model versions
    monitor     live prediction logging and realised performance
    gate        the live confidence gate the engine consults

The package depends on numpy and the standard library only. This machine has
no pandas and no scikit-learn, so the model is a small ridge-regularised
logistic regression trained in numpy; that keeps the pipeline runnable and
auditable instead of depending on a package that cannot be installed here.
"""
