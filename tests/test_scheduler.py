"""Scheduler behaviour: constraints are hard, weights are soft, and the
carbon-aware policy actually beats the baseline."""

import pytest

from greenscale.scheduler import NoFeasibleRegion, Scheduler
from greenscale.simulator import compare_policies, run_policy
from greenscale.workload import Task, generate_trace


def make_task(**overrides) -> Task:
    defaults = dict(
        tenant="test-tenant", task_type="batch", vcpus=4, memory_gb=16,
        input_size_gb=20, submitted_hour=12, max_deferral_hours=8,
        latency_budget_ms=400,
    )
    defaults.update(overrides)
    return Task.new(**defaults)


# ------------------------------------------------------------ constraints ---
def test_residency_zone_is_a_hard_filter(scheduler):
    task = make_task(residency_zone="IN")
    decision = scheduler.schedule(task)
    assert decision.chosen.region.residency_zone == "IN"
    assert all(c.region.residency_zone == "IN" for c in decision.candidates)


def test_residency_beats_any_weighting(scheduler):
    """Even with carbon as the only objective, an IN-locked job stays in India."""
    scheduler.set_weights({"carbon": 1.0, "energy": 0.0, "cost": 0.0, "latency": 0.0})
    decision = scheduler.schedule(make_task(residency_zone="IN", max_deferral_hours=12))
    assert decision.chosen.region.country == "India"


def test_latency_budget_excludes_distant_regions(scheduler):
    decision = scheduler.schedule(make_task(task_type="web", latency_budget_ms=60,
                                            max_deferral_hours=0))
    assert decision.chosen.est.latency_ms <= 60 + 15  # budget + configured grace
    assert decision.sla_met


def test_impossible_constraints_raise_with_reasons(scheduler):
    with pytest.raises(NoFeasibleRegion) as excinfo:
        scheduler.schedule(make_task(latency_budget_ms=1, max_deferral_hours=0))
    assert excinfo.value.reasons
    assert any("latency budget" in r for r in excinfo.value.reasons)


def test_capacity_is_consumed_and_released(scheduler):
    region_id = "eu-north-1"
    before = scheduler.committed[region_id]
    task = make_task(vcpus=8, residency_zone="EU")
    decision = scheduler.schedule(task)
    assert scheduler.committed[decision.chosen.region.region_id] == before + 8
    scheduler.release(decision)
    assert scheduler.committed[decision.chosen.region.region_id] == before


def test_high_priority_jobs_are_never_deferred(scheduler):
    decision = scheduler.schedule(make_task(priority="high", max_deferral_hours=12))
    assert decision.chosen.hours_deferred == 0


def test_zero_deferral_produces_one_candidate_per_region(scheduler):
    decision = scheduler.schedule(make_task(max_deferral_hours=0))
    starts = {c.start_hour for c in decision.candidates}
    assert starts == {12}


# ---------------------------------------------------------------- scoring ---
def test_normalised_objectives_stay_in_unit_range(scheduler):
    decision = scheduler.schedule(make_task())
    for candidate in decision.candidates:
        for name, value in candidate.normalised.items():
            assert 0.0 <= value <= 1.0, f"{name} out of range: {value}"


def test_chosen_candidate_has_the_lowest_score(scheduler):
    decision = scheduler.schedule(make_task())
    assert decision.chosen.score == min(c.score for c in decision.candidates)


def test_all_carbon_weight_picks_the_lowest_carbon_candidate(scheduler):
    scheduler.set_weights({"carbon": 1.0, "energy": 0.0, "cost": 0.0, "latency": 0.0})
    # Deferral carries its own penalty, so compare within the same start hour.
    decision = scheduler.schedule(make_task(max_deferral_hours=0))
    assert decision.chosen.est.carbon_kg == min(c.est.carbon_kg for c in decision.candidates)


def test_all_cost_weight_picks_the_cheapest_candidate(scheduler):
    scheduler.set_weights({"carbon": 0.0, "energy": 0.0, "cost": 1.0, "latency": 0.0})
    decision = scheduler.schedule(make_task(max_deferral_hours=0))
    assert decision.chosen.est.cost_usd == pytest.approx(
        min(c.est.cost_usd for c in decision.candidates)
    )


def test_weights_are_renormalised(scheduler):
    weights = scheduler.set_weights({"carbon": 2.0, "cost": 2.0})
    assert sum(weights.values()) == pytest.approx(1.0)


# --------------------------------------------------------------- outcomes ---
def test_greenscale_beats_the_home_region_baseline_on_a_trace():
    tasks = generate_trace(n=200, seed=5)
    sched = Scheduler()
    green = run_policy(tasks, "greenscale", scheduler=sched)
    baseline = run_policy(tasks, "home_region", scheduler=sched)
    assert green.carbon_kg < baseline.carbon_kg
    saving = 100 * (baseline.carbon_kg - green.carbon_kg) / baseline.carbon_kg
    # We claim "roughly 55-65%" in the report; fail loudly if that drifts.
    assert 40 < saving < 80, f"carbon saving moved to {saving:.1f}%"


def test_carbon_only_is_greener_but_slower_than_greenscale():
    data = compare_policies(n=200, seed=5)
    rows = {r["policy"]: r for r in data["results"]}
    assert rows["carbon_only"]["carbon_kg"] <= rows["greenscale"]["carbon_kg"]
    assert rows["carbon_only"]["avg_deferral_hours"] > rows["greenscale"]["avg_deferral_hours"]


def test_no_sla_violations_under_the_default_profile():
    result = run_policy(generate_trace(n=200, seed=9), "greenscale")
    assert result.sla_violations == 0
    assert result.unplaceable == 0
