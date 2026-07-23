"""AES-256 (Fernet) encryption helpers for storing sensitive credentials.

Usage:
    from app.core.encryption import encrypt, decrypt

    ciphertext = encrypt(plaintext_token)
    plaintext  = decrypt(ciphertext)

When CREDENTIAL_ENCRYPTION_KEY is not configured (local dev without secrets),
both functions are pass-through no-ops so the app still works.

Key generation (run once, store result in .env):
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet | None:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = getattr(settings, "credential_encryption_key", "") or ""
    if not key:
        return None
    try:
        _fernet = Fernet(key.encode())
        return _fernet
    except Exception as exc:
        logger.error("Invalid CREDENTIAL_ENCRYPTION_KEY: %s", exc)
        return None


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns the original value unchanged
    when no encryption key is configured (dev mode)."""
    if not plaintext:
        return plaintext
    fernet = _get_fernet()
    if fernet is None:
        return plaintext
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns the original value unchanged
    when no encryption key is configured (dev mode)."""
    if not ciphertext:
        return ciphertext
    fernet = _get_fernet()
    if fernet is None:
        return ciphertext
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt value — token invalid or key mismatch")
        return ciphertext
