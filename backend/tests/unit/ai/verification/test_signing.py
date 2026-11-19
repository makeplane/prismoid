# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Ed25519 over the verification report - and the forgeries it must refuse.

A verdict stored in a row you can `UPDATE` is not a verdict, it is a note. Anyone with database
access - a bug, a migration, a person having a bad day - can turn a `fail` into a `pass`, and
nothing downstream will ever know. The signature is what makes that edit **detectable**, and these
tests are what make that sentence a fact rather than an intention.
"""

from __future__ import annotations

import base64
import datetime as dt

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prismoid.ai.verification import signing
from prismoid.schemas.model_class import Family
from prismoid.schemas.verification import (
    Check,
    CheckKind,
    CheckStatus,
    JudgeOpinion,
    SignedVerification,
    Verdict,
    VerificationReport,
)


def a_report(verdict: Verdict = Verdict.PASS) -> VerificationReport:
    return VerificationReport(
        artifact_sha256="a" * 64,
        suite="document-suite",  # pyright: ignore[reportArgumentType]
        checks=[
            Check(
                name="numbers:grounded",
                status=CheckStatus.PASS,
                kind=CheckKind.SUBSTANTIVE,
                detail="16 verbatim",
            )
        ],
        judge=JudgeOpinion(
            verdict=verdict, rationale="reads true", model="openai:big", family=Family("openai")
        ),
        verdict=verdict,
        producer_model="anthropic:small",
        producer_family=Family("anthropic"),
        at=dt.datetime(2026, 7, 14, tzinfo=dt.UTC),
    )


def test_a_signed_report_verifies() -> None:
    assert signing.is_valid(signing.sign(a_report()))


def test_the_signature_survives_a_round_trip_through_the_database() -> None:
    """**The one that would silently break in production.**

    The row is JSONB, and Postgres does not preserve key order in a JSON object. If the signature
    covered the *stored bytes* it would fail on every read. It covers the canonical serialisation of
    the re-parsed model - so the order is restored by the schema, not by the database, and the two
    can never drift.
    """
    signed = signing.sign(a_report())

    # Exactly what `latest_for` does: JSONB → dict → model → SignedVerification.
    stored = signed.report.model_dump(mode="json")
    shuffled = dict(reversed(list(stored.items())))  # Postgres is free to do this and does
    revived = SignedVerification(
        report=VerificationReport.stored(shuffled),
        signature=signed.signature,
        public_key=signed.public_key,
    )
    assert signing.is_valid(revived)


# ── the tamper test ────────────────────────────────────────────────────────────────────────────


def test_flipping_the_verdict_invalidates_the_signature() -> None:
    """`UPDATE verifications SET report = jsonb_set(report, '{verdict}', '"pass"')`.

    The single most valuable line of SQL an attacker could write, and the single most likely
    accident a migration could commit. It must not produce a lie with a green tick.
    """
    signed = signing.sign(a_report(Verdict.FAIL))
    assert signing.is_valid(signed)

    tampered = signed.model_copy(
        update={"report": signed.report.model_copy(update={"verdict": Verdict.PASS})}
    )
    assert not signing.is_valid(tampered)


def test_editing_a_single_check_invalidates_the_signature() -> None:
    """The signature covers the *whole* report, not just the verdict - so quietly turning a failing
    check into a passing one is caught too, even though the verdict never moved."""
    signed = signing.sign(a_report())
    checks = [
        Check(
            name="numbers:grounded", status=CheckStatus.FAIL, kind=CheckKind.SUBSTANTIVE, detail="16 verbatim"
        )
    ]
    tampered = signed.model_copy(update={"report": signed.report.model_copy(update={"checks": checks})})
    assert not signing.is_valid(tampered)


def test_rewriting_the_judges_rationale_invalidates_the_signature() -> None:
    """A judge's reasoning is part of what was signed. Changing what it *said* it found is exactly
    as detectable as changing what it found."""
    signed = signing.sign(a_report())
    judge = signed.report.judge
    assert judge is not None
    edited = judge.model_copy(update={"rationale": "actually it was fine"})
    tampered = signed.model_copy(update={"report": signed.report.model_copy(update={"judge": edited})})
    assert not signing.is_valid(tampered)


def test_a_forged_signature_from_someone_elses_key_is_refused() -> None:
    """**The hole that nearly shipped, and the reason this file has a paragraph about trust anchors.**

    The first version verified against `signed.public_key` - the key carried *in the report*. That
    is a complete forgery hole, because the signature covers the report and not the envelope around
    it: an attacker edits the verdict, signs the edited report with **their own** key, writes their
    own public key into the row, and every check passes. The signature really does verify - against
    the key they chose. All it proves is that whoever wrote the row owned *a* key.

    **A public key inside a document is a label, not a trust anchor.** The signature is checked
    against the key we hold, and a report bearing any other key is not ours, however impeccably it
    is signed.
    """
    attacker = Ed25519PrivateKey.generate()
    forged_report = a_report(Verdict.PASS)

    unsigned = SignedVerification(
        report=forged_report,
        signature="",
        public_key=base64.b64encode(attacker.public_key().public_bytes_raw()).decode(),
    )
    forged = unsigned.model_copy(
        update={"signature": base64.b64encode(attacker.sign(unsigned.canonical_payload())).decode()}
    )

    # It is internally consistent - a naive verifier would wave it through.
    attacker.public_key().verify(base64.b64decode(forged.signature), forged.canonical_payload())

    # And it is not ours.
    assert not signing.is_valid(forged)


@pytest.mark.parametrize("field", ["signature", "public_key"])
def test_a_blanked_field_is_not_valid(field: str) -> None:
    signed = signing.sign(a_report())
    assert not signing.is_valid(signed.model_copy(update={field: ""}))


def test_a_swapped_algorithm_is_refused() -> None:
    """`algorithm: "none"` is the oldest trick in the JWT playbook. It does not work here, because
    the algorithm is checked before anything else is."""
    signed = signing.sign(a_report())
    assert not signing.is_valid(signed.model_copy(update={"algorithm": "none"}))
