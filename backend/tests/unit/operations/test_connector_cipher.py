# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connector cipher (ADR-020) - the first reversible secret store, pinned.

What must stay true: the round trip is exact; the key ladder is config → file → generate-0600;
rotation decrypts old ciphertext while encrypting with the newest key; tampering surfaces as
`CipherError`, never a raw library exception; ciphertext wears the `gAAAAA` prefix the scrub
patterns key on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from prismoid.conf import settings
from prismoid.operations import cipher


@pytest.fixture(autouse=True)
def fresh_cipher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Every test resolves its own key: clear the lru_cache and point the ladder at a sandbox."""
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(""))
    monkeypatch.setattr(settings.connectors, "key_path", str(tmp_path / "key"))
    cipher.reset()
    yield
    cipher.reset()


def test_round_trip_is_exact_and_prefixed():
    token = cipher.encrypt("Bearer super-secret-token")
    assert token.startswith("gAAAAA"), "the scrub patterns key on Fernet's fixed prefix"
    assert "super-secret" not in token
    assert cipher.decrypt(token) == "Bearer super-secret-token"


def test_ladder_config_wins(monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(key))
    cipher.reset()
    token = cipher.encrypt("hello")
    assert Fernet(key.encode()).decrypt(token.encode()) == b"hello", "config key is THE key"
    assert not Path(settings.connectors.key_path).exists(), "a configured key writes no file"


def test_ladder_generates_once_at_0600():
    cipher.encrypt("first use generates")
    path = Path(settings.connectors.key_path)
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    first = path.read_bytes()
    cipher.reset()  # a fresh process
    assert cipher.decrypt(cipher.encrypt("again")) == "again"
    assert path.read_bytes() == first, "the second process reads the same file, never regenerates"


def test_rotation_decrypts_old_encrypts_new(monkeypatch: pytest.MonkeyPatch):
    old = Fernet.generate_key()
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(old.decode()))
    cipher.reset()
    stale = cipher.encrypt("minted under the old key")

    new = Fernet.generate_key()
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(f"{new.decode()},{old.decode()}"))
    cipher.reset()
    assert cipher.decrypt(stale) == "minted under the old key", "rotation must not orphan rows"
    fresh = cipher.encrypt("minted under the new key")
    assert Fernet(new).decrypt(fresh.encode()) == b"minted under the new key", "newest key encrypts"


def test_tamper_and_wrong_key_surface_as_cipher_error(monkeypatch: pytest.MonkeyPatch):
    token = cipher.encrypt("intact")
    with pytest.raises(cipher.CipherError):
        cipher.decrypt(token[:-4] + "AAAA")
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(Fernet.generate_key().decode()))
    cipher.reset()
    with pytest.raises(cipher.CipherError):
        cipher.decrypt(token)


def test_key_id_is_the_settings_label(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.connectors, "key_id", "v7")
    assert cipher.key_id() == "v7"
