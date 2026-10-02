"""Carbon-aware multi-objective scheduler.

The decision the scheduler makes is a pair: *which region* and *which hour*. Both
matter and they interact -- Stockholm at 03:00 UTC is not the same as Stockholm
at 15:00 UTC, and neither is comparable to Mumbai at either hour.

Scoring
-------
For every feasible (region, start_hour) candidate we compute four raw objectives
(carbon kg, energy kWh, cost USD, latency ms), min-max normalise each one across
the candidate set, and take a weighted sum. Lower is better.

    score = Sw_i * normalised_i  +  delay_penalty * hours_deferred

Why min-max and not raw values: the objectives have incompatible units. Carbon is
in kilograms (order 0.01), cost in dollars (order 0.1), latency in milliseconds
(order 100). Summing them raw would mean latency silently decides everything.
Normalising per decision puts all four on [0, 1] so the weights actually mean
what the slider says they mean.

Why a weighted sum and not a Pareto front: with four objectives and a handful of
candidates a full Pareto analysis returns a set, and a scheduler has to return
one answer anyway. The weights are how the operator states their trade-off, and
`compare_policies` shows what the alternatives would have picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .energy import Estimate, estimate
from .predictor import Prediction, get_predictor
from .regions import CarbonModel, Region, build_carbon_model, load_regions
from .workload import Task

POLICIES = ("greenscale", "carbon_only", "cost_only", "home_region", "round_robin")

# What each comparator represents, in one line. Shown in the dashboard legend.
POLICY_NOTES = {
    "greenscale": "Our proposal: multi-objective score over region and start hour.",
    "carbon_only": "Minimise CO2 and nothing else -- shows what pure greenness costs in delay.",
    "cost_only": "Cheapest region, start now. Classic cost-optimising scheduler.",
    "home_region": "Nearest region, start now. This is how almost every Indian SMB "
                   "deploys today, so it is our baseline.",
    "round_robin": "Naive even spread across regions. Included as a sanity floor.",
}


@dataclass
class Candidate:
    region: Region
    start_hour: int
    hours_deferred: int
    est: Estimate
    normalised: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "region_id": self.region.region_id,
            "region_label": self.region.label,
            "provider": self.region.provider,
            "start_hour": self.start_hour,
            "hours_deferred": self.hours_deferred,
            "score": round(self.score, 4),
            "normalised": {k: round(v, 4) for k, v in self.normalised.items()},
            **self.est.to_dict(),
        }


@dataclass
class Decision:
    task: Task
    policy: str
    prediction: Prediction
    chosen: Candidate
    candidates: List[Candidate]
    baseline: Optional[Candidate]
    sla_met: bool
    sla_notes: List[str]
    weights: Dict[str, float]

    @property
    def carbon_saved_kg(self) -> float:
        if self.baseline is None:
            return 0.0
        return max(0.0, self.baseline.est.carbon_kg - self.chosen.est.carbon_kg)

    @property
    def carbon_saved_pct(self) -> float:
        if self.baseline is None or self.baseline.est.carbon_kg <= 0:
            return 0.0
        return 100.0 * self.carbon_saved_kg / self.baseline.est.carbon_kg

    @property
    def cost_delta_usd(self) -> float:
        """Positive means the green choice cost us more than the cheap choice."""
        if self.baseline is None:
            return 0.0
        return self.chosen.est.cost_usd - self.baseline.est.cost_usd

    def to_dict(self, include_candidates: bool = True) -> Dict[str, Any]:
        out = {
            "task": self.task.to_dict(),
            "policy": self.policy,
            "prediction": {
                "runtime_hours": round(self.prediction.runtime_hours, 3),
                "utilisation": round(self.prediction.utilisation, 3),
                "source": self.prediction.source,
            },
            "chosen": self.chosen.to_dict(),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "carbon_saved_kg": round(self.carbon_saved_kg, 4),
            "carbon_saved_pct": round(self.carbon_saved_pct, 2),
            "cost_delta_usd": round(self.cost_delta_usd, 4),
            "sla_met": self.sla_met,
            "sla_notes": self.sla_notes,
            "weights": self.weights,
        }
        if include_candidates:
            out["candidates"] = [c.to_dict() for c in self.candidates]
        return out


class NoFeasibleRegion(Exception):
    """Raised when constraints rule out every region. The API turns this into a
    400 with the reasons attached, rather than silently placing the job
    somewhere that breaks the tenant's contract."""

    def __init__(self, task: Task, reasons: List[str]):
        super().__init__(f"No feasible region for task {task.task_id}: {'; '.join(reasons)}")
        self.reasons = reasons


