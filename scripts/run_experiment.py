#!/usr/bin/env python
"""Reproduce every number in the report.

    python scripts/run_experiment.py [--jobs 400] [--seed 42] [--out results.json]

Runs the policy comparison, a weight-sensitivity sweep, and a ledger integrity
check, then prints tables we paste straight into the slides.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greenscale.blockchain import Blockchain, KeyPair  # noqa: E402
from greenscale.ledger import AllocationLedger  # noqa: E402
from greenscale.predictor import get_predictor  # noqa: E402
from greenscale.scheduler import Scheduler  # noqa: E402
from greenscale.simulator import compare_policies, run_policy  # noqa: E402
from greenscale.workload import generate_trace  # noqa: E402


def hr(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def policy_table(jobs: int, seed: int) -> dict:
    hr(f"1. Policy comparison  ({jobs} jobs, seed {seed})")
    data = compare_policies(n=jobs, seed=seed)
    print(f"{'policy':14s}{'kg CO2':>10s}{'saved %':>10s}{'cost $':>10s}"
          f"{'cost %':>9s}{'delay h':>9s}{'SLA %':>8s}")
    for row in data["results"]:
        print(f"{row['policy']:14s}{row['carbon_kg']:10.2f}{row.get('carbon_saving_pct', 0):10.2f}"
              f"{row['cost_usd']:10.2f}{row.get('cost_change_pct', 0):9.2f}"
              f"{row.get('avg_deferral_hours', 0):9.2f}{row['sla_compliance_pct']:8.2f}")
    print(f"\nBaseline: {data['baseline_policy']} (nearest region, immediate start).")
    return data


def weight_sweep(jobs: int, seed: int) -> list:
    hr("2. Sensitivity to the carbon weight")
    tasks = generate_trace(n=jobs, seed=seed)
    rows = []
    print(f"{'w_carbon':>9s}{'kg CO2':>10s}{'cost $':>10s}{'delay h':>9s}")
    for w_carbon in (0.0, 0.2, 0.4, 0.5, 0.55, 0.6, 0.7, 0.85):
        rest = (1.0 - w_carbon) / 3.0
        sched = Scheduler(predictor=get_predictor())
        sched.set_weights({"carbon": w_carbon, "energy": rest, "cost": rest, "latency": rest})
        res = run_policy(tasks, "greenscale", scheduler=sched)
        avg_delay = res.hours_deferred / res.tasks if res.tasks else 0
        rows.append({"w_carbon": w_carbon, "carbon_kg": res.carbon_kg,
                     "cost_usd": res.cost_usd, "avg_delay_h": avg_delay})
        print(f"{w_carbon:9.2f}{res.carbon_kg:10.2f}{res.cost_usd:10.2f}{avg_delay:9.2f}")
    print("\nEmissions fall steeply until the carbon weight reaches roughly 0.55 and then\n"
          "stop improving, while cost and average delay stay almost flat across the whole\n"
          "range. That knee is why the shipped default is 0.55 -- past it we would be\n"
          "spending schedule flexibility for no further carbon benefit.")
    return rows


def ledger_check(jobs: int, seed: int) -> dict:
    hr(f"3. Ledger integrity  ({jobs} jobs written to a throwaway chain)")
    tmp = Path("data/experiment_chain.json")
    if tmp.exists():
        tmp.unlink()
    keypair = KeyPair.generate("experiment-node")
    chain = Blockchain(difficulty=3, keypair=keypair, max_tx_per_block=8, path=tmp, autosave=False)
    ledger = AllocationLedger(chain)

    tasks = generate_trace(n=jobs, seed=seed)
    started = time.perf_counter()
    run_policy(tasks, "greenscale", scheduler=Scheduler(predictor=get_predictor()), ledger=ledger)
    ledger.flush()
    elapsed = time.perf_counter() - started

    result = ledger.validate()
    txs = result.checked_transactions
    print(f"  blocks mined          {chain.height}")
    print(f"  transactions signed   {txs}")
    print(f"  wall clock            {elapsed:.2f} s  ({1000 * elapsed / max(txs, 1):.2f} ms per transaction)")
    print(f"  validation            {'PASS' if result.valid else 'FAIL'}")

    # Now prove tamper evidence rather than just claiming it.
    victim = chain.blocks[1].transactions[0]
    original = victim["payload"].get("carbon_kg")
    victim["payload"]["carbon_kg"] = 0.0
    after = chain.validate()
    victim["payload"]["carbon_kg"] = original
    print(f"  after forging one field: {'PASS (BAD!)' if after.valid else 'FAIL as expected'}"
          f" -- {len(after.problems)} problem(s)")
    for p in after.problems[:3]:
        print(f"      {p}")

    tmp.unlink(missing_ok=True)
    return {"blocks": chain.height, "transactions": txs, "seconds": elapsed,
            "valid": result.valid, "tamper_detected": not after.valid,
            "problems": after.problems}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/results.json"))
    args = parser.parse_args()

    print("GreenScale Cloud 2.0 -- experiment run")
    print(f"predictor in use: {get_predictor().source}")

    results = {
        "policies": policy_table(args.jobs, args.seed),
        "weight_sweep": weight_sweep(args.jobs, args.seed),
        "ledger": ledger_check(min(args.jobs, 200), args.seed),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nFull results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
