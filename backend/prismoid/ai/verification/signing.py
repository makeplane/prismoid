# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Ed25519 over the canonical verification report - the thing that makes "signed" a claim.

A verdict stored in a row you can `UPDATE` is not a verdict, it is a note. Anyone with database
access - a bug, a migration, a person having a bad day - can turn a `fail` into a `pass`, and
nothing downstream will ever know. **The signature is what makes that edit *detectable*.**

The property, precisely: the signature covers the canonical JSON of the report, and re-verification
**recomputes those bytes from the stored report and checks them**. It never trusts a stored boolean.
So an `UPDATE verifications SET report = …` in psql does not produce a lie with a green tick - it
produces `signature_valid: false`, and the tamper test is what turns that sentence into a
fact rather than an intention.

**One definition of the signed bytes, used by both signer and verifier.** Two definitions is exactly
how signature schemes rot: they agree in every test and disagree on the one field somebody added
later. `SignedVerification.canonical_payload()` is that definition, and it lives with the schema.

The engine's *deterministic* half (ADR-001): no model, no database, no I/O but a key file. Testable in microseconds with nothing stood up.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

import logfire
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from prismoid.ai import config
from prismoid.schemas.verification import SIGNING_ALGORITHM, SignedVerification, VerificationReport


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    """The one signing key, resolved once: config, then a file, then generated and persisted; never
    a default or derived key (a predictable key makes forgeable ticks)."""
    signing = config.current().signing
    configured = signing.private_key.get_secret_value()
    if configured:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(configured))

    path = Path(signing.key_path)
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_bytes()))

    # First run on this machine. Generate, persist 0600, and say so loudly - a key that lives in a
    # file the operator did not choose is a key the operator must be told about.
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    path.write_bytes(base64.b64encode(seed))
    path.chmod(0o600)
    logfire.warn(
        "generated an Ed25519 signing key at {path} - set PRISMOID_SIGNING_PRIVATE_KEY in production",
        path=str(path),
    )
    return key


def public_key() -> str:
    """base64. Ours - the trust anchor. It also travels *inside* each report, as a label."""
    return base64.b64encode(_private_key().public_key().public_bytes_raw()).decode()


def sign(report: VerificationReport) -> SignedVerification:
    """Sign the canonical bytes of the report. The signature covers the whole thing, verdict included."""
    unsigned = SignedVerification(report=report, signature="", public_key=public_key())
    signature = _private_key().sign(unsigned.canonical_payload())
    return unsigned.model_copy(update={"signature": base64.b64encode(signature).decode()})


def is_valid(signed: SignedVerification) -> bool:
    """Recompute the signature over the stored report against OUR public key, never the one in the row:
    a key carried in the document is a label, and trusting it lets anyone re-sign an edited verdict."""
    if not signed.signature or signed.algorithm != SIGNING_ALGORITHM:
        return False

    trusted = public_key()
    if signed.public_key != trusted:
        return (
            False  # signed by a key we do not hold: not a forgery we can detect - a report that is not ours
        )

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted))
        key.verify(base64.b64decode(signed.signature), signed.canonical_payload())
    except (InvalidSignature, ValueError):
        return False
    return True
