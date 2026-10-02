"""Energy/carbon model, predictor, regions and migration logic."""

import pytest

from greenscale.energy import average_intensity, estimate, it_power_watts
from greenscale.migration import downtime_seconds, migration_carbon_kg
from greenscale.migration import evaluate as evaluate_migration
from greenscale.predictor import FEATURE_NAMES, HeuristicPredictor, feature_vector
from greenscale.regions import HOURS_IN_DAY
from greenscale.workload import TASK_TYPES, TENANTS, Task, generate_trace


def make_task(**overrides) -> Task:
    defaults = dict(tenant="t", task_type="batch", vcpus=4, memory_gb=16,
                    input_size_gb=20, submitted_hour=12)
    defaults.update(overrides)
    return Task.new(**defaults)


# ---------------------------------------------------------------- regions ---
def test_every_region_has_a_carbon_profile(regions, carbon):
    for region_id in regions:
        curve = carbon.day_curve(region_id)
        assert len(curve) == HOURS_IN_DAY
        assert all(v > 0 for v in curve)


def test_profile_mean_equals_the_published_annual_average(regions, carbon):
    """Normalisation must not shift a region's yearly average."""
    for region_id, region in regions.items():
        curve = carbon.day_curve(region_id)
        assert sum(curve) / len(curve) == pytest.approx(region.grid_gco2_kwh, rel=1e-6)


def test_indian_grid_is_dirtier_than_the_nordic_grid_at_every_hour(carbon):
    for hour in range(HOURS_IN_DAY):
        assert carbon.intensity("ap-south-1", hour) > carbon.intensity("eu-north-1", hour)


def test_local_hour_conversion_handles_wraparound(regions):
    mumbai = regions["ap-south-1"]          # UTC+5.5
    assert mumbai.local_hour(0) == 5
    assert mumbai.local_hour(20) == 1       # 20:00 UTC is 01:30 next day IST
    oregon = regions["us-west-2"]           # UTC-7
    assert oregon.local_hour(3) == 20


# ----------------------------------------------------------------- energy ---
def test_power_rises_with_utilisation():
    task = make_task()
    assert it_power_watts(task, 0.1) < it_power_watts(task, 0.9)


def test_idle_power_is_never_zero():
    """An idle server still burns power -- this is why consolidation matters."""
    assert it_power_watts(make_task(), 0.0) > 0


def test_pue_multiplies_the_it_load(regions, carbon):
    task = make_task()
    low = regions["asia-south1"]   # PUE 1.10
    high = regions["ap-south-1"]   # PUE 1.20, effectively the same grid
    e_low = estimate(task, low, carbon, 12, 2.0, 0.7)
    e_high = estimate(task, high, carbon, 12, 2.0, 0.7)
    assert e_high.energy_kwh > e_low.energy_kwh
    assert e_high.energy_kwh / e_low.energy_kwh == pytest.approx(1.20 / 1.10, rel=1e-9)


def test_long_jobs_average_intensity_across_the_hours_they_span(carbon):
    one_hour = average_intensity(carbon, "europe-west4", 11, 1.0)
    twelve = average_intensity(carbon, "europe-west4", 11, 12.0)
    # Starting in the cleanest hour looks great for one hour and much worse
    # once the job runs into the evening peak.
    assert twelve > one_hour


def test_carbon_scales_with_intensity(regions, carbon):
    task = make_task()
    dirty = estimate(task, regions["ap-south-1"], carbon, 12, 1.0, 0.7)
    clean = estimate(task, regions["eu-north-1"], carbon, 12, 1.0, 0.7)
    assert dirty.carbon_kg > 5 * clean.carbon_kg


# -------------------------------------------------------------- predictor ---
def test_feature_vector_matches_the_declared_layout():
    assert len(feature_vector(make_task())) == len(FEATURE_NAMES)


def test_one_hot_encoding_sets_exactly_one_type_flag():
    for task_type in TASK_TYPES:
        row = dict(zip(FEATURE_NAMES, feature_vector(make_task(task_type=task_type))))
        assert sum(row[f"type_{t}"] for t in TASK_TYPES) == 1
        assert row[f"type_{task_type}"] == 1


def test_an_unseen_tenant_gets_an_all_zero_tenant_block():
    """New customers must still get a prediction on their first day."""
    row = dict(zip(FEATURE_NAMES, feature_vector(make_task(tenant="brand-new-corp"))))
    assert sum(row[f"tenant_{t}"] for t in TENANTS) == 0


