"""Energy, carbon, cost and latency estimation for a (task, region, hour) triple.

Everything the scheduler scores comes out of this one module, so if a reviewer
disagrees with the physics they only have to argue with this file.

Model
-----
    P_it   = vcpus * (P_idle + (P_peak - P_idle) * u) + RAM_GB * P_ram
             + DISK_GB * P_disk                                     [watts]
    E      = P_it * PUE * runtime_hours / 1000                      [kWh]
    CO2    = E * carbon_intensity(region, hour) / 1000              [kg]
    Cost   = vcpus * price_per_vcpu_hour * runtime_hours            [USD]

PUE multiplies the IT load because cooling and power distribution are overhead
on top of what the servers themselves draw -- that is exactly what PUE measures.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .regions import CarbonModel, Region
from .workload import Task


@dataclass(frozen=True)
class Estimate:
    """The full cost of running one task in one region starting at one hour."""

    region_id: str
    start_hour: int
    runtime_hours: float
    utilisation: float
    it_power_w: float
    energy_kwh: float
    carbon_intensity: float   # gCO2/kWh, averaged over the hours the job runs
    carbon_kg: float
    cost_usd: float
    latency_ms: float

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "start_hour": self.start_hour,
            "runtime_hours": round(self.runtime_hours, 3),
            "utilisation": round(self.utilisation, 3),
            "it_power_w": round(self.it_power_w, 1),
            "energy_kwh": round(self.energy_kwh, 4),
            "carbon_intensity": round(self.carbon_intensity, 1),
            "carbon_kg": round(self.carbon_kg, 4),
            "cost_usd": round(self.cost_usd, 4),
            "latency_ms": round(self.latency_ms, 1),
        }


def it_power_watts(task: Task, utilisation: float) -> float:
    """Linear power model -- idle draw plus a utilisation-proportional part."""
    cpu = task.vcpus * (
        config.IDLE_POWER_PER_VCPU_W
        + (config.PEAK_POWER_PER_VCPU_W - config.IDLE_POWER_PER_VCPU_W) * utilisation
    )
    ram = task.memory_gb * config.POWER_PER_GB_RAM_W
    disk = task.input_size_gb * config.POWER_PER_GB_DISK_W
    return cpu + ram + disk


def average_intensity(
    carbon: CarbonModel, region_id: str, start_hour: int, runtime_hours: float
) -> float:
    """Mean grid intensity across every hour the job is actually running.

    A four-hour job started at 22:00 spans four different grid hours. Scoring it
    against only the 22:00 value would let the scheduler cheat by starting jobs
    right before the grid gets dirty.
    """
    span = max(1, int(round(runtime_hours + 0.499)))
    total = 0.0
    for offset in range(span):
        total += carbon.intensity(region_id, (start_hour + offset) % 24)
    return total / span


def estimate(
    task: Task,
    region: Region,
    carbon: CarbonModel,
    start_hour: int,
    runtime_hours: float,
    utilisation: float,
) -> Estimate:
    power = it_power_watts(task, utilisation)
    energy_kwh = power * region.pue * runtime_hours / 1000.0
    intensity = average_intensity(carbon, region.region_id, start_hour, runtime_hours)
    carbon_kg = energy_kwh * intensity / 1000.0
    cost = task.vcpus * region.usd_per_vcpu_hour * runtime_hours
    return Estimate(
        region_id=region.region_id,
        start_hour=start_hour,
        runtime_hours=runtime_hours,
        utilisation=utilisation,
        it_power_w=power,
        energy_kwh=energy_kwh,
        carbon_intensity=intensity,
        carbon_kg=carbon_kg,
        cost_usd=cost,
        latency_ms=region.latency_ms,
    )
