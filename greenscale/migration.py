"""Should a running VM be moved to a greener region right now?

The grid moves while a job is running. A four-hour training job placed in
Eemshaven at 09:00 UTC is sitting on a very clean grid; by 19:00 UTC that same
grid is at its dirtiest hour of the day. Live migration lets us react.

Migration is not free, and pretending otherwise is how you get a paper that
claims implausible savings. Moving a VM means pushing its memory image across a
WAN link, which costs energy, emits carbon at both ends, and causes a short
downtime that eats into the SLA. We therefore only recommend a move when the
carbon saved over the *remaining* runtime clears the migration's own footprint
by a margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .energy import average_intensity, it_power_watts
from .regions import CarbonModel, Region
from .workload import Task

# Energy to move one GB across the public internet. Published estimates vary by
# an order of magnitude (0.001-0.06 kWh/GB depending on year and methodology);
# we take a mid-range figure and flag the sensitivity in the report.
NETWORK_KWH_PER_GB = 0.006

# Live migration copies memory pages repeatedly while the VM keeps running, so
# the bytes actually transferred exceed the RAM size. 1.4x is a common
# measurement for pre-copy migration of a moderately busy VM.
DIRTY_PAGE_FACTOR = 1.4

# Do not move for a rounding error. The move must beat its own cost by 15%.
MIGRATION_MARGIN = 0.15


@dataclass
class MigrationAdvice:
    should_migrate: bool
    reason: str
    from_region: str
    to_region: Optional[str]
    carbon_if_stay_kg: float
    carbon_if_move_kg: float
    migration_cost_kg: float
    net_saving_kg: float
    downtime_seconds: float

    def to_dict(self) -> dict:
        return {
            "should_migrate": self.should_migrate,
            "reason": self.reason,
            "from_region": self.from_region,
            "to_region": self.to_region,
            "carbon_if_stay_kg": round(self.carbon_if_stay_kg, 5),
            "carbon_if_move_kg": round(self.carbon_if_move_kg, 5),
            "migration_cost_kg": round(self.migration_cost_kg, 5),
            "net_saving_kg": round(self.net_saving_kg, 5),
            "downtime_seconds": round(self.downtime_seconds, 2),
        }


def downtime_seconds(task: Task, link_gbps: float = 1.0) -> float:
    """Pre-copy migration keeps the VM up until a short final stop-and-copy."""
    final_copy_gb = task.memory_gb * 0.04  # residual dirty pages at cutover
    transfer = final_copy_gb * 8 / max(link_gbps, 0.1)
    return 0.35 + transfer  # 0.35 s of control-plane overhead


def migration_carbon_kg(
    task: Task,
    source: Region,
    target: Region,
    carbon: CarbonModel,
    utc_hour: int,
) -> float:
    """Carbon emitted by the act of moving, charged at both grids' current rate."""
    gb_moved = task.memory_gb * DIRTY_PAGE_FACTOR
    energy = gb_moved * NETWORK_KWH_PER_GB
    src_intensity = carbon.intensity(source.region_id, utc_hour)
    dst_intensity = carbon.intensity(target.region_id, utc_hour)
    # Split the transport energy across the two grids it traverses.
    return energy * (src_intensity + dst_intensity) / 2 / 1000.0


def evaluate(
    task: Task,
    current_region: Region,
    candidate_regions: Dict[str, Region],
    carbon: CarbonModel,
    utc_hour: int,
    hours_remaining: float,
    utilisation: float,
    committed: Optional[Dict[str, int]] = None,
) -> MigrationAdvice:
    """Compare staying put against the best legal alternative."""
    power_kw = it_power_watts(task, utilisation) / 1000.0

    def remaining_carbon(region: Region) -> float:
        energy = power_kw * region.pue * hours_remaining
        intensity = average_intensity(carbon, region.region_id, utc_hour, hours_remaining)
        return energy * intensity / 1000.0

    stay = remaining_carbon(current_region)

    if hours_remaining < 0.25:
        return MigrationAdvice(
            False, "Less than 15 minutes of runtime left; not worth the downtime.",
            current_region.region_id, None, stay, stay, 0.0, 0.0, 0.0,
        )

    best: Optional[Region] = None
    best_total = stay
    best_move = 0.0
    best_cost = 0.0

    for region in candidate_regions.values():
        if region.region_id == current_region.region_id:
            continue
        if task.residency_zone and region.residency_zone != task.residency_zone:
            continue
        if region.latency_ms > task.latency_budget_ms:
            continue
        if committed is not None and committed.get(region.region_id, 0) + task.vcpus > region.capacity_vcpu:
            continue

        move = remaining_carbon(region)
        cost = migration_carbon_kg(task, current_region, region, carbon, utc_hour)
        total = move + cost
        if total < best_total:
            best, best_total, best_move, best_cost = region, total, move, cost

    if best is None:
        return MigrationAdvice(
            False, "No reachable region is cleaner once migration cost is counted.",
            current_region.region_id, None, stay, stay, 0.0, 0.0, 0.0,
        )

    net = stay - best_total
    down = downtime_seconds(task)
    if net < stay * MIGRATION_MARGIN:
        return MigrationAdvice(
            False,
            f"Saving of {net:.4f} kg is under the {MIGRATION_MARGIN:.0%} margin "
            "required to justify a move.",
            current_region.region_id, best.region_id, stay, best_move, best_cost, net, down,
        )

    return MigrationAdvice(
        True,
        f"Moving to {best.region_id} cuts {net:.4f} kg CO2 over the remaining "
        f"{hours_remaining:.1f} h, net of migration cost.",
        current_region.region_id, best.region_id, stay, best_move, best_cost, net, down,
    )
