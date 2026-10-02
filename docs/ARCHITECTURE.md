# Architecture

## The request path

```
        job submission (HTTP POST /api/schedule)
                     |
                     v
        +------------------------+
        |  predictor.py          |   features -> (runtime_hours, utilisation)
        |  gradient-boosted trees|   R2 0.977 on held-out data
        +------------------------+
                     |
                     v
        +------------------------+
        |  scheduler.py          |   1. hard constraints filter regions
        |                        |      (residency, latency budget, capacity)
        |                        |   2. build (region x start-hour) candidates
        |                        |   3. energy.py prices each candidate
        |                        |   4. min-max normalise the four objectives
        |                        |   5. weighted sum + delay penalty -> winner
        +------------------------+
                     |
                     v
        +------------------------+
        |  ledger.py             |   ALLOCATION + CARBON_SAVING + SLA_* records
        +------------------------+
                     |
                     v
        +------------------------+
        |  blockchain/           |   sign (ECDSA P-256) -> pool -> Merkle root
        |                        |   -> proof of work -> append -> fsync
        +------------------------+
                     |
                     v
              decision + block hash returned
```

The dashboard reads back out of the same chain: `ledger.summary()` replays every sealed
transaction and recomputes the totals. There is no second copy of the truth.

## Why each design choice

### Why a weighted sum instead of a Pareto front

Four objectives and a handful of candidates means a Pareto analysis returns a *set*, and
a scheduler still has to return one placement. The weights are how an operator states
their trade-off explicitly, and the sensitivity sweep in `scripts/run_experiment.py` is
how we chose the shipped default rather than guessing it.

### Why min-max normalisation per decision

The four objectives have incompatible units — carbon in kilograms (order 0.01), cost in
dollars (order 0.1), latency in milliseconds (order 100). Summing raw values would let
latency silently dominate every decision regardless of the weights. Normalising across
the candidate set puts all four on [0, 1] so a weight of 0.55 actually means 55% of the
decision.

The subtlety is that normalisation is *relative to this job's candidates*. A job that
can only run in India sees a much narrower carbon range than one that can run anywhere,
so the same weights behave sensibly in both cases.

### Why constraints are filters, not weighted terms

Data residency and latency budgets are contractual. If they were terms in the score, a
high enough carbon weight could buy its way past a legal requirement. They are applied
before scoring and `test_residency_beats_any_weighting` pins that behaviour down.

### Why the start hour is part of the decision

Grid intensity varies by a factor of two within a day in a wind-and-solar-heavy region
like Eemshaven. Choosing only *where* throws away half the available saving. The
candidate set is therefore the cross product of feasible regions and permitted start
hours, bounded by the job's own delay tolerance and by `MAX_DEFERRAL_HOURS`.

Long jobs are charged the *average* intensity over the hours they span, not the
intensity at their start hour. Without that, the scheduler learns to start jobs in the
last clean hour before the grid gets dirty — technically optimal against the metric and
useless in reality.

### Why the ledger is the only store of record

If a SQL table of allocations sat next to the chain and the dashboard read from the
table, the table would be the real source of truth and the chain would be decoration.
That is precisely the criticism aimed at a lot of "blockchain-enabled" architectures.
Replaying the chain costs a few milliseconds at our scale. A production system would
cache the replay, which is still not the same as an authoritative database.

### Why we wrote the chain ourselves

Two reasons. The review asks us to explain how it works, and we can only do that for
code we wrote. And a Hyperledger Fabric network is much heavier than this problem needs:
we have one writer and many readers, which is the easiest possible consensus situation.

Three independent integrity mechanisms are layered, and the tamper test shows two of
them firing at once:

| Mechanism | Catches |
| --- | --- |
| Block hash over the header | any edit to index, timestamp, nonce, links, Merkle root |
| Merkle root over transactions | any edit to any transaction inside a block |
| ECDSA signature per transaction | forged records, and records altered after signing |
| `previous_hash` chaining | deletion or reordering of whole blocks |

Hashing alone would prove records were not *edited*; it would not prove *who* wrote them.
Anyone who could reach `data/chain.json` could append a plausible block and re-hash the
tail. The signature closes that gap: an auditor holding only the public key can verify
authorship without being able to forge it.

### Why proof of work at all

Honestly, it is overkill for a single-writer ledger, and we say so in the report. We kept
it for one property: rewriting history is expensive. An attacker who edits block 12 must
re-mine block 12 and every block after it. Difficulty 3 keeps a demo responsive; the
constant lives in `config.POW_DIFFICULTY` and `GS_POW_DIFFICULTY` overrides it.

### Why a learned predictor rather than a formula

We started with the closed-form model that is still in the code as
`HeuristicPredictor`. It is a reasonable power law: bigger input is slower, more vCPUs
is faster with diminishing returns. Two real effects break it.

- **Out-of-core spill.** Once the working set exceeds RAM the job starts hitting disk
  and slows down sharply. It is a threshold, not a smooth curve.
- **Tenant identity.** Different customers' code has different efficiency. Identical
  job specifications from two tenants do not take the same time, and no formula can
  know that — only history can.

Feeding both to gradient-boosted trees takes held-out runtime MAE from **2.54 h to
0.65 h**, a 74% improvement, at R² 0.977. `scripts/train_model.py` prints that
comparison on every run, and the rule we set ourselves is written into the script: if
the margin ever goes negative, delete the model and keep the formula.

The one-hot tenant block has a deliberate property — an unseen tenant gets an all-zero
block and falls back to population-average behaviour, so a new customer still gets a
prediction on their first day.

## Module boundaries

`app.py` makes no decisions. It parses HTTP, calls into the package, and serialises the
result. That is what let us write 72 tests against the package directly and keep only a
handful of route tests.

`energy.py` is the single place where physics happens. If a reviewer disagrees with the
power model, there is exactly one file to argue with.

`regions.py` is the single place that knows about carbon data. Replacing the static
snapshot with a live API means rewriting `CarbonModel.intensity` and nothing else.

## Concurrency

The chain's mutating methods take a re-entrant lock, and the Flask layer holds a module
lock around read-modify-write sequences. Two concurrent requests mining at once would
otherwise corrupt the pending pool — this actually happened during a demo run before the
lock was added.

The consequence for deployment is that the ledger lives in **one process**. See
[DEPLOYMENT.md](DEPLOYMENT.md).
