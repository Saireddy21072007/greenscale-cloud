"""Experiment harness: run a whole trace through a policy and total up the result.

This is what produced every number in our report. `compare_policies` is the
function behind the results table -- it runs the identical trace through each
policy with the capacity counters reset in between, so the comparison is fair.

`observe` is the ground-truth generator used to build training data for the
predictor. It is deliberately *not* the same formula the heuristic predictor
uses: it adds noise and a couple of non-linear interactions, so the learned model
has something real to learn instead of memorising a straight line.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .predictor import HeuristicPredictor
from .regions import CarbonModel, Region, build_carbon_model, load_regions
from .scheduler import NoFeasibleRegion, POLICIES, POLICY_NOTES, Scheduler
from .workload import TENANT_EFFICIENCY, Task, generate_trace


# ---------------------------------------------------------------------------
# Ground truth (used only to build the training set)
# ---------------------------------------------------------------------------
def observe(task: Task, rng: random.Random) -> Tuple[float, float]:
    """What the job "actually" did, had we run it.

    Real clusters are messy: identical submissions differ run to run because of
    noisy neighbours, cache state and input skew. The lognormal noise term is
    what stops the predictor from reaching a fake R^2 of 1.0.
    """
    profile = task.profile
    size_factor = (max(task.input_size_gb, 0.5) / 12.0) ** 0.62

    # Parallel speed-up is not the same for every workload class. Training jobs
    # scale almost linearly across cores; analytics queries hit a shuffle
    # bottleneck and stop improving. A single exponent cannot express both.
    exponents = {"web": 0.30, "batch": 0.47, "ml_train": 0.66, "analytics": 0.34}
    parallel_factor = (4.0 / max(task.vcpus, 1)) ** exponents[task.task_type]

    # Out-of-core spill. Once the working set no longer fits in RAM the job
    # starts hitting disk and slows down sharply. It is a threshold effect, not
    # a smooth curve, which is exactly what a tree model handles well and a
    # power-law formula handles badly.
    spill_ratio = task.input_size_gb / max(task.memory_gb, 1.0)
    if spill_ratio > 1.0:
        spill_penalty = 1.0 + min(spill_ratio - 1.0, 6.0) * 0.34
    else:
        spill_penalty = 1.0

    tenant_factor = TENANT_EFFICIENCY.get(task.tenant, 1.0)

    runtime = (profile["base_hours"] * size_factor * parallel_factor
               * spill_penalty * tenant_factor)
    runtime *= rng.lognormvariate(0.0, 0.14)
    runtime = min(max(runtime, 0.05), 24.0)

    # A spilling job spends time blocked on I/O, so its CPUs sit idle.
    utilisation = profile["utilisation"] * rng.lognormvariate(0.0, 0.07)
    if spill_ratio > 1.0:
        utilisation *= 1.0 - min(0.30, (spill_ratio - 1.0) * 0.09)
    if task.priority == "high":
        utilisation *= 1.06
    utilisation = min(max(utilisation, 0.05), 0.99)
    return runtime, utilisation


def build_training_set(n: int = 4000, seed: int = 11) -> Tuple[List[Task], List[float], List[float]]:
    tasks = generate_trace(n=n, seed=seed)
    rng = random.Random(seed + 1)
    runtimes, utilisations = [], []
    for t in tasks:
        r, u = observe(t, rng)
        runtimes.append(r)
        utilisations.append(u)
    return tasks, runtimes, utilisations


# ---------------------------------------------------------------------------
# Running a trace
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    policy: str
    tasks: int
    unplaceable: int
    energy_kwh: float
    carbon_kg: float
    cost_usd: float
    hours_deferred: int
    sla_violations: int
    region_mix: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "tasks": self.tasks,
            "unplaceable": self.unplaceable,
            "energy_kwh": round(self.energy_kwh, 3),
            "carbon_kg": round(self.carbon_kg, 3),
            "cost_usd": round(self.cost_usd, 3),
            "hours_deferred": self.hours_deferred,
            "sla_violations": self.sla_violations,
            "sla_compliance_pct": round(
                100.0 * (self.tasks - self.sla_violations) / self.tasks, 2
            ) if self.tasks else 100.0,
            "region_mix": self.region_mix,
        }


def run_policy(
    tasks: Sequence[Task],
    policy: str,
    scheduler: Optional[Scheduler] = None,
    ledger=None,
) -> RunResult:
    """Schedule every task under one policy.

    Capacity is released immediately after each decision. We are modelling a
    steady-state cluster rather than an instantaneous burst, and holding every
    job's vCPUs for the whole trace would make the last few hundred tasks
    unplaceable for reasons that have nothing to do with the policy.
    """
    sched = scheduler or Scheduler()
    sched.reset_capacity()

    energy = carbon = cost = 0.0
    deferred = violations = unplaceable = 0
    mix: Dict[str, int] = {}

    for task in tasks:
        try:
            decision = sched.schedule(task, policy=policy)
        except NoFeasibleRegion:
            unplaceable += 1
            continue

        est = decision.chosen.est
        energy += est.energy_kwh
        carbon += est.carbon_kg
        cost += est.cost_usd
        deferred += decision.chosen.hours_deferred
        if not decision.sla_met:
            violations += 1
        mix[est.region_id] = mix.get(est.region_id, 0) + 1

        if ledger is not None:
            ledger.record_decision(decision)
        sched.release(decision)

    return RunResult(
        policy=policy,
        tasks=len(tasks) - unplaceable,
        unplaceable=unplaceable,
        energy_kwh=energy,
        carbon_kg=carbon,
        cost_usd=cost,
        hours_deferred=deferred,
        sla_violations=violations,
        region_mix=mix,
    )


def compare_policies(
    tasks: Optional[Sequence[Task]] = None,
    policies: Sequence[str] = POLICIES,
    scheduler: Optional[Scheduler] = None,
    n: int = 400,
    seed: int = 42,
) -> Dict[str, Any]:
    """Run the same trace through every policy and report deltas against
    `cost_only`, which is what a conventional cloud scheduler does today."""
    tasks = list(tasks) if tasks is not None else generate_trace(n=n, seed=seed)
    sched = scheduler or Scheduler()

    results = {p: run_policy(tasks, p, scheduler=sched) for p in policies}
    baseline = results.get("home_region")

    rows = []
    for policy, res in results.items():
        row = res.to_dict()
        row["note"] = POLICY_NOTES.get(policy, "")
        if baseline and baseline.carbon_kg > 0:
            # Positive = greener than the baseline; negative = worse.
            row["carbon_saving_pct"] = round(
                100.0 * (baseline.carbon_kg - res.carbon_kg) / baseline.carbon_kg, 2
            )
            row["cost_change_pct"] = round(
                100.0 * (res.cost_usd - baseline.cost_usd) / baseline.cost_usd, 2
            ) if baseline.cost_usd else 0.0
            row["avg_deferral_hours"] = round(
                res.hours_deferred / res.tasks, 2
            ) if res.tasks else 0.0
        rows.append(row)

    return {
        "trace_size": len(tasks),
        "seed": seed,
        "baseline_policy": "home_region",
        "results": rows,
    }
