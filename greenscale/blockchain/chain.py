"""The chain itself: append transactions, seal them into mined blocks, validate.

Threading note: the Flask dev server and gunicorn both serve requests
concurrently, and two requests mining at once would corrupt the pending pool.
Every mutating method therefore takes a re-entrant lock. This bit me during the
demo run before we added it.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .block import Block
from .crypto import KeyPair, fingerprint, verify
from .merkle import merkle_root

GENESIS_PREVIOUS_HASH = "0" * 64


@dataclass
class ValidationResult:
    valid: bool
    checked_blocks: int
    checked_transactions: int
    problems: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_blocks": self.checked_blocks,
            "checked_transactions": self.checked_transactions,
            "problems": self.problems,
        }


def signable_bytes(tx: Dict[str, Any]) -> bytes:
    """Exactly the fields a signature commits to.

    `signature` and `signer_fingerprint` are excluded for the obvious reason
    that they do not exist yet when the signature is computed.
    """
    core = {k: tx[k] for k in ("tx_id", "type", "timestamp", "payload")}
    return json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class Blockchain:
    def __init__(
        self,
        difficulty: int = 3,
        keypair: Optional[KeyPair] = None,
        max_tx_per_block: int = 8,
        path: Optional[Path] = None,
        autosave: bool = True,
    ):
        self.difficulty = difficulty
        self.max_tx_per_block = max_tx_per_block
        self.keypair = keypair
        self.path = path
        self.autosave = autosave and path is not None
        self._lock = threading.RLock()

        self.blocks: List[Block] = []
        self.pending: List[Dict[str, Any]] = []
        # node_id -> public key PEM. Lives inside the chain file so that a copy
        # of the ledger is independently verifiable with no extra files.
        self.public_keys: Dict[str, str] = {}

        if keypair is not None:
            self.public_keys[keypair.node_id] = keypair.public_pem()

        self._create_genesis()

    # -- construction ------------------------------------------------------
    def _create_genesis(self) -> None:
        genesis_tx = {
            "tx_id": "genesis",
            "type": "GENESIS",
            "timestamp": 0.0,
            "payload": {
                "project": "GreenScale Cloud 2.0",
                "note": "Immutable ledger for carbon-aware resource allocation",
            },
            "signer": None,
            "signature": None,
        }
        genesis = Block(index=0, transactions=[genesis_tx], previous_hash=GENESIS_PREVIOUS_HASH,
                        timestamp=0.0)
        genesis.mine(self.difficulty)
        self.blocks = [genesis]

    # -- properties --------------------------------------------------------
    @property
    def last_block(self) -> Block:
        return self.blocks[-1]

    @property
    def height(self) -> int:
        return len(self.blocks)

    def transactions(self, tx_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every sealed transaction, oldest first. Pending ones are not included --
        they are not durable yet, and the dashboard says so explicitly."""
        out: List[Dict[str, Any]] = []
        for block in self.blocks:
            for tx in block.transactions:
                if tx_type is None or tx["type"] == tx_type:
                    enriched = dict(tx)
                    enriched["block_index"] = block.index
                    out.append(enriched)
        return out

    # -- writing -----------------------------------------------------------
    def add_transaction(self, tx_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sign a record and queue it. Seals a block once the pool is full."""
        with self._lock:
            tx = {
                "tx_id": f"tx-{uuid.uuid4().hex[:12]}",
                "type": tx_type,
                "timestamp": time.time(),
                "payload": payload,
            }
            if self.keypair is not None:
                tx["signer"] = self.keypair.node_id
                tx["signature"] = self.keypair.sign(signable_bytes(tx))
                tx["signer_fingerprint"] = fingerprint(self.keypair.public_pem())
            else:
                tx["signer"] = None
                tx["signature"] = None

            self.pending.append(tx)
            if len(self.pending) >= self.max_tx_per_block:
                self.mine_pending()
            elif self.autosave:
                self._save_unlocked()
            return tx

    def mine_pending(self) -> Optional[Block]:
        """Seal whatever is queued into a new block. Returns None if nothing waits."""
        with self._lock:
            if not self.pending:
                return None
            block = Block(
                index=self.last_block.index + 1,
                transactions=self.pending,
                previous_hash=self.last_block.hash,
            )
            started = time.perf_counter()
            block.mine(self.difficulty)
            self.blocks.append(block)
            self.pending = []
            if self.autosave:
                self._save_unlocked()
            self._last_mine_seconds = time.perf_counter() - started
            return block

    def flush(self) -> Optional[Block]:
        """Force a seal -- called before showing the ledger so nothing is stuck
        in the pending pool at the end of a demo run."""
        return self.mine_pending()

    # -- validation --------------------------------------------------------
    def validate(self) -> ValidationResult:
        """Full audit: hashes, links, proof of work, Merkle roots, signatures."""
        problems: List[str] = []
        tx_count = 0

        for i, block in enumerate(self.blocks):
            if block.compute_hash() != block.hash:
                problems.append(
                    f"Block {block.index}: stored hash does not match its contents "
                    "(a header field was edited)."
                )
            if merkle_root(block.transactions) != block.merkle_root_hex:
                problems.append(
                    f"Block {block.index}: Merkle root mismatch "
                    "(a transaction inside the block was edited)."
                )
            if not block.hash.startswith("0" * self.difficulty):
                problems.append(
                    f"Block {block.index}: hash does not satisfy difficulty {self.difficulty}."
                )
            if i == 0:
                if block.previous_hash != GENESIS_PREVIOUS_HASH:
                    problems.append("Genesis block has a non-zero previous hash.")
            else:
                if block.previous_hash != self.blocks[i - 1].hash:
                    problems.append(
                        f"Block {block.index}: previous_hash does not point at block "
                        f"{block.index - 1} (the chain is broken here)."
                    )

            for tx in block.transactions:
                tx_count += 1
                if tx["type"] == "GENESIS":
                    continue
                signer = tx.get("signer")
                if signer is None:
                    problems.append(f"Transaction {tx['tx_id']} is unsigned.")
                    continue
                pem = self.public_keys.get(signer)
                if pem is None:
                    problems.append(
                        f"Transaction {tx['tx_id']} signed by unknown node {signer!r}."
                    )
                elif not verify(pem, tx.get("signature") or "", signable_bytes(tx)):
                    problems.append(
                        f"Transaction {tx['tx_id']}: signature does not verify "
                        "(forged or altered after signing)."
                    )

        return ValidationResult(
            valid=not problems,
            checked_blocks=len(self.blocks),
            checked_transactions=tx_count,
            problems=problems,
        )

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "difficulty": self.difficulty,
            "max_tx_per_block": self.max_tx_per_block,
            "public_keys": self.public_keys,
            "blocks": [b.to_dict() for b in self.blocks],
            "pending": self.pending,
        }

    def save(self, path: Optional[Path] = None) -> Path:
        with self._lock:
            return self._save_unlocked(path)

    def _save_unlocked(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and replace, so a crash mid-write cannot leave a
        # half-written ledger behind.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(
        cls,
        path: Path,
        keypair: Optional[KeyPair] = None,
        autosave: bool = True,
    ) -> "Blockchain":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        chain = cls(
            difficulty=data.get("difficulty", 3),
            keypair=keypair,
            max_tx_per_block=data.get("max_tx_per_block", 8),
            path=path,
            autosave=autosave,
        )
        chain.blocks = [Block.from_dict(b) for b in data["blocks"]]
        chain.pending = data.get("pending", [])
        chain.public_keys = data.get("public_keys", {})
        if keypair is not None:
            chain.public_keys[keypair.node_id] = keypair.public_pem()
        return chain

    @classmethod
    def open(
        cls,
        path: Path,
        difficulty: int = 3,
        keypair: Optional[KeyPair] = None,
        max_tx_per_block: int = 8,
        autosave: bool = True,
    ) -> "Blockchain":
        """Load an existing ledger or start a fresh one at `path`."""
        if Path(path).exists():
            try:
                return cls.load(path, keypair=keypair, autosave=autosave)
            except (json.JSONDecodeError, KeyError):
                # Corrupt ledger: keep the bad file for inspection rather than
                # silently overwriting evidence.
                Path(path).replace(Path(str(path) + ".corrupt"))
        return cls(
            difficulty=difficulty,
            keypair=keypair,
            max_tx_per_block=max_tx_per_block,
            path=Path(path),
            autosave=autosave,
        )
