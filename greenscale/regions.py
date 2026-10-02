"""Multi-cloud region catalogue and the carbon-intensity model.

Two CSVs drive everything here:

  data/regions.csv          one row per cloud region (price, latency, PUE, the
                            annual average grid carbon intensity, capacity)
  data/carbon_profiles.csv  24 multipliers per region describing how the grid
                            swings over a day in *local* time

Why a shape multiplied by an annual average instead of one flat number: the
whole idea of carbon-aware scheduling is that a grid is dirtier at 19:00 than at
11:00. If we only stored the annual average, every hour would look identical and
the scheduler would never have a reason to defer a job. Splitting it this way
also means anyone can swap in a live feed (Electricity Maps / WattTime) later by
replacing one function, `CarbonModel.intensity`.

The numbers in regions.csv are a static snapshot of published grid averages,
not live data. That limitation is stated in the report as well -- we did not
want to claim a live feed we do not have.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Dict, List

from .config import DATA_DIR

HOURS_IN_DAY = 24


@dataclass(frozen=True)
class Region:
    region_id: str
    provider: str
    site: str
    country: str
    residency_zone: str      # IN / EU / US / SA -- used for data-residency rules
    utc_offset: float
    grid_gco2_kwh: float     # annual average carbon intensity of the local grid
    pue: float               # power usage effectiveness of the data centre
    renewable_share: float
    usd_per_vcpu_hour: float
    latency_ms: float        # measured from our campus in Coimbatore, India
    capacity_vcpu: int

    @property
    def label(self) -> str:
        return f"{self.provider} {self.region_id} ({self.site})"

    def local_hour(self, utc_hour: int) -> int:
        """Convert a UTC hour to the region's local hour bucket."""
        return int((utc_hour + self.utc_offset) % HOURS_IN_DAY)


class CarbonModel:
    """Carbon intensity lookup, in gCO2eq per kWh delivered to the IT load."""

    def __init__(self, regions: Dict[str, Region], profiles: Dict[str, List[float]]):
        self._regions = regions
        # Normalise every profile so its 24-hour mean is exactly 1.0. Without
        # this the shape would quietly scale the annual average up or down and
        # our "carbon saved" numbers would be wrong.
        self._profiles = {
            rid: _normalise(shape) for rid, shape in profiles.items()
        }

    def intensity(self, region_id: str, utc_hour: int) -> float:
        region = self._regions[region_id]
        shape = self._profiles[region_id]
        return region.grid_gco2_kwh * shape[region.local_hour(utc_hour)]

    def day_curve(self, region_id: str) -> List[float]:
        """All 24 UTC hours for one region -- used by the dashboard chart."""
        return [self.intensity(region_id, h) for h in range(HOURS_IN_DAY)]

    def greenest_hour(self, region_id: str) -> int:
        curve = self.day_curve(region_id)
        return min(range(HOURS_IN_DAY), key=lambda h: curve[h])


def _normalise(shape: List[float]) -> List[float]:
    mean = sum(shape) / len(shape)
    return [v / mean for v in shape]


def load_regions() -> Dict[str, Region]:
    path = DATA_DIR / "regions.csv"
    regions: Dict[str, Region] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            regions[row["region_id"]] = Region(
                region_id=row["region_id"],
                provider=row["provider"],
                site=row["site"],
                country=row["country"],
                residency_zone=row["residency_zone"],
                utc_offset=float(row["utc_offset"]),
                grid_gco2_kwh=float(row["grid_gco2_kwh"]),
                pue=float(row["pue"]),
                renewable_share=float(row["renewable_share"]),
                usd_per_vcpu_hour=float(row["usd_per_vcpu_hour"]),
                latency_ms=float(row["latency_ms"]),
                capacity_vcpu=int(row["capacity_vcpu"]),
            )
    if not regions:
        raise RuntimeError(f"No regions found in {path}")
    return regions


def load_profiles() -> Dict[str, List[float]]:
    path = DATA_DIR / "carbon_profiles.csv"
    profiles: Dict[str, List[float]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            profiles[row["region_id"]] = [
                float(row[f"h{h:02d}"]) for h in range(HOURS_IN_DAY)
            ]
    return profiles


def build_carbon_model(regions: Dict[str, Region] | None = None) -> CarbonModel:
    regions = regions or load_regions()
    profiles = load_profiles()
    missing = set(regions) - set(profiles)
    if missing:
        raise RuntimeError(f"carbon_profiles.csv is missing rows for: {sorted(missing)}")
    return CarbonModel(regions, profiles)