class Scheduler:
    def __init__(
        self,
        regions: Optional[Dict[str, Region]] = None,
        carbon: Optional[CarbonModel] = None,
        predictor=None,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.regions = regions or load_regions()
        self.carbon = carbon or build_carbon_model(self.regions)
        # Default to whatever is actually deployed -- the trained model if one
        # is on disk, the heuristic otherwise. Defaulting to the heuristic here
        # meant the experiment script reported "predictor: model" while the
        # comparison table had quietly been produced by the formula.
        self.predictor = predictor or get_predictor()
        self.weights = dict(weights or config.DEFAULT_WEIGHTS)
        # vCPUs currently committed per region. Reset between experiments.
        self.committed: Dict[str, int] = {rid: 0 for rid in self.regions}
        self._round_robin_cursor = 0

    # -- weights -----------------------------------------------------------
    def set_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Accept partial weight dicts and renormalise so they sum to 1."""
        merged = dict(self.weights)
        merged.update({k: float(v) for k, v in weights.items() if k in merged})
        total = sum(merged.values())
        if total <= 0:
            raise ValueError("weights must contain at least one positive value")
        self.weights = {k: v / total for k, v in merged.items()}
        return self.weights

    # -- feasibility -------------------------------------------------------
    def feasible_regions(self, task: Task) -> tuple[List[Region], List[str]]:
        """Hard constraints. A region that fails any of these is never scored --
        we do not want a weight slider to be able to buy its way past a data
        residency rule."""
        allowed: List[Region] = []
        reasons: List[str] = []
        for region in self.regions.values():
            if task.residency_zone and region.residency_zone != task.residency_zone:
                reasons.append(
                    f"{region.region_id}: outside residency zone {task.residency_zone}"
                )
                continue
            if self.committed[region.region_id] + task.vcpus > region.capacity_vcpu:
                reasons.append(f"{region.region_id}: not enough free vCPU capacity")
                continue
            if region.latency_ms > task.latency_budget_ms + config.SLA_LATENCY_GRACE_MS:
                reasons.append(
                    f"{region.region_id}: {region.latency_ms:.0f} ms exceeds the "
                    f"{task.latency_budget_ms:.0f} ms latency budget"
                )
                continue
            allowed.append(region)
        return allowed, reasons

    # -- candidate generation ---------------------------------------------
    def _build_candidates(self, task: Task, prediction: Prediction) -> List[Candidate]:
        allowed, reasons = self.feasible_regions(task)
        if not allowed:
            raise NoFeasibleRegion(task, reasons)

        max_defer = min(task.max_deferral_hours, config.MAX_DEFERRAL_HOURS)
        # High-priority work is never deferred even if its class allows it.
        if task.priority == "high":
            max_defer = 0

        candidates: List[Candidate] = []
        for region in allowed:
            for delay in range(max_defer + 1):
                start = (task.submitted_hour + delay) % 24
                est = estimate(
                    task, region, self.carbon, start,
                    prediction.runtime_hours, prediction.utilisation,
                )
                candidates.append(Candidate(region, start, delay, est))
        return candidates

    @staticmethod
    def _normalise(candidates: Sequence[Candidate]) -> None:
        """Min-max each objective across the candidate set, in place."""
        fields = {
            "carbon": lambda c: c.est.carbon_kg,
            "energy": lambda c: c.est.energy_kwh,
            "cost": lambda c: c.est.cost_usd,
            "latency": lambda c: c.est.latency_ms,
        }
        for name, getter in fields.items():
            values = [getter(c) for c in candidates]
            lo, hi = min(values), max(values)
            span = hi - lo
            for c, v in zip(candidates, values):
                # Every candidate identical on this objective => it carries no
                # information for this decision, so score it flat at 0.
                c.normalised[name] = 0.0 if span <= 1e-12 else (v - lo) / span

    def _score(self, candidates: Sequence[Candidate]) -> None:
        self._normalise(candidates)
        for c in candidates:
            weighted = sum(self.weights[k] * c.normalised[k] for k in self.weights)
            c.score = weighted + config.DELAY_PENALTY_PER_HOUR * c.hours_deferred

    # -- policies ----------------------------------------------------------
    def _pick(self, candidates: List[Candidate], policy: str) -> Candidate:
        if policy == "greenscale":
            return min(candidates, key=lambda c: c.score)
        if policy == "carbon_only":
            return min(candidates, key=lambda c: c.est.carbon_kg)
        if policy == "cost_only":
            # A conventional cost-optimising scheduler also has no reason to
            # wait, so it is restricted to starting immediately.
            immediate = [c for c in candidates if c.hours_deferred == 0] or candidates
            return min(immediate, key=lambda c: c.est.cost_usd)
        if policy == "home_region":
            # "Home" = whichever feasible region is closest to the user. For our
            # Indian tenants that is almost always Mumbai or Pune, which sit on
            # one of the dirtiest grids in the catalogue. That is precisely the
            # problem this project exists to fix, so it is the honest baseline.
            immediate = [c for c in candidates if c.hours_deferred == 0] or candidates
            return min(immediate, key=lambda c: c.est.latency_ms)
        if policy == "round_robin":
            immediate = [c for c in candidates if c.hours_deferred == 0] or candidates
            pick = immediate[self._round_robin_cursor % len(immediate)]
            self._round_robin_cursor += 1
            return pick
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")

    # -- SLA ---------------------------------------------------------------
    def _check_sla(self, task: Task, chosen: Candidate) -> tuple[bool, List[str]]:
        notes: List[str] = []
        if chosen.est.latency_ms > task.latency_budget_ms + config.SLA_LATENCY_GRACE_MS:
            notes.append(
                f"latency {chosen.est.latency_ms:.0f} ms over budget "
                f"{task.latency_budget_ms:.0f} ms"
            )
        if chosen.hours_deferred > task.max_deferral_hours:
            notes.append(
                f"deferred {chosen.hours_deferred} h beyond the agreed "
                f"{task.max_deferral_hours} h window"
            )
        return (not notes), notes

    # -- entry point -------------------------------------------------------
    def schedule(self, task: Task, policy: str = "greenscale", commit: bool = True) -> Decision:
        task.validate()
        prediction = self.predictor.predict(task)
        candidates = self._build_candidates(task, prediction)
        self._score(candidates)

        chosen = self._pick(candidates, policy)
        # Savings are always quoted against what the tenant would get today with
        # no scheduler at all: nearest region, immediate start.
        baseline = self._pick(list(candidates), "home_region")

        sla_met, notes = self._check_sla(task, chosen)
        if commit:
            self.committed[chosen.region.region_id] += task.vcpus

        return Decision(
            task=task,
            policy=policy,
            prediction=prediction,
            chosen=chosen,
            candidates=sorted(candidates, key=lambda c: c.score),
            baseline=baseline,
            sla_met=sla_met,
            sla_notes=notes,
            weights=dict(self.weights),
        )

    def release(self, decision: Decision) -> None:
        """Give the capacity back when a job finishes."""
        rid = decision.chosen.region.region_id
        self.committed[rid] = max(0, self.committed[rid] - decision.task.vcpus)

    def reset_capacity(self) -> None:
        self.committed = {rid: 0 for rid in self.regions}
        self._round_robin_cursor = 0
