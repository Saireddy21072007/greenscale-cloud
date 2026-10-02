"""Route-level tests. The heavy logic is covered elsewhere; these check that the
HTTP layer wires it up correctly and fails politely."""

import pytest

from greenscale.app import create_app
from greenscale.ledger import AllocationLedger


@pytest.fixture
def client(chain):
    app = create_app(ledger=AllocationLedger(chain))
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_health(client):
    body = client.get("/api/health").get_json()
    assert body["status"] == "ok"
    assert body["regions"] >= 8


def test_pages_render(client):
    for path in ("/", "/scheduler", "/compare", "/ledger"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b"GreenScale Cloud 2.0" in response.data


def test_unknown_page_returns_404(client):
    assert client.get("/nope").status_code == 404
    assert client.get("/api/nope").get_json()["error"] == "not found"


def test_regions_endpoint_lists_the_catalogue(client):
    regions = client.get("/api/regions").get_json()
    ids = {r["region_id"] for r in regions}
    assert {"ap-south-1", "eu-north-1", "us-east-1"} <= ids
    assert all(0 <= r["greenest_utc_hour"] <= 23 for r in regions)


def test_carbon_curves_have_24_points(client):
    curves = client.get("/api/carbon/curves").get_json()
    assert all(len(v) == 24 for v in curves.values())


def test_schedule_writes_to_the_chain(client):
    body = client.post("/api/schedule", json={
        "tenant": "medi-clinic", "task_type": "ml_train", "vcpus": 8,
        "memory_gb": 32, "input_size_gb": 40, "submitted_hour": 14,
        "max_deferral_hours": 12, "latency_budget_ms": 400,
    }).get_json()

    assert body["chosen"]["region_id"]
    assert body["ledger"]["block_index"] >= 1
    assert body["ledger"]["block_hash"].startswith("0")
    types = {t["type"] for t in body["ledger"]["transactions"]}
    assert "ALLOCATION" in types

    verify = client.get("/api/ledger/verify").get_json()
    assert verify["valid"], verify["problems"]


def test_schedule_rejects_a_bad_task_type(client):
    response = client.post("/api/schedule", json={"task_type": "quantum"})
    assert response.status_code == 400
    assert "quantum" in response.get_json()["error"]


def test_schedule_rejects_an_unknown_policy(client):
    response = client.post("/api/schedule", json={"policy": "vibes"})
    assert response.status_code == 400
    assert "greenscale" in response.get_json()["allowed"]


def test_impossible_constraints_return_400_with_reasons(client):
    response = client.post("/api/schedule", json={
        "task_type": "web", "latency_budget_ms": 1, "max_deferral_hours": 0,
    })
    assert response.status_code == 400
    assert response.get_json()["reasons"]


def test_summary_reflects_scheduled_work(client):
    for hour in (2, 9, 14, 19):
        client.post("/api/schedule", json={"task_type": "batch", "submitted_hour": hour,
                                           "latency_budget_ms": 400})
    summary = client.get("/api/summary").get_json()
    assert summary["tasks_scheduled"] == 4
    assert summary["total_energy_kwh"] > 0
    assert summary["sla_checks"] == 4


def test_compare_returns_every_policy(client):
    data = client.get("/api/compare?n=60&seed=3").get_json()
    policies = {r["policy"] for r in data["results"]}
    assert {"greenscale", "carbon_only", "cost_only", "home_region", "round_robin"} == policies
    assert data["baseline_policy"] == "home_region"


def test_tamper_test_reports_a_break_without_touching_the_ledger(client):
    client.post("/api/schedule", json={"task_type": "batch", "latency_budget_ms": 400})
    result = client.post("/api/ledger/tamper-test", json={"field": "carbon_kg", "value": 0.0}).get_json()

    assert result["before"]["valid"] is True
    assert result["after"]["valid"] is False
    assert result["after"]["problems"]
    # The real ledger must still be intact afterwards.
    assert client.get("/api/ledger/verify").get_json()["valid"]


def test_migration_endpoint(client):
    result = client.post("/api/migration/evaluate", json={
        "current_region": "ap-south-1", "utc_hour": 18, "vcpus": 8,
        "memory_gb": 32, "hours_remaining": 6, "latency_budget_ms": 400,
    }).get_json()
    assert "should_migrate" in result
    assert result["from_region"] == "ap-south-1"


def test_simulate_populates_the_ledger(client):
    body = client.post("/api/simulate", json={"n": 40, "seed": 7}).get_json()
    assert body["run"]["tasks"] > 0
    assert body["summary"]["tasks_scheduled"] == body["run"]["tasks"]
    assert client.get("/api/ledger/verify").get_json()["valid"]
