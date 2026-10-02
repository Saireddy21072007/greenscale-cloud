"""The "AI" half of the project: predicting how a job will behave before we run it.

The scheduler cannot compare regions until it knows two things about a job that
are not in the submission form:

  * how long it will run   (drives energy, cost, and how many grid hours it spans)
  * how hard it will push the CPU (drives the power draw)

We learn both with gradient-boosted regression trees over the historical trace in
`data/history.csv`. Two separate models, because runtime and utilisation have
very different error scales and one multi-output model made both worse when we
tried it.

Simplifying assumption, stated in the report: every region offers the same
instance family, so runtime does not depend on where the job lands. Dropping
that assumption means adding a per-region performance index to regions.csv and a
feature column here -- nothing else changes.

If no trained model is on disk the code falls back to `HeuristicPredictor`, which
is the closed-form formula we started with. That fallback is what keeps the app
bootable on a fresh clone before anyone has run the training script.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from .config import MODEL_DIR
from .workload import TASK_TYPES, TENANTS, Task

MODEL_PATH = MODEL_DIR / "predictor.joblib"

# Feature order matters and is asserted in the tests -- a silent reordering
# between training and serving is the classic way to ship a broken model.
FEATURE_NAMES: List[str] = [
    "vcpus",
    "log_vcpus",
    "memory_gb",
    "memory_per_vcpu",
    "input_size_gb",
    "log_input_size",
    # Engineered, not raw: the ratio is what decides whether the working set
    # fits in RAM. Handing the model input and memory separately made it hunt
    # for the boundary itself and cost us about 4 points of R^2.
    "input_to_memory_ratio",
    "is_spilling",
    "is_high_priority",
] + [f"type_{t}" for t in sorted(TASK_TYPES)] + [f"tenant_{t}" for t in TENANTS]


def feature_vector(task: Task) -> List[float]:
    types = sorted(TASK_TYPES)
    mem_per_vcpu = task.memory_gb / max(task.vcpus, 1)
    ratio = task.input_size_gb / max(task.memory_gb, 1.0)
    row = [
        float(task.vcpus),
        math.log1p(task.vcpus),
        float(task.memory_gb),
        mem_per_vcpu,
        float(task.input_size_gb),
        math.log1p(task.input_size_gb),
        ratio,
        1.0 if ratio > 1.0 else 0.0,
        1.0 if task.priority == "high" else 0.0,
    ]
    row.extend(1.0 if task.task_type == t else 0.0 for t in types)
    # Tenant identity carries real signal -- each customer's code has its own
    # efficiency. An unseen tenant simply gets an all-zero block and falls back
    # to the population average, which is the behaviour we want on day one of
    # onboarding.
    row.extend(1.0 if task.tenant == t else 0.0 for t in TENANTS)
    return row


@dataclass
class Prediction:
    runtime_hours: float
    utilisation: float
    source: str  # "model" or "heuristic" -- surfaced in the UI so it is honest


class HeuristicPredictor:
    """Closed-form baseline. No training, no dependencies, always available."""

    source = "heuristic"

    def predict(self, task: Task) -> Prediction:
        profile = task.profile
        # More input data means longer; more vCPUs means shorter, but with
        # diminishing returns (Amdahl's law, crudely).
        size_factor = (max(task.input_size_gb, 0.5) / 12.0) ** 0.60
        parallel_factor = (4.0 / max(task.vcpus, 1)) ** 0.45
        runtime = profile["base_hours"] * size_factor * parallel_factor
        runtime = float(min(max(runtime, 0.05), 24.0))
        return Prediction(runtime, float(profile["utilisation"]), self.source)


class LearnedPredictor:
    """Gradient-boosted trees over the historical trace."""

    source = "model"

    def __init__(self, runtime_model, utilisation_model):
        self._runtime = runtime_model
        self._utilisation = utilisation_model

    # -- training ----------------------------------------------------------
    @classmethod
    def train(
        cls,
        tasks: Sequence[Task],
        runtimes: Sequence[float],
        utilisations: Sequence[float],
        random_state: int = 7,
    ) -> "LearnedPredictor":
        from sklearn.ensemble import GradientBoostingRegressor

        X = [feature_vector(t) for t in tasks]
        runtime_model = GradientBoostingRegressor(
            n_estimators=220, max_depth=3, learning_rate=0.06,
            subsample=0.9, random_state=random_state,
        ).fit(X, list(runtimes))
        util_model = GradientBoostingRegressor(
            n_estimators=160, max_depth=3, learning_rate=0.08,
            subsample=0.9, random_state=random_state,
        ).fit(X, list(utilisations))
        return cls(runtime_model, util_model)

    def evaluate(
        self,
        tasks: Sequence[Task],
        runtimes: Sequence[float],
        utilisations: Sequence[float],
    ) -> dict:
        from sklearn.metrics import mean_absolute_error, r2_score

        X = [feature_vector(t) for t in tasks]
        rt_pred = self._runtime.predict(X)
        ut_pred = self._utilisation.predict(X)
        return {
            "runtime_mae_hours": float(mean_absolute_error(runtimes, rt_pred)),
            "runtime_r2": float(r2_score(runtimes, rt_pred)),
            "utilisation_mae": float(mean_absolute_error(utilisations, ut_pred)),
            "utilisation_r2": float(r2_score(utilisations, ut_pred)),
            "n_samples": len(tasks),
        }

    # -- serving -----------------------------------------------------------
    def predict(self, task: Task) -> Prediction:
        X = [feature_vector(task)]
        runtime = float(self._runtime.predict(X)[0])
        util = float(self._utilisation.predict(X)[0])
        # Trees can extrapolate into nonsense on out-of-distribution inputs, so
        # we clamp to physically sensible ranges before anyone bills on them.
        runtime = min(max(runtime, 0.05), 24.0)
        util = min(max(util, 0.05), 0.99)
        return Prediction(runtime, util, self.source)

    # -- persistence -------------------------------------------------------
    def save(self, path: Path = MODEL_PATH) -> Path:
        import joblib

        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"runtime": self._runtime, "utilisation": self._utilisation,
             "features": FEATURE_NAMES},
            path,
        )
        return path

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "LearnedPredictor":
        import joblib

        bundle = joblib.load(path)
        if bundle.get("features") != FEATURE_NAMES:
            raise RuntimeError(
                "Saved model was trained with a different feature layout. "
                "Re-run scripts/train_model.py."
            )
        return cls(bundle["runtime"], bundle["utilisation"])


def get_predictor(path: Path = MODEL_PATH):
    """Load the trained model if it exists, otherwise fall back gracefully."""
    if path.exists():
        try:
            return LearnedPredictor.load(path)
        except Exception:
            # A stale or corrupt model must never take the whole service down.
            pass
    return HeuristicPredictor()
