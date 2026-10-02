"""Merkle tree over a block's transactions.

The block header stores only the 32-byte root. That is what lets an auditor
prove "transaction X is in block 47" by sending ~log2(n) hashes instead of the
whole block -- the standard Merkle proof, implemented in `proof` / `verify_proof`
below because the reviewer asked us to demonstrate it, not just cite it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_transaction(tx: Dict[str, Any]) -> str:
    """Canonical JSON so the same transaction always hashes the same way.

    sort_keys is not optional here: Python dict ordering would otherwise leak
    into the hash and two identical transactions could get different digests.
    """
    payload = json.dumps(tx, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_hex(payload.encode("utf-8"))


def _pair(left: str, right: str) -> str:
    return sha256_hex((left + right).encode("utf-8"))


def merkle_levels(leaves: List[str]) -> List[List[str]]:
    """All levels of the tree, leaves first, root last."""
    if not leaves:
        return [[sha256_hex(b"")]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        current = levels[-1]
        # Odd node count: duplicate the last hash. Same convention as Bitcoin.
        if len(current) % 2 == 1:
            current = current + [current[-1]]
        levels.append([_pair(current[i], current[i + 1]) for i in range(0, len(current), 2)])
    return levels


def merkle_root(transactions: List[Dict[str, Any]]) -> str:
    return merkle_levels([hash_transaction(tx) for tx in transactions])[-1][0]


def proof(transactions: List[Dict[str, Any]], index: int) -> List[Tuple[str, str]]:
    """Sibling hashes needed to walk transaction `index` up to the root.

    Returns [(side, hash), ...] where side is 'left' or 'right' relative to the
    node being carried upward.
    """
    leaves = [hash_transaction(tx) for tx in transactions]
    if not 0 <= index < len(leaves):
        raise IndexError(f"transaction index {index} out of range")

    path: List[Tuple[str, str]] = []
    levels = merkle_levels(leaves)
    idx = index
    for level in levels[:-1]:
        row = level if len(level) % 2 == 0 else level + [level[-1]]
        sibling = idx ^ 1
        side = "right" if idx % 2 == 0 else "left"
        path.append((side, row[sibling]))
        idx //= 2
    return path


def verify_proof(leaf_tx: Dict[str, Any], path: List[Tuple[str, str]], root: str) -> bool:
    node = hash_transaction(leaf_tx)
    for side, sibling in path:
        node = _pair(node, sibling) if side == "right" else _pair(sibling, node)
    return node == root