def test_spill_flag_tracks_the_input_to_memory_ratio():
    fits = dict(zip(FEATURE_NAMES, feature_vector(make_task(memory_gb=64, input_size_gb=10))))
    spills = dict(zip(FEATURE_NAMES, feature_vector(make_task(memory_gb=8, input_size_gb=200))))
    assert fits["is_spilling"] == 0
    assert spills["is_spilling"] == 1
    assert spills["input_to_memory_ratio"] > fits["input_to_memory_ratio"]


def test_heuristic_predictions_stay_physical():
    predictor = HeuristicPredictor()
    for task in generate_trace(n=200, seed=3):
        p = predictor.predict(task)
        assert 0.05 <= p.runtime_hours <= 24.0
        assert 0.05 <= p.utilisation <= 0.99


def test_more_vcpus_shorten_a_job():
    predictor = HeuristicPredictor()
    small = predictor.predict(make_task(vcpus=2)).runtime_hours
    large = predictor.predict(make_task(vcpus=16)).runtime_hours
    assert large < small


def test_more_input_data_lengthens_a_job():
    predictor = HeuristicPredictor()
    assert (predictor.predict(make_task(input_size_gb=5)).runtime_hours
            < predictor.predict(make_task(input_size_gb=200)).runtime_hours)


# -------------------------------------------------------------- migration ---
def test_no_migration_when_the_job_is_nearly_done(regions, carbon):
    advice = evaluate_migration(
        task=make_task(memory_gb=32), current_region=regions["ap-south-1"],
        candidate_regions=regions, carbon=carbon, utc_hour=18,
        hours_remaining=0.1, utilisation=0.8,
    )
    assert not advice.should_migrate
    assert "15 minutes" in advice.reason


def test_dirty_to_clean_move_is_recommended_for_a_long_job(regions, carbon):
    advice = evaluate_migration(
        task=make_task(memory_gb=32, latency_budget_ms=400),
        current_region=regions["ap-south-1"], candidate_regions=regions,
        carbon=carbon, utc_hour=18, hours_remaining=6.0, utilisation=0.85,
    )
    assert advice.should_migrate
    assert advice.net_saving_kg > 0


def test_migration_cost_grows_with_the_memory_image(regions, carbon):
    small = migration_carbon_kg(make_task(memory_gb=2), regions["us-east-1"],
                                regions["eu-north-1"], carbon, 12)
    huge = migration_carbon_kg(make_task(memory_gb=2048), regions["us-east-1"],
                               regions["eu-north-1"], carbon, 12)
    assert huge > small
    assert downtime_seconds(make_task(memory_gb=2048)) > downtime_seconds(make_task(memory_gb=2))


def test_a_huge_vm_is_not_moved_for_a_short_remaining_runtime(regions, carbon):
    """The move has to pay for itself. Copying a 2 TB memory image to save 20
    minutes of slightly cleaner power does not."""
    kwargs = dict(current_region=regions["us-east-1"], candidate_regions=regions,
                  carbon=carbon, utc_hour=12, hours_remaining=0.4, utilisation=0.5)
    small = evaluate_migration(task=make_task(memory_gb=2, latency_budget_ms=400), **kwargs)
    huge = evaluate_migration(task=make_task(memory_gb=2048, latency_budget_ms=400), **kwargs)
    assert small.should_migrate
    assert not huge.should_migrate


def test_migration_respects_residency(regions, carbon):
    advice = evaluate_migration(
        task=make_task(residency_zone="IN", latency_budget_ms=400),
        current_region=regions["ap-south-1"], candidate_regions=regions,
        carbon=carbon, utc_hour=18, hours_remaining=6.0, utilisation=0.85,
    )
    assert advice.to_region in (None, "centralindia", "asia-south1")


# --------------------------------------------------------------- workload ---
def test_generated_trace_is_reproducible():
    a = generate_trace(n=50, seed=1)
    b = generate_trace(n=50, seed=1)
    assert [t.task_type for t in a] == [t.task_type for t in b]
    assert [t.vcpus for t in a] == [t.vcpus for t in b]


def test_web_jobs_are_never_deferrable():
    for task in generate_trace(n=300, seed=2):
        if task.task_type == "web":
            assert task.max_deferral_hours == 0


def test_task_validation_rejects_nonsense():
    with pytest.raises(ValueError):
        make_task(task_type="quantum").validate()
    with pytest.raises(ValueError):
        make_task(vcpus=0).validate()
    with pytest.raises(ValueError):
        make_task(submitted_hour=25).validate()
