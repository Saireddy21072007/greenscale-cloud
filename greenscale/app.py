"""Flask application: REST API plus the four dashboard pages.

Structure follows the usual thin-controller rule -- nothing in this file makes a
scheduling or ledger decision, it only translates HTTP to a call into the
package and back. That is what let us write the tests in tests/ against the
package directly and keep only a handful of route tests.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict

from flask import Flask, jsonify, render_template, request

from . import config
from .blockchain.block import Block
from .blockchain.chain import Blockchain
from .ledger import AllocationLedger
from .migration import evaluate as evaluate_migration
from .predictor import get_predictor
from .regions import build_carbon_model, load_regions
from .scheduler import NoFeasibleRegion, POLICIES, POLICY_NOTES, Scheduler
from .simulator import compare_policies, run_policy
from .workload import DEFAULT_DEFERRAL, TASK_TYPES, Task, generate_trace

# One scheduler and one ledger per process, guarded by a lock. Gunicorn with
# several workers would give each worker its own copy of the chain, which is
# wrong -- see DEPLOYMENT.md, we run a single worker with threads for exactly
# this reason.
_lock = threading.Lock()


def create_app(ledger: AllocationLedger | None = None) -> Flask:
    app = Flask(__name__, template_folder="../web/templates", static_folder="../web/static")
    app.config["SECRET_KEY"] = config.SECRET_KEY

    regions = load_regions()
    carbon = build_carbon_model(regions)
    predictor = get_predictor()
    scheduler = Scheduler(regions=regions, carbon=carbon, predictor=predictor)
    ledger = ledger or AllocationLedger.open()

    app.extensions["greenscale"] = {
        "scheduler": scheduler,
        "ledger": ledger,
        "carbon": carbon,
        "regions": regions,
    }

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.route("/")
    def dashboard():
        with _lock:
            summary = ledger.summary()
        return render_template(
            "dashboard.html",
            summary=summary,
            regions=list(regions.values()),
            active="dashboard",
        )

    @app.route("/scheduler")
    def scheduler_page():
        return render_template(
            "scheduler.html",
            task_types=sorted(TASK_TYPES),
            default_deferral=DEFAULT_DEFERRAL,
            regions=list(regions.values()),
            policies=POLICIES,
            policy_notes=POLICY_NOTES,
            weights=scheduler.weights,
            predictor_source=predictor.source,
            active="scheduler",
        )

    @app.route("/ledger")
    def ledger_page():
        with _lock:
            ledger.flush()
            blocks = [b.to_dict() for b in reversed(ledger.chain.blocks)]
            result = ledger.validate()
        return render_template(
            "ledger.html",
            blocks=blocks,
            validation=result.to_dict(),
            difficulty=ledger.chain.difficulty,
            node_ids=list(ledger.chain.public_keys),
            active="ledger",
        )

    @app.route("/compare")
    def compare_page():
        return render_template(
            "compare.html", policies=POLICIES, policy_notes=POLICY_NOTES, active="compare"
        )

    # ------------------------------------------------------------------
    # API -- read
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "chain_height": ledger.chain.height,
                "pending": len(ledger.chain.pending),
                "predictor": predictor.source,
                "regions": len(regions),
            }
        )

    @app.get("/api/regions")
    def api_regions():
        return jsonify(
            [
                {
                    "region_id": r.region_id,
                    "provider": r.provider,
                    "site": r.site,
                    "country": r.country,
                    "residency_zone": r.residency_zone,
                    "grid_gco2_kwh": r.grid_gco2_kwh,
                    "pue": r.pue,
                    "renewable_share": r.renewable_share,
                    "usd_per_vcpu_hour": r.usd_per_vcpu_hour,
                    "latency_ms": r.latency_ms,
                    "capacity_vcpu": r.capacity_vcpu,
                    "committed_vcpu": scheduler.committed[r.region_id],
                    "greenest_utc_hour": carbon.greenest_hour(r.region_id),
                }
                for r in regions.values()
            ]
        )

    @app.get("/api/carbon/curves")
    def api_curves():
        return jsonify({rid: carbon.day_curve(rid) for rid in regions})

    @app.get("/api/summary")
    def api_summary():
        with _lock:
            return jsonify(ledger.summary())

    @app.get("/api/ledger")
    def api_ledger():
        limit = min(int(request.args.get("limit", 25)), 500)
        tx_type = request.args.get("type") or None
        with _lock:
            return jsonify(ledger.recent(limit=limit, tx_type=tx_type))

    @app.get("/api/ledger/verify")
    def api_verify():
        with _lock:
            ledger.flush()
            return jsonify(ledger.validate().to_dict())

    # ------------------------------------------------------------------
    # API -- write
    # ------------------------------------------------------------------
    @app.post("/api/schedule")
    def api_schedule():
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        policy = body.get("policy", "greenscale")
        if policy not in POLICIES:
            return jsonify({"error": f"unknown policy {policy!r}", "allowed": list(POLICIES)}), 400

        try:
            task = Task.new(
                tenant=body.get("tenant", "demo-tenant"),
                task_type=body.get("task_type", "batch"),
                vcpus=int(body.get("vcpus", 4)),
                memory_gb=float(body.get("memory_gb", 16)),
                input_size_gb=float(body.get("input_size_gb", 20)),
                submitted_hour=int(body.get("submitted_hour", 12)),
                max_deferral_hours=int(body.get("max_deferral_hours", 6)),
                latency_budget_ms=float(body.get("latency_budget_ms", 250)),
                residency_zone=body.get("residency_zone") or None,
                priority=body.get("priority", "normal"),
            )
            task.validate()
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        with _lock:
            if body.get("weights"):
                scheduler.set_weights(body["weights"])
            try:
                decision = scheduler.schedule(task, policy=policy)
            except NoFeasibleRegion as exc:
                return jsonify({"error": str(exc), "reasons": exc.reasons}), 400

            written = ledger.record_decision(decision)
            # Seal immediately so the demo can point at a real block rather than
            # a pending transaction.
            block = ledger.flush()
            # Capacity is released right away; this endpoint models a placement
            # decision, not a long-lived reservation.
            scheduler.release(decision)

        payload = decision.to_dict()
        payload["ledger"] = {
            "transactions": [{"tx_id": t["tx_id"], "type": t["type"]} for t in written],
            "block_index": block.index if block else None,
            "block_hash": block.hash if block else None,
            "nonce": block.nonce if block else None,
        }
        return jsonify(payload)

    @app.post("/api/simulate")
    def api_simulate():
        body = request.get_json(silent=True) or {}
        n = min(int(body.get("n", 100)), 1000)
        seed = int(body.get("seed", 42))
        policy = body.get("policy", "greenscale")
        if policy not in POLICIES:
            return jsonify({"error": f"unknown policy {policy!r}"}), 400

        tasks = generate_trace(n=n, seed=seed)
        with _lock:
            result = run_policy(tasks, policy, scheduler=scheduler, ledger=ledger)
            ledger.flush()
            summary = ledger.summary()
        return jsonify({"run": result.to_dict(), "summary": summary})

    @app.get("/api/compare")
    def api_compare():
        n = min(int(request.args.get("n", 300)), 2000)
        seed = int(request.args.get("seed", 42))
        with _lock:
            # Runs on a throwaway scheduler so a comparison never disturbs the
            # capacity counters or the ledger the dashboard is showing.
            fresh = Scheduler(regions=regions, carbon=carbon, predictor=predictor,
                              weights=scheduler.weights)
            return jsonify(compare_policies(n=n, seed=seed, scheduler=fresh))

    @app.post("/api/migration/evaluate")
    def api_migration():
        body = request.get_json(silent=True) or {}
        try:
            task = Task.new(
                tenant=body.get("tenant", "demo-tenant"),
                task_type=body.get("task_type", "ml_train"),
                vcpus=int(body.get("vcpus", 8)),
                memory_gb=float(body.get("memory_gb", 32)),
                input_size_gb=float(body.get("input_size_gb", 40)),
                submitted_hour=int(body.get("utc_hour", 18)),
                latency_budget_ms=float(body.get("latency_budget_ms", 400)),
                residency_zone=body.get("residency_zone") or None,
            )
            current = regions[body.get("current_region", "europe-west4")]
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": f"bad request: {exc}"}), 400

        prediction = predictor.predict(task)
        advice = evaluate_migration(
            task=task,
            current_region=current,
            candidate_regions=regions,
            carbon=carbon,
            utc_hour=int(body.get("utc_hour", 18)),
            hours_remaining=float(body.get("hours_remaining", prediction.runtime_hours)),
            utilisation=prediction.utilisation,
            committed=scheduler.committed,
        )
        result = advice.to_dict()
        if advice.should_migrate:
            with _lock:
                tx = ledger.record_migration(
                    task_id=task.task_id,
                    tenant=task.tenant,
                    from_region=advice.from_region,
                    to_region=advice.to_region,
                    reason=advice.reason,
                    carbon_before_kg=advice.carbon_if_stay_kg,
                    carbon_after_kg=advice.carbon_if_move_kg,
                    migration_cost_kg=advice.migration_cost_kg,
                    downtime_seconds=advice.downtime_seconds,
                )
                ledger.flush()
            result["tx_id"] = tx["tx_id"]
        return jsonify(result)

    @app.post("/api/ledger/tamper-test")
    def api_tamper():
        """Prove the chain actually detects edits.

        This runs on a deep copy. The real ledger is never modified -- we are
        demonstrating a property, not vandalising the store of record.
        """
        body = request.get_json(silent=True) or {}
        field = body.get("field", "carbon_kg")
        new_value = body.get("value", 0.0)

        with _lock:
            ledger.flush()
            before = ledger.validate().to_dict()
            # Snapshot through to_dict() rather than deepcopy: the chain holds a
            # threading lock, which is not copyable.
            snapshot = copy.deepcopy(ledger.chain.to_dict())

        clone = Blockchain(
            difficulty=snapshot["difficulty"],
            keypair=None,
            max_tx_per_block=snapshot["max_tx_per_block"],
            path=None,
            autosave=False,
        )
        clone.blocks = [Block.from_dict(b) for b in snapshot["blocks"]]
        clone.public_keys = snapshot["public_keys"]
        target = None
        for block in clone.blocks:
            for tx in block.transactions:
                if tx["type"] == "ALLOCATION" and field in tx["payload"]:
                    target = (block.index, tx["tx_id"], tx["payload"][field])
                    tx["payload"][field] = new_value
                    break
            if target:
                break

        if target is None:
            return jsonify({"error": f"no ALLOCATION transaction carries a {field!r} field"}), 400

        after = clone.validate().to_dict()
        return jsonify(
            {
                "edited": {
                    "block_index": target[0],
                    "tx_id": target[1],
                    "field": field,
                    "original_value": target[2],
                    "forged_value": new_value,
                },
                "before": before,
                "after": after,
                "note": "Performed on an in-memory copy; the stored ledger is untouched.",
            }
        )

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return render_template("404.html", active=""), 404

    return app
