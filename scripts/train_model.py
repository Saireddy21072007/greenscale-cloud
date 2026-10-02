#!/usr/bin/env python
"""Train the runtime / utilisation predictor and print its accuracy.

    python scripts/train_model.py [--samples 4000] [--seed 11]

Writes models/predictor.joblib. Until this is run, the app falls back to the
closed-form heuristic and says so in the UI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greenscale.predictor import HeuristicPredictor, LearnedPredictor, MODEL_PATH  # noqa: E402
from greenscale.simulator import build_training_set  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--test-split", type=float, default=0.25)
    args = parser.parse_args()

    tasks, runtimes, utils = build_training_set(n=args.samples, seed=args.seed)
    cut = int(len(tasks) * (1 - args.test_split))
    train = (tasks[:cut], runtimes[:cut], utils[:cut])
    test = (tasks[cut:], runtimes[cut:], utils[cut:])

    print(f"Training on {len(train[0])} jobs, holding out {len(test[0])}...")
    model = LearnedPredictor.train(*train, random_state=args.seed)

    scores = model.evaluate(*test)
    print("\nHeld-out performance")
    print(f"  runtime      MAE {scores['runtime_mae_hours']:.4f} h   R2 {scores['runtime_r2']:.4f}")
    print(f"  utilisation  MAE {scores['utilisation_mae']:.4f}     R2 {scores['utilisation_r2']:.4f}")

    # The heuristic is the thing we have to beat to justify shipping a model at
    # all. If this margin is ever negative, delete the model and keep the formula.
    heuristic = HeuristicPredictor()
    err = sum(abs(heuristic.predict(t).runtime_hours - y) for t, y in zip(test[0], test[1]))
    baseline_mae = err / len(test[0])
    improvement = 100 * (baseline_mae - scores["runtime_mae_hours"]) / baseline_mae
    print(f"\n  heuristic baseline runtime MAE {baseline_mae:.4f} h")
    print(f"  learned model is {improvement:.1f}% better")

    path = model.save(MODEL_PATH)
    print(f"\nSaved -> {path}")

    metrics_path = path.parent / "metrics.json"
    metrics_path.write_text(
        json.dumps(
            {**scores, "heuristic_runtime_mae_hours": baseline_mae,
             "improvement_pct": improvement, "seed": args.seed},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Metrics -> {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
