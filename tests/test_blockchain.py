"""Integrity properties of the ledger.

These are the tests we actually care about: the chain is only worth having if a
forged record is detectable, so each way of forging one gets its own test.
"""

import pytest

from greenscale.blockchain import Blockchain, KeyPair, merkle_root
from greenscale.blockchain.chain import signable_bytes
from greenscale.blockchain.crypto import verify
from greenscale.blockchain.merkle import hash_transaction, proof, verify_proof


def test_genesis_exists_and_is_valid(chain):
    assert chain.height == 1
    assert chain.blocks[0].index == 0
    assert chain.blocks[0].previous_hash == "0" * 64
    assert chain.validate().valid


def test_transactions_seal_into_a_block_when_the_pool_fills(chain):
    for i in range(4):  # max_tx_per_block is 4 in the fixture
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}", "carbon_kg": 0.1})
    assert chain.height == 2
    assert chain.pending == []
    assert chain.validate().valid


def test_blocks_link_to_their_predecessor(chain):
    for i in range(8):
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}"})
    for previous, current in zip(chain.blocks, chain.blocks[1:]):
        assert current.previous_hash == previous.hash


def test_proof_of_work_meets_difficulty(chain):
    for i in range(4):
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}"})
    for block in chain.blocks:
        assert block.hash.startswith("0" * chain.difficulty)
        assert block.compute_hash() == block.hash


def test_editing_a_payload_breaks_validation(chain):
    for i in range(4):
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}", "carbon_kg": 0.5})
    assert chain.validate().valid

    chain.blocks[1].transactions[0]["payload"]["carbon_kg"] = 0.0
    result = chain.validate()
    assert not result.valid
    # Two independent alarms fire: the Merkle root no longer covers the payload,
    # and the signature no longer matches it.
    assert any("Merkle" in p for p in result.problems)
    assert any("signature" in p for p in result.problems)


def test_editing_a_header_field_breaks_the_hash(chain):
    for i in range(4):
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}"})
    chain.blocks[1].nonce += 1
    result = chain.validate()
    assert not result.valid
    assert any("stored hash does not match" in p for p in result.problems)


def test_removing_a_block_breaks_the_chain(chain):
    for i in range(12):
        chain.add_transaction("ALLOCATION", {"task_id": f"t{i}"})
    assert chain.height >= 4
    del chain.blocks[1]
    result = chain.validate()
    assert not result.valid
    assert any("previous_hash" in p for p in result.problems)


def test_a_transaction_signed_by_an_unknown_node_is_rejected(chain):
    chain.add_transaction("ALLOCATION", {"task_id": "t0"})
    chain.flush()
    outsider = KeyPair.generate("attacker")
    forged = {
        "tx_id": "tx-forged", "type": "ALLOCATION", "timestamp": 1.0,
        "payload": {"task_id": "evil", "carbon_kg": 0.0},
    }
    forged["signer"] = outsider.node_id
    forged["signature"] = outsider.sign(signable_bytes(forged))
    chain.blocks[1].transactions.append(forged)
    chain.blocks[1].merkle_root_hex = merkle_root(chain.blocks[1].transactions)
    chain.blocks[1].hash = chain.blocks[1].compute_hash()

    result = chain.validate()
    assert not result.valid
    assert any("unknown node" in p for p in result.problems)


def test_signature_verifies_only_over_its_own_payload(chain):
    tx = chain.add_transaction("ALLOCATION", {"task_id": "t0", "carbon_kg": 1.0})
    pem = chain.public_keys[tx["signer"]]
    assert verify(pem, tx["signature"], signable_bytes(tx))

    tampered = dict(tx)
    tampered["payload"] = {"task_id": "t0", "carbon_kg": 0.0}
    assert not verify(pem, tx["signature"], signable_bytes(tampered))


def test_persistence_round_trip(tmp_path):
    keypair = KeyPair.generate("node-a")
    path = tmp_path / "chain.json"
    original = Blockchain(difficulty=1, keypair=keypair, max_tx_per_block=2, path=path)
    for i in range(6):
        original.add_transaction("ALLOCATION", {"task_id": f"t{i}"})
    original.flush()
    original.save()

    reloaded = Blockchain.load(path, keypair=keypair)
    assert reloaded.height == original.height
    assert reloaded.last_block.hash == original.last_block.hash
    assert reloaded.validate().valid


# ---------------------------------------------------------------- Merkle ----
def test_merkle_root_is_order_sensitive():
    a = {"tx_id": "1", "v": 1}
    b = {"tx_id": "2", "v": 2}
    assert merkle_root([a, b]) != merkle_root([b, a])


def test_merkle_root_is_key_order_insensitive():
    assert hash_transaction({"a": 1, "b": 2}) == hash_transaction({"b": 2, "a": 1})


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 9])
def test_merkle_proof_verifies_for_every_leaf(n):
    txs = [{"tx_id": str(i), "payload": {"v": i}} for i in range(n)]
    root = merkle_root(txs)
    for i in range(n):
        assert verify_proof(txs[i], proof(txs, i), root)


def test_merkle_proof_fails_for_a_forged_leaf():
    txs = [{"tx_id": str(i)} for i in range(4)]
    root = merkle_root(txs)
    path = proof(txs, 2)
    assert not verify_proof({"tx_id": "forged"}, path, root)
