"""Ed25519 signing keys for audit checkpoints and evidence bundles (Part 12).

The private key lives at AUDIT_SIGNING_KEY_PATH (never committed). key_id = the first 16 hex chars of the sha256
of the raw public key. Public keys are looked up by key_id in AUDIT_PUBLIC_KEYS_DIRS (<key_id>.pem), so rotated
(old) keys keep verifying old checkpoints. In development a key is generated on first use; production refuses.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

logger = logging.getLogger(__name__)


def key_id_of(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def public_pem(public: Ed25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def generate(private_path: Path, public_dir: Path) -> str:
    """Write a new private key (0600 where supported) + its public key <key_id>.pem. Returns the key_id."""
    key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    try:
        private_path.chmod(0o600)
    except OSError:  # pragma: no cover (Windows)
        pass
    kid = key_id_of(key.public_key())
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / f"{kid}.pem").write_bytes(public_pem(key.public_key()))
    return kid


@lru_cache
def _load(path: str) -> Ed25519PrivateKey:
    from app.config import get_settings

    p = Path(path)
    if not p.exists():
        s = get_settings()
        if s.app_env == "production":
            raise RuntimeError("AUDIT_SIGNING_KEY_PATH does not exist; run scripts/generate_audit_key.py")
        kid = generate(p, s.audit_public_keys_dirs[-1])
        logger.warning("audit.dev_key_generated key_id=%s (development only; use scripts/generate_audit_key.py)", kid)
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def signing_key() -> tuple[Ed25519PrivateKey, str]:
    from app.config import get_settings

    key = _load(str(get_settings().audit_signing_key_path))
    pub = key.public_key()
    kid = key_id_of(pub)
    for d in get_settings().audit_public_keys_dirs:  # make sure the public half is findable for verification
        if (d / f"{kid}.pem").exists():
            break
    else:
        d = get_settings().audit_public_keys_dirs[-1]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{kid}.pem").write_bytes(public_pem(pub))
    return key, kid


def sign(data: bytes) -> tuple[str, str]:
    key, kid = signing_key()
    return base64.b64encode(key.sign(data)).decode(), kid


def load_public(key_id: str, dirs: list[Path]) -> Ed25519PublicKey | None:
    for d in dirs:
        p = Path(d) / f"{key_id}.pem"
        if p.exists():
            key = serialization.load_pem_public_key(p.read_bytes())
            if isinstance(key, Ed25519PublicKey):
                return key
    return None


def verify(key_id: str, data: bytes, signature_b64: str, dirs: list[Path]) -> bool:
    key = load_public(key_id, dirs)
    if key is None:
        return False
    try:
        key.verify(base64.b64decode(signature_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False
