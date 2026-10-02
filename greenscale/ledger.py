"""The bridge between scheduling decisions and the blockchain.

Design choice worth defending in the review: the ledger is the *only* store of
record. There is no SQL table of allocations sitting next to it that the
dashboard reads from. Every number on the dashboard is recomputed by replaying
the chain. If we kept a mutable database alongside the chain, the database would
be the real source of truth and the chain would be decoration -- which is exactly
the criticism levelled at a lot of "blockchain-enabled" papers.

The trade-off is that summary() is O(number of transactions). At our scale
(a few thousand records) that is a couple of milliseconds. A production system
would keep a cache rebuilt from the chain on startup, which is still not the
same thing as an authoritative database.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .blockchain import Blockchain, KeyPair
from .scheduler import Decision

TX_ALLOCATION = "ALLOCATION"
TX_MIGRATION = "MIGRATION"
TX_CARBON_SAVING = "CARBON_SAVING"
TX_SLA_COMPLIANCE = "SLA_COMPLIANCE"
TX_SLA_VIOLATION = "SLA_VIOLATION"


class AllocationLedger:
    def __init__(self, chain: Blockchain):
        self.chain = chain

    @classmethod
    def open(
        cls,
        path: Optional[Path] = None,
        difficulty: Optional[int] = None,
        autosave: Optional[bool] = None,
    ) -> "AllocationLedger":
        path = Path(path or config.LEDGER_PATH)
        keypair = KeyPair.load_or_create(config.KEYSTORE_PATH)
        chain = Blockchain.open(
            path,
            difficulty=config.POW_DIFFICULTY if difficulty is None else difficulty,
            keypair=keypair,
            max_tx_per_block=config.MAX_TX_PER_BLOCK,
            autosave=config.CHAIN_AUTOSAVE if autosave is None else autosave,
        )
        return cls(chain)

    # -- writes ------------------------------------------------------------
    def record_decision(self, decision: Decision) -> List[Dict[str, Any]]:
        """Write the full story of one decision: where it went, what that saved,
        and whether the SLA held. Three transaction types because auditors ask
        three different questions and we do not want them parsing one blob."""
        written = [self._record_allocation(decision)]

        if decision.carbon_saved_kg > 0:
            written.append(
                self.chain.add_transaction(
                    TX_CARBON_SAVING,
                    {
                        "task_id": decision.task.task_id,
                        "tenant": decision.task.tenant,
                        "baseline_region": decision.baseline.region.region_id if decision.baseline else None,
                        "baseline_carbon_kg": round(decision.baseline.est.carbon_kg, 5) if decision.baseline else None,
                        "actual_region": decision.chosen.region.region_id,
                        "actual_carbon_kg": round(decision.chosen.est.carbon_kg, 5),
                        "saved_kg": round(decision.carbon_saved_kg, 5),
                        "saved_pct": round(decision.carbon_saved_pct, 2),
                        "cost_delta_usd": round(decision.cost_delta_usd, 5),
                    },
                )
            )

        written.append(self._record_sla(decision))
        return written

    def _record_allocation(self, decision: Decision) -> Dict[str, Any]:
        est = decision.chosen.est
        return self.chain.add_transaction(
            TX_ALLOCATION,
            {
                "task_id": decision.task.task_id,
                "tenant": decision.task.tenant,
                "task_type": decision.task.task_type,
                "vcpus": decision.task.vcpus,
                "memory_gb": decision.task.memory_gb,
                "policy": decision.policy,
                "region_id": est.region_id,
                "provider": decision.chosen.region.provider,
                "submitted_hour": decision.task.submitted_hour,
                "start_hour": est.start_hour,
                "hours_deferred": decision.chosen.hours_deferred,
                "runtime_hours": round(est.runtime_hours, 3),
                "prediction_source": decision.prediction.source,
                "energy_kwh": round(est.energy_kwh, 5),
                "carbon_intensity_gco2_kwh": round(est.carbon_intensity, 1),
                "carbon_kg": round(est.carbon_kg, 5),
                "cost_usd": round(est.cost_usd, 5),
                "latency_ms": round(est.latency_ms, 1),
                "weights": decision.weights,
                "score": round(decision.chosen.score, 5),
            },
        )

    def _record_sla(self, decision: Decision) -> Dict[str, Any]:
        tx_type = TX_SLA_COMPLIANCE if decision.sla_met else TX_SLA_VIOLATION
        return self.chain.add_transaction(
            tx_type,
            {
                "task_id": decision.task.task_id,
                "tenant": decision.task.tenant,
                "latency_budget_ms": decision.task.latency_budget_ms,
                "observed_latency_ms": round(decision.chosen.est.latency_ms, 1),
                "max_deferral_hours": decision.task.max_deferral_hours,
                "hours_deferred": decision.chosen.hours_deferred,
                "notes": decision.sla_notes,
            },
        )

    def record_migration(
        self,
        task_id: str,
        tenant: str,
        from_region: str,
        to_region: str,
        reason: str,
        carbon_before_kg: float,
        carbon_after_kg: float,
        migration_cost_kg: float,
        downtime_seconds: float,
    ) -> Dict[str, Any]:
        return self.chain.add_transaction(
            TX_MIGRATION,
            {
                "task_id": task_id,
                "tenant": tenant,
                "from_region": from_region,
                "to_region": to_region,
                "reason": reason,
                "carbon_before_kg": round(carbon_before_kg, 5),
                "carbon_after_kg": round(carbon_after_kg, 5),
                "migration_cost_kg": round(migration_cost_kg, 5),
                "net_saving_kg": round(carbon_before_kg - carbon_after_kg - migration_cost_kg, 5),
                "downtime_seconds": round(downtime_seconds, 2),
            },
        )

    def flush(self):
        return self.chain.flush()

    # -- reads -------------------------------------------------------------
    def validate(self):
        return self.chain.validate()

    def summary(self) -> Dict[str, Any]:
        """Replay the chain into the numbers the dashboard shows."""
        allocations = self.chain.transactions(TX_ALLOCATION)
        savings = self.chain.transactions(TX_CARBON_SAVING)
        compliant = self.chain.transactions(TX_SLA_COMPLIANCE)
        violations = self.chain.transactions(TX_SLA_VIOLATION)
        migrations = self.chain.transactions(TX_MIGRATION)

        by_region: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"tasks": 0, "carbon_kg": 0.0, "energy_kwh": 0.0, "cost_usd": 0.0}
        )
        by_tenant: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"tasks": 0, "carbon_kg": 0.0, "cost_usd": 0.0, "saved_kg": 0.0}
        )
        by_provider: Dict[str, int] = defaultdict(int)
        deferred_hours = 0

        total_energy = total_carbon = total_cost = 0.0
        for tx in allocations:
            p = tx["payload"]
            total_energy += p["energy_kwh"]
            total_carbon += p["carbon_kg"]
            total_cost += p["cost_usd"]
            deferred_hours += p.get("hours_deferred", 0)

            r = by_region[p["region_id"]]
            r["tasks"] += 1
            r["carbon_kg"] += p["carbon_kg"]
            r["energy_kwh"] += p["energy_kwh"]
            r["cost_usd"] += p["cost_usd"]

            t = by_tenant[p["tenant"]]
            t["tasks"] += 1
            t["carbon_kg"] += p["carbon_kg"]
            t["cost_usd"] += p["cost_usd"]

            by_provider[p.get("provider", "unknown")] += 1

        total_saved = 0.0
        total_cost_delta = 0.0
        for tx in savings:
            p = tx["payload"]
            total_saved += p["saved_kg"]
            total_cost_delta += p.get("cost_delta_usd", 0.0)
            by_tenant[p["tenant"]]["saved_kg"] += p["saved_kg"]

        sla_total = len(compliant) + len(violations)
        baseline_carbon = total_carbon + total_saved

        return {
            "tasks_scheduled": len(allocations),
            "total_energy_kwh": round(total_energy, 4),
            "total_carbon_kg": round(total_carbon, 4),
            "baseline_carbon_kg": round(baseline_carbon, 4),
            "carbon_saved_kg": round(total_saved, 4),
            "carbon_saved_pct": round(100.0 * total_saved / baseline_carbon, 2) if baseline_carbon else 0.0,
            "total_cost_usd": round(total_cost, 4),
            "cost_delta_usd": round(total_cost_delta, 4),
            "hours_deferred": deferred_hours,
            "migrations": len(migrations),
            "sla_checks": sla_total,
            "sla_violations": len(violations),
            "sla_compliance_pct": round(100.0 * len(compliant) / sla_total, 2) if sla_total else 100.0,
            "by_region": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in by_region.items()},
            "by_tenant": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in by_tenant.items()},
            "by_provider": dict(by_provider),
            "chain_height": self.chain.height,
            "pending_transactions": len(self.chain.pending),
            # A rough but real figure: an Indian household emits roughly 1.3 t
            # CO2 a year, and a mature tree sequesters about 21 kg a year.
            "equivalent_trees_year": round(total_saved / 21.0, 2),
        }

    def recent(self, limit: int = 25, tx_type: Optional[str] = None) -> List[Dict[str, Any]]:
        txs = self.chain.transactions(tx_type)
        return list(reversed(txs))[:limit]
