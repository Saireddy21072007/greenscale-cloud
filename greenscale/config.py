"""Central place for every tunable number in the project.

We kept all constants here instead of sprinkling them through the code, because
during the review we had to justify each one and it is much easier when they sit
in a single file with a comment next to them.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"
LEDGER_PATH = DATA_DIR / "chain.json"
KEYSTORE_PATH = DATA_DIR / "scheduler_key.pem"

DATA_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Power model
# ---------------------------------------------------------------------------
# Server power is modelled the usual linear way: a fixed idle draw plus a part
# that scales with CPU utilisation. The numbers below are per-vCPU figures taken
# from the SPECpower-style model used in Beloglazov et al. (2012), scaled down
# from a whole socket to one vCPU.
IDLE_POWER_PER_VCPU_W = 3.1
PEAK_POWER_PER_VCPU_W = 12.4
POWER_PER_GB_RAM_W = 0.38

# Storage draw is small but not zero; included so the numbers stay honest.
POWER_PER_GB_DISK_W = 0.006


# ---------------------------------------------------------------------------
# Scheduling weights (must sum to 1.0 -- validated at load time)
# ---------------------------------------------------------------------------
# These are the four objectives from our abstract. The default profile leans on
# carbon because that is the point of the project, but the dashboard lets you
# move the sliders and re-run, which is how we produced the comparison table.
DEFAULT_WEIGHTS = {
    "carbon": 0.55,
    "energy": 0.15,
    "cost": 0.20,
    "latency": 0.10,
}
# We started at carbon=0.40 because it felt like a reasonable balance. The
# sensitivity sweep in scripts/run_experiment.py showed that was leaving a lot
# on the table: emissions keep falling steeply until about 0.55 and then stop
# improving, while cost and delay barely move across that whole range. So the
# default is set from the measurement, not from intuition.

# Waiting for greener grid hours is only useful up to a point -- past this the
# job is just late. Applied as a soft penalty inside the scoring function.
DELAY_PENALTY_PER_HOUR = 0.012
MAX_DEFERRAL_HOURS = 12


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------
# If the chosen region's round-trip latency is worse than the task's budget, or
# the job finishes after its deadline, we log an SLA_VIOLATION transaction.
DEFAULT_LATENCY_BUDGET_MS = 250
SLA_LATENCY_GRACE_MS = 15


# ---------------------------------------------------------------------------
# Blockchain
# ---------------------------------------------------------------------------
# Difficulty 3 mines a block in a few milliseconds on a laptop, which keeps the
# dashboard responsive during a demo. Raise it to 5 if you want to actually feel
# the proof-of-work cost.
POW_DIFFICULTY = int(os.getenv("GS_POW_DIFFICULTY", "3"))
MAX_TX_PER_BLOCK = 8
CHAIN_AUTOSAVE = True


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("GS_SECRET_KEY", "dev-only-change-me")
HOST = os.getenv("GS_HOST", "0.0.0.0")
PORT = int(os.getenv("GS_PORT", "5000"))
DEBUG = os.getenv("GS_DEBUG", "0") == "1"


def validate() -> None:
    """Fail loudly at import time rather than silently mis-scoring later."""
    total = sum(DEFAULT_WEIGHTS.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"DEFAULT_WEIGHTS must sum to 1.0, got {total}")


validate()
