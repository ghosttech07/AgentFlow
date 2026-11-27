"""Envelope encryption (Part 12): a per-workflow AES-256-GCM data key, wrapped by the master key.

  master key  : AUDIT_MASTER_KEY (base64, 32 bytes). Development: generated once at AUDIT_MASTER_KEY_PATH (gitignored);
                production refuses to start without it.
  data key    : random 32 bytes per workflow, stored wrapped in workflow_keys (AES-GCM under the master key, AAD =
                the workflow id). Deleting that row = crypto-shredding: every ciphertext of the workflow is gone.
  ciphertexts : AES-GCM(data key, 12-byte random nonce, AAD = workflow id + purpose).
  value HMACs : HMAC-SHA256(master, normalized value) to find an erasure subject without decrypting anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import uuid
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.privacy import WorkflowKey

logger = logging.getLogger(__name__)


class ShreddedError(Exception):
    """The workflow's data key was deleted (crypto-shredded): its stored content can never be read again."""


@lru_cache
def master_key() -> bytes:
    from app.config import get_settings

    s = get_settings()
    if s.audit_master_key is not None:
        key = base64.b64decode(s.audit_master_key.get_secret_value())
        if len(key) != 32:
            raise RuntimeError("AUDIT_MASTER_KEY must be 32 bytes (base64)")
        return key
    if s.app_env == "production":
        raise RuntimeError("AUDIT_MASTER_KEY is required when APP_ENV=production")
    path = s.audit_master_key_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64encode(os.urandom(32)))
        logger.warning("privacy.dev_master_key_generated (development only; set AUDIT_MASTER_KEY in production)")
    return base64.b64decode(path.read_bytes().strip())


def value_hmac(value: str) -> str:
    norm = re.sub(r"[\s\-]", "", value.strip().lower())
    return hmac.new(master_key(), norm.encode(), hashlib.sha256).hexdigest()


async def data_key(session: AsyncSession, workflow_id: uuid.UUID, create: bool = True) -> bytes:
    row = await session.get(WorkflowKey, workflow_id)
    if row is None:
        if not create:
            raise ShreddedError(f"no data key for workflow {workflow_id} (shredded or never stored)")
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(master_key()).encrypt(nonce, key, str(workflow_id).encode())
        session.add(WorkflowKey(workflow_id=workflow_id, wrapped_key=wrapped))
        await session.flush()
        return key
    blob = row.wrapped_key
    return AESGCM(master_key()).decrypt(blob[:12], blob[12:], str(workflow_id).encode())


def encrypt(key: bytes, plaintext: str, aad: str) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext.encode(), aad.encode())


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: str) -> str:
    return AESGCM(key).decrypt(nonce, ciphertext, aad.encode()).decode()


def encrypt_master(plaintext: str, aad: str) -> tuple[bytes, bytes]:
    return encrypt(master_key(), plaintext, aad)


def decrypt_master(nonce: bytes, ciphertext: bytes, aad: str) -> str:
    return decrypt(master_key(), nonce, ciphertext, aad)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
