"""ECDSA keys for the scheduler node.

Hashing alone proves a record was not *edited*. It does not prove who wrote it --
anyone who can reach the ledger file could append a plausible-looking block and
re-hash the tail. Signing each transaction with a private key that never leaves
the scheduler closes that gap: an auditor holding only the public key can check
authorship without being able to forge it.

Curve is NIST P-256 (SECP256R1) with SHA-256, which is what the `cryptography`
package ships and what TLS uses everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: F401 (documented alt path)

_CURVE = ec.SECP256R1()
_ALGO = ec.ECDSA(hashes.SHA256())


class KeyPair:
    """Wraps one EC private key plus helpers to persist and share the public half."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, node_id: str = "scheduler-01"):
        self._private = private_key
        self.node_id = node_id

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def generate(cls, node_id: str = "scheduler-01") -> "KeyPair":
        return cls(ec.generate_private_key(_CURVE), node_id)

    @classmethod
    def load_or_create(cls, path: Path, node_id: str = "scheduler-01") -> "KeyPair":
        """Reuse the key across restarts, otherwise every restart orphans the
        signatures already in the ledger."""
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            return cls(key, node_id)

        pair = cls.generate(node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            pair._private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                # No passphrase: this is a demo key for a college project and it
                # is gitignored. A production deployment would keep it in a KMS
                # or an HSM, never on the application disk.
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        try:
            path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass  # Windows does not honour POSIX modes; harmless here.
        return pair

    # -- use ---------------------------------------------------------------
    def public_pem(self) -> str:
        return self._private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def sign(self, message: bytes) -> str:
        return self._private.sign(message, _ALGO).hex()


def verify(public_pem: str, signature_hex: str, message: bytes) -> bool:
    """True only if `signature_hex` was produced by the matching private key."""
    if not public_pem or not signature_hex:
        return False
    try:
        public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
        public_key.verify(bytes.fromhex(signature_hex), message, _ALGO)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def fingerprint(public_pem: Optional[str]) -> str:
    """Short, human-readable id for a public key -- shown in the ledger UI."""
    import hashlib

    if not public_pem:
        return "unsigned"
    return hashlib.sha256(public_pem.encode("ascii")).hexdigest()[:16]
