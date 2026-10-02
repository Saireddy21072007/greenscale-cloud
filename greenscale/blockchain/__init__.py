"""A small but real blockchain: Merkle-summarised, ECDSA-signed, proof-of-work.

We wrote this ourselves instead of pulling in Hyperledger or Ganache for two
reasons. First, the review asks us to explain how the chain works, and we can
only do that for code we wrote. Second, a full Fabric network is far heavier than
this project needs -- we have one writer (the scheduler) and many readers
(auditors), which is the easiest possible consensus problem.

What it does give us, and what the base paper's frameworks do not:
  * append-only history of every allocation decision
  * tamper evidence -- editing any past record breaks every hash after it
  * origin proof -- each record is signed by the scheduler's private key
"""

from .block import Block
from .chain import Blockchain, ValidationResult
from .crypto import KeyPair
from .merkle import merkle_root

__all__ = ["Block", "Blockchain", "ValidationResult", "KeyPair", "merkle_root"]
