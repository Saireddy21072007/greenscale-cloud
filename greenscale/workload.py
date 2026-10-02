"""Workload model: what a "task" is, and how we generate a realistic trace.

We do not have access to a production cluster trace, so `generate_trace` draws
jobs from distributions loosely shaped after the public Google cluster trace:
most jobs are small and short, a long tail is large and long, and the arrival
rate follows an office-hours pattern. This is stated plainly in the report --
the scheduler and the predictor both work unchanged on a real trace, only the
source of the rows changes.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# The four workload classes we model. The multipliers say how CPU-hungry the
# class is (mean utilisation while running) and how long it typically runs.
TASK_TYPES: Dict[str, Dict[str, float]] = {
    "web":       {"utilisation": 0.35, "base_hours": 0.6, "io_factor": 0.9},
    "batch":     {"utilisation": 0.78, "base_hours": 2.4, "io_factor": 1.4},
    "ml_train":  {"utilisation": 0.92, "base_hours": 4.1, "io_factor": 2.2},
    "analytics": {"utilisation": 0.64, "base_hours": 1.7, "io_factor": 1.8},
}

# Delay tolerance by class, in hours. A web front-end cannot be deferred at all;
# an ML training job usually can. This is what makes temporal shifting possible.
DEFAULT_DEFERRAL: Dict[str, int] = {
    "web": 0,
    "batch": 8,
    "ml_train": 12,
    "analytics": 6,
}


@dataclass
class Task:
    """One unit of work submitted to the scheduler."""

    task_id: str
    tenant: str
    task_type: str
    vcpus: int
    memory_gb: float
    input_size_gb: float
    submitted_hour: int                 # UTC hour of day, 0-23
    max_deferral_hours: int = 0         # 0 => must start immediately
    latency_budget_ms: float = 250.0
    residency_zone: Optional[str] = None  # e.g. "IN" forces an Indian region
    priority: str = "normal"            # normal | high
    metadata: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def new(**kwargs) -> "Task":
        kwargs.setdefault("task_id", f"tsk-{uuid.uuid4().hex[:10]}")
        return Task(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def profile(self) -> Dict[str, float]:
        return TASK_TYPES[self.task_type]

    def validate(self) -> None:
        if self.task_type not in TASK_TYPES:
            raise ValueError(
                f"unknown task_type {self.task_type!r}; expected one of {sorted(TASK_TYPES)}"
            )
        if self.vcpus < 1:
            raise ValueError("vcpus must be at least 1")
        if self.memory_gb <= 0:
            raise ValueError("memory_gb must be positive")
        if not 0 <= self.submitted_hour <= 23:
            raise ValueError("submitted_hour must be a UTC hour in 0..23")
        if self.max_deferral_hours < 0:
            raise ValueError("max_deferral_hours cannot be negative")


# ---------------------------------------------------------------------------
# Trace generation
# ---------------------------------------------------------------------------

# Relative arrival rate per UTC hour. Peak is late morning IST / European
# morning, which is when our hypothetical customers are actually working.
_ARRIVAL_SHAPE = [
    0.4, 0.3, 0.3, 0.3, 0.4, 0.6, 0.9, 1.2, 1.5, 1.7, 1.8, 1.7,
    1.5, 1.4, 1.4, 1.3, 1.2, 1.1, 1.0, 0.9, 0.8, 0.7, 0.6, 0.5,
]

TENANTS = ["acme-retail", "medi-clinic", "edtech-labs", "fintrust", "logi-move"]

# Every tenant's code is written by different people, so identical-looking jobs
# do not take identical time. A well-tuned Spark job and a naive one with the
# same vCPU count differ by a factor of two, and the scheduler can only learn
# that from history -- there is no formula for it. This is the main reason we
# use a learned predictor instead of the closed-form one.
TENANT_EFFICIENCY: Dict[str, float] = {
    "acme-retail": 1.00,
    "medi-clinic": 1.28,   # legacy PHP batch jobs, poorly parallelised
    "edtech-labs": 0.82,   # well-tuned PyTorch pipelines
    "fintrust": 1.11,
    "logi-move": 0.94,
}


def _lognormal(rng: random.Random, mean: float, sigma: float) -> float:
    """Long-tailed positive draw -- job sizes are never normally distributed."""
    return rng.lognormvariate(0.0, sigma) * mean


def generate_trace(n: int = 500, seed: int = 42) -> List[Task]:
    """Produce `n` tasks with a fixed seed so results are reproducible."""
    rng = random.Random(seed)
    hours = list(range(24))
    weights = _ARRIVAL_SHAPE

    tasks: List[Task] = []
    for _ in range(n):
        ttype = rng.choices(
            list(TASK_TYPES), weights=[0.42, 0.26, 0.14, 0.18], k=1
        )[0]
        vcpus = max(1, int(round(_lognormal(rng, 4, 0.7))))
        vcpus = min(vcpus, 64)
        memory_gb = round(vcpus * rng.uniform(1.8, 4.2), 1)
        input_size_gb = round(_lognormal(rng, 12 * TASK_TYPES[ttype]["io_factor"], 0.9), 2)
        hour = rng.choices(hours, weights=weights, k=1)[0]

        tasks.append(
            Task.new(
                tenant=rng.choice(TENANTS),
                task_type=ttype,
                vcpus=vcpus,
                memory_gb=memory_gb,
                input_size_gb=input_size_gb,
                submitted_hour=hour,
                max_deferral_hours=DEFAULT_DEFERRAL[ttype],
                latency_budget_ms=250.0 if ttype != "web" else 90.0,
                # Roughly one job in six carries an Indian data-residency
                # requirement -- our customers are Indian SMBs.
                residency_zone="IN" if rng.random() < 0.17 else None,
                priority="high" if rng.random() < 0.12 else "normal",
            )
        )
    return tasks
