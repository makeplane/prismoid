# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connector cipher - the first reversible secret store in the product (ADR-020).

Everything else at rest is hashed (passwords argon2id, session/API tokens sha256) because
nothing ever needs the plaintext back. OAuth tokens are different: their whole point is to be
presented to someone else's server later, so they must come back out. Fernet (AES + HMAC,
versioned, misuse-resistant - no nonce management to get wrong) over a key resolved exactly
like the signing key: config first, then a file, then generate-once-0600 and say so loudly.

Rotation is designed in, not bolted on (the signing module's recorded regret, see signing.py):
`PRISMOID_CONNECTORS_ENCRYPTION_KEY` takes a comma-separated list - MultiFernet
encrypts with the first and decrypts with any - and every ciphertext row carries `key_id()`
so an operator can see what a re-encryption sweep still owes.

Lives in `operations/` because its only callers are operations (encrypt before a row write,
decrypt after a row read): `ai/` is persistence-free and must never see a key or a ciphertext,
and `db/models` holds rows, not I/O.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import logfire
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from prismoid.conf import settings

__all__ = ["CipherError", "decrypt", "decrypt_bytes", "encrypt", "encrypt_bytes", "key_id", "reset"]


class CipherError(Exception):
    """Ciphertext that will not open: a wrong key, a truncated column, a tampered row."""


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    """The one cipher, resolved once. Config first; then a file; then generate and persist.

    **Never a default or a derived key.** A predictable cipher key is worse than none at all:
    it reads as "encrypted at rest" while anyone holding the repo can open every token.
    """
    configured = settings.connectors.encryption_key.get_secret_value()
    if configured:
        return MultiFernet([Fernet(k.strip().encode()) for k in configured.split(",") if k.strip()])

    path = Path(settings.connectors.key_path)
    if path.exists():
        return MultiFernet([Fernet(path.read_bytes().strip())])

    # First run on this machine. Generate, persist 0600, and say so loudly - a key that lives
    # in a file the operator did not choose is a key the operator must be told about.
    key = Fernet.generate_key()
    path.write_bytes(key)
    path.chmod(0o600)
    logfire.warn(
        "generated a connector encryption key at {path} - set PRISMOID_CONNECTORS_ENCRYPTION_KEY in production",
        path=str(path),
    )
    return MultiFernet([Fernet(key)])


def reset() -> None:
    """Drop the resolved cipher so the ladder runs again - the seam tests re-point the key
    through. A process never needs this; a test that changes `settings.connectors` does."""
    _fernet.cache_clear()


def key_id() -> str:
    """The label stamped beside every ciphertext, so rotation is observable."""
    return settings.connectors.key_id


def encrypt(plaintext: str) -> str:
    """str → Fernet token (`gAAAAA…`). The scrub patterns know that prefix; logs see neither side."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Fernet token → str, or `CipherError` - never a raw `InvalidToken` leaking library innards."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as bad:
        raise CipherError("connector ciphertext would not open - wrong key or tampered row") from bad


def encrypt_bytes(data: bytes) -> bytes:
    """Bytes in, a Fernet token out - for archives, where a str round trip would cost a
    base64 layer twice. The same keys and rotation as `encrypt`."""
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(token)
    except InvalidToken as bad:
        raise CipherError("archive ciphertext would not open - wrong key or tampered object") from bad
