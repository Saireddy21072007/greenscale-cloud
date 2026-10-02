import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greenscale.blockchain import Blockchain, KeyPair  # noqa: E402
from greenscale.ledger import AllocationLedger  # noqa: E402
from greenscale.regions import build_carbon_model, load_regions  # noqa: E402
from greenscale.scheduler import Scheduler  # noqa: E402


@pytest.fixture
def regions():
    return load_regions()


@pytest.fixture
def carbon(regions):
    return build_carbon_model(regions)


@pytest.fixture
def scheduler(regions, carbon):
    return Scheduler(regions=regions, carbon=carbon)


@pytest.fixture
def chain(tmp_path):
    """Difficulty 1 so the whole suite does not spend its life mining."""
    return Blockchain(
        difficulty=1,
        keypair=KeyPair.generate("test-node"),
        max_tx_per_block=4,
        path=tmp_path / "chain.json",
        autosave=False,
    )


@pytest.fixture
def ledger(chain):
    return AllocationLedger(chain)
