"""One block: a header plus the transactions it seals."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .merkle import merkle_root, sha256_hex


@dataclass
class Block:
    index: int
    transactions: List[Dict[str, Any]]
    previous_hash: str
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0
    merkle_root_hex: str = ""
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.merkle_root_hex:
            self.merkle_root_hex = merkle_root(self.transactions)

    # -- hashing -----------------------------------------------------------
    def header_bytes(self) -> bytes:
        """Only header fields go into the hash.

        The transactions are covered indirectly through merkle_root_hex, so a
        block header stays a fixed small size no matter how many transactions it
        carries -- the same reason real chains do it this way.
        """
        header = {
            "index": self.index,
            "timestamp": round(self.timestamp, 6),
            "merkle_root": self.merkle_root_hex,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
        }
        return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def compute_hash(self) -> str:
        return sha256_hex(self.header_bytes())

    # -- proof of work -----------------------------------------------------
    def mine(self, difficulty: int) -> "Block":
        """Search for a nonce whose hash has `difficulty` leading zeros.

        Proof of work is honestly overkill for a single-writer ledger, and we say
        so in the report. We kept it because it makes rewriting history
        expensive: an attacker who edits block 12 must re-mine 12 and every block
        after it, and the dashboard's tamper test shows exactly that.
        """
        target = "0" * difficulty
        self.nonce = 0
        self.hash = self.compute_hash()
        while not self.hash.startswith(target):
            self.nonce += 1
            self.hash = self.compute_hash()
        return self

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "transactions": self.transactions,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
            "merkle_root": self.merkle_root_hex,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        return cls(
            index=data["index"],
            transactions=data["transactions"],
            previous_hash=data["previous_hash"],
            timestamp=data["timestamp"],
            nonce=data["nonce"],
            merkle_root_hex=data["merkle_root"],
            hash=data["hash"],
        )
