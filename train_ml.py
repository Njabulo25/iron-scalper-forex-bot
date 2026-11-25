"""Train, inspect and monitor the Iron Scalper signal filter.

    python train_ml.py --collect              # pull more M5 history from MT5
    python train_ml.py --train                # walk-forward + save a new version
    python train_ml.py --report               # stored out-of-sample results
    python train_ml.py --monitor --days 30    # live prediction performance

Nothing here is invented: ``--collect`` refuses to run without a signed-in
MetaTrader 5 terminal, ``--train`` refuses to save when walk-forward produced
no out-of-sample predictions, and ``--report`` prints exactly what the saved
model recorded.

The gate stays off in live trading until ``ml.enabled`` is set in
config/settings.py, which is a trading decision, not a build step.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import CONFIG
from src.ml import history, monitor, pipeline


def _print_report(report):
    aggregate = report.get("aggregate") or {}
    if not aggregate:
        print("No out-of-sample fold could be scored:", report.get("error", "unknown reason"))
        return

    classification = aggregate["classification"]
    baseline = aggregate["baseline"]
    gated = aggregate["gated"]
    uplift = aggregate["uplift"]

    print("\nOut-of-sample, walk-forward")
    print("-" * 62)
    print(f"  folds scored          {aggregate['folds_scored']}")
    print(f"  signals               {classification['samples']}")
    print(f"  accuracy              {classification['accuracy']:.3f}   "
          f"(base rate {classification['base_rate']:.3f})")
    print(f"  precision             {classification['precision']:.3f}")
    print(f"  recall                {classification['recall']:.3f}")
    print(f"  F1                    {classification['f1']:.3f}")
    print(f"  AUC                   {classification['auc']:.3f}")
    print(f"  Brier                 {classification['brier']:.3f}")

    print("\nTrading economics of the same out-of-sample signals")
    print("-" * 62)
    print(f"  {'':<22}{'baseline':>12}{'gated':>12}")
    print(f"  {'trades':<22}{baseline['trades']:>12}{gated['trades']:>12}")
    print(f"  {'win rate':<22}{baseline['win_rate']:>12.3f}{gated['win_rate']:>12.3f}")
    print(f"  {'expectancy (R)':<22}{baseline['avg_r']:>12.3f}{gated['avg_r']:>12.3f}")
    print(f"  {'total R':<22}{baseline['total_r']:>12.2f}{gated['total_r']:>12.2f}")
    print(f"  {'max drawdown (R)':<22}{baseline['max_drawdown_r']:>12.2f}{gated['max_drawdown_r']:>12.2f}")
    print(f"  take rate             {aggregate['take_rate']:.3f}")
    print(f"  expectancy change     {uplift['avg_r_delta']:+.4f} R per trade")

    print("\nBy predicted confidence")
    print("-" * 62)
    print(f"  {'band':<10}{'trades':>8}{'win rate':>10}{'avg R':>10}")
    for band in aggregate["confidence_bands"]:
        print(f"  {band['band']:<10}{band['trades']:>8}{band['win_rate']:>10.3f}{band['avg_r']:>10.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="download bars from MT5")
    parser.add_argument("--train", action="store_true", help="walk-forward and save a model")
    parser.add_argument("--report", action="store_true", help="show the saved model's results")
    parser.add_argument("--monitor", action="store_true", help="show live prediction performance")
    parser.add_argument("--data", default=CONFIG.ml.data_path, help="bar CSV to use")
    parser.add_argument("--bars", type=int, default=100_000, help="bars to request from MT5")
    parser.add_argument(
        "--store-db", action="store_true",
        help="also upsert collected bars into the market_data table",
    )
    parser.add_argument("--timeframe", default=CONFIG.scalper.timeframe)
    parser.add_argument("--days", type=int, default=CONFIG.ml.monitor_days)
    parser.add_argument("--out", default=CONFIG.ml.model_path)
    arguments = parser.parse_args()

    if not any((arguments.collect, arguments.train, arguments.report, arguments.monitor)):
        parser.print_help()
        return 0

    if arguments.collect:
        result = history.collect_mt5(
            CONFIG.mt5.symbol, arguments.timeframe, arguments.bars,
            store=arguments.data, store_db=arguments.store_db,
        )
        print(json.dumps(result, indent=2))

    if arguments.train:
        path = Path(arguments.data)
        if not path.exists():
            # Fall back to the history that was downloaded with the project.
            fallback = Path("data/raw/XAUUSD_M5.csv")
            if not fallback.exists():
                print(f"No bars at {path} and no fallback at {fallback}.")
                return 2
            print(f"{path} is absent; using the project's MT5 download at {fallback}")
            path = fallback

        bars = history.load_bars(path)
        validation = history.validate_bars(bars)
        print(
            f"{validation['bars']} bars, {validation['first']} -> {validation['last']}, "
            f"{validation['span_days']} days"
        )
        for issue in validation["issues"]:
            print(f"  warning: {issue}")
        if validation["invalid_rows"] or validation["duplicates"]:
            print("Refusing to train on an invalid history; re-collect with --collect.")
            return 2

        model, report, paths = pipeline.train_and_save(
            bars, CONFIG, store_path=arguments.out, source=str(path)
        )
        _print_report(report)
        print(f"\nSaved model {model.metadata['version']}")
        print(f"  model     {paths['model']}")
        print(f"  registry  {paths['registry']}")
        print(
            "\nThe gate is still disabled. To use it, set ml.enabled = True in "
            "config/settings.py after reviewing the numbers above."
        )

    if arguments.report:
        registry = pipeline.load_registry(Path(arguments.out).parent / "registry.json")
        versions = registry.get("versions") or []
        if not versions:
            print("No model versions saved yet; run --train.")
            return 1
        latest = versions[0]
        print(f"Latest model {latest['version']} (trained {latest['trained_at']})")
        print(f"  data: {latest['data']['bars']} bars, {latest['data']['first']} -> {latest['data']['last']}")
        print(f"  samples: {latest['samples']}")
        _print_report({"aggregate": latest.get("out_of_sample") or {}})
        print(f"\n  {len(versions)} version(s) in the registry")

    if arguments.monitor:
        payload = monitor.summary(arguments.days)
        print(json.dumps(payload, indent=2))
        if not payload["predictions"]:
            print(
                "\nNo live predictions yet. They appear once ml.enabled is on and "
                "the engine is running."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
