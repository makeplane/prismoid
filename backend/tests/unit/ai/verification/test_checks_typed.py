# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A check says what it looked at, and the report says which shape it was signed in.

`Check.kind` decides whether a pass can carry a verdict: the field, not the check's name. A check that
does not declare it (a suite's JSON, a report signed before kinds) is read from its namespace. Every
report carries `schema_version`; one stored without it is a version 1 report, and the bytes it was
signed over are rebuilt exactly, so a receipt signed before checks were typed still verifies.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prismoid.ai.verification import signing
from prismoid.ai.verification.pipeline import verdict_of
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import (
    REPORT_SCHEMA_VERSION,
    Check,
    CheckKind,
    CheckStatus,
    SignedVerification,
    Verdict,
    VerificationReport,
)

KEY = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)

# A report as the signer wrote it before checks were typed: the exact bytes, and their signature by KEY.
LEGACY_BYTES = (
    '{"artifact_sha256":"abababababababababababababababababababababababababababababababab",'
    '"suite":"document-suite","checks":[{"name":"numbers:grounded","status":"pass","detail":"16 verbatim",'
    '"expected":null,"actual":"x"},{"name":"bytes:sha256","status":"pass","detail":"","expected":null,'
    '"actual":null}],"judge":{"verdict":"pass","rationale":"ok","model":"openai:big","family":"openai",'
    '"criteria_met":["a"],"criteria_missed":[],"bad_derivations":[]},"verdict":"pass",'
    '"producer_model":"anthropic:big","producer_family":"anthropic","attempt":1,"at":"2026-11-01T12:00:00Z"}'
)
LEGACY_SIGNATURE = "T5QZmCGaDUZD32QGSa3hooP+Wn1Z4VXrEHqeXNBjaYzS2VdLw0qYFxFwIQWXhrZFRnNjH+CKXeEuu73rjAWdAQ=="
LEGACY_PUBLIC_KEY = "6kpsY+KcUgq+9VB7Ey7F+ZVHdq6+vnuSQh7qaRRG0iw="


@pytest.fixture(autouse=True)
def fixed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signing, "_private_key", lambda: KEY)


def check(name: str, status: CheckStatus, kind: CheckKind) -> Check:
    return Check(name=name, status=status, kind=kind)


def stored_legacy(raw: str = LEGACY_BYTES) -> SignedVerification:
    """What `latest_for` hands back for a row written before checks were typed."""
    return SignedVerification(
        report=VerificationReport.stored(json.loads(raw)),
        signature=LEGACY_SIGNATURE,
        public_key=LEGACY_PUBLIC_KEY,
    )


def test_the_declared_kind_decides_never_the_name() -> None:
    assert verdict_of([check("format:docx", CheckStatus.PASS, CheckKind.SUBSTANTIVE)]) is Verdict.PASS
    assert (
        verdict_of([check("numbers:grounded", CheckStatus.PASS, CheckKind.STRUCTURAL)]) is Verdict.UNVERIFIED
    )


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("bytes:sha256", CheckKind.STRUCTURAL),
        ("sources:present", CheckKind.STRUCTURAL),
        ("suite:ran", CheckKind.STRUCTURAL),
        ("format:parse", CheckKind.STRUCTURAL),
        ("numbers:grounded", CheckKind.SUBSTANTIVE),
        ("quotes:verbatim", CheckKind.SUBSTANTIVE),
    ],
)
def test_an_undeclared_check_is_read_from_its_namespace(name: str, kind: CheckKind) -> None:
    assert Check.model_validate({"name": name, "status": "pass"}).kind is kind


def test_a_suite_may_declare_its_kind() -> None:
    declared = Check.model_validate({"name": "format:schema", "status": "pass", "kind": "substantive"})
    assert declared.kind is CheckKind.SUBSTANTIVE


def test_a_new_report_carries_its_version_and_signs_its_kinds() -> None:
    report = VerificationReport(
        artifact_sha256="cd" * 32,
        suite=VerifierSuite.DOCUMENT,
        checks=[check("numbers:grounded", CheckStatus.PASS, CheckKind.SUBSTANTIVE)],
        verdict=Verdict.PASS,
        producer_model="anthropic:big",
        producer_family=Family("anthropic"),
        at=dt.datetime(2026, 11, 14, tzinfo=dt.UTC),
    )
    signed = signing.sign(report)
    stored = json.loads(signed.report.model_dump_json())
    assert stored["schema_version"] == REPORT_SCHEMA_VERSION == 2
    assert stored["checks"][0]["kind"] == "substantive"
    revived = signed.model_copy(update={"report": VerificationReport.stored(stored)})
    assert signing.is_valid(revived)

    stored["checks"][0]["kind"] = "structural"
    retyped = signed.model_copy(update={"report": VerificationReport.stored(stored)})
    assert not signing.is_valid(retyped), "the kind is signed: retyping a check is an edit"


def test_a_report_signed_before_checks_were_typed_still_verifies() -> None:
    signed = stored_legacy()
    assert signed.report.schema_version == 1
    assert signed.canonical_payload() == LEGACY_BYTES.encode()
    assert signing.is_valid(signed)
    assert verdict_of(signed.report.checks) is Verdict.PASS  # its kinds are read from its namespaces


def test_an_edited_old_report_is_still_caught() -> None:
    assert not signing.is_valid(
        stored_legacy(LEGACY_BYTES.replace('"verdict":"pass"', '"verdict":"fail"', 1))
    )


def test_a_new_report_relabelled_old_does_not_verify() -> None:
    report = VerificationReport(
        artifact_sha256="cd" * 32,
        suite=VerifierSuite.DOCUMENT,
        checks=[check("numbers:grounded", CheckStatus.PASS, CheckKind.SUBSTANTIVE)],
        verdict=Verdict.PASS,
        producer_model="anthropic:big",
        producer_family=Family("anthropic"),
        at=dt.datetime(2026, 11, 14, tzinfo=dt.UTC),
    )
    signed = signing.sign(report)
    relabelled = signed.model_copy(update={"report": report.model_copy(update={"schema_version": 1})})
    assert not signing.is_valid(relabelled), "the version is signed: a downgrade is an edit"
