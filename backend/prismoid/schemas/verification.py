# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Verification - the report that makes a green check mean something.

"Signed" is a claim with a definition: the type suite **re-derived the work** in a fresh sandbox, a
judge seated as far from the maker as the host allows (`judged` names the seat) read it against the
task's criteria, and the whole report is Ed25519-signed so it cannot be quietly edited afterwards.
The tamper test is what turns that sentence into a fact.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prismoid.schemas.model_class import Family, JudgeFamily
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.qa import QAFinding


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # a check that could not run says so; it never silently counts as a pass


class CheckKind(StrEnum):
    """What a check looked at: the plumbing around the work, or what the work claims."""

    STRUCTURAL = (
        "structural"  # the bytes, the sources, the suite itself, well-formedness: true of a fabrication too
    )
    SUBSTANTIVE = "substantive"  # a claim, a figure, a quote: only these can carry a PASS


# The namespaces whose checks are structural: how a check stored before kinds (old suite JSON, old reports) is read.
STRUCTURAL_NAMESPACES: Final = ("bytes:", "sources:", "suite:", "format:")


class Check(BaseModel):
    """One deterministic assertion against the published bytes."""

    model_config = ConfigDict(frozen=True)

    name: str  # "recompute:Sheet1!B7", "cross-foot:totals", "claim→source:para 3"
    status: CheckStatus
    detail: str = ""
    expected: str | None = None
    actual: str | None = None
    kind: CheckKind

    @model_validator(mode="before")
    @classmethod
    def _undeclared_kind(cls, data: object) -> object:
        """A check stored before kinds (old suite JSON, a report signed before kinds) is read from its
        namespace; every suite now declares its kind."""
        if not isinstance(data, dict):
            return data
        raw = cast("dict[str, object]", data)
        if "kind" in raw:
            return raw
        structural = str(raw.get("name", "")).startswith(STRUCTURAL_NAMESPACES)
        return {**raw, "kind": CheckKind.STRUCTURAL if structural else CheckKind.SUBSTANTIVE}


# The code suite's receipt check: its `actual` is a `CodeReceipt`; skipped, it means untested, which no judge lifts.
TESTS_RUN: Final = "tests:run"


class JUnitRead(StrEnum):
    """Whether a test run's JUnit report was read."""

    JUNIT = "junit"  # read: the counts are the report's
    NONE = "none"  # the run wrote none
    UNREADABLE = "unreadable"  # it wrote one the suite could not parse


class FailedTest(BaseModel):
    """One failing or erroring testcase, by its JUnit name (`classname::name`)."""

    model_config = ConfigDict(frozen=True)

    name: str
    message: str = ""


class CodeReceipt(BaseModel):
    """One run of a code deliverable's tests in the verifier's frame. The counts are the JUnit report's,
    None when `report` is not `junit`."""

    model_config = ConfigDict(frozen=True)

    command: str
    exit_code: int | None  # None: the run was cut at the suite's time cap
    report: JUnitRead
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    failures: list[FailedTest] = Field(default_factory=list[FailedTest])
    output: str = ""  # the tail of the run's output


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIED = "unverified"  # ships only with an explicit mark


class JudgeVerdict(BaseModel):
    """**What the judging model is asked for - and notice what is not in it.**

    No `model`, no `family`. The system knows which model answered (the ledger recorded it),
    and asking the model to state its own name is asking it to duplicate a fact we already hold and
    to be wrong about it. Same lesson as `Narration.published` and the artifact's `sources`: three
    times now, in three different places, and it is the same lesson each time.
    """

    verdict: Verdict
    rationale: str = Field(min_length=1)
    criteria_met: list[str] = Field(default_factory=list)
    criteria_missed: list[str] = Field(default_factory=list)

    # The M2.1b hand-off. The deterministic suite can prove a figure is *derivable*; it cannot
    # prove the derivation means anything - `(R&D − lease) / R&D` is legal arithmetic over
    # meaningless quantities. A reader can see that in a second. This is where it says so.
    bad_derivations: list[str] = Field(default_factory=list)
    # Each defect where a rebuild can find it, with the one change that fixes it.
    findings: list[QAFinding] = Field(default_factory=list[QAFinding], max_length=40)


class JudgeOpinion(BaseModel):
    """The judge's half as recorded, built by `operations`: the verdict and reasoning are the judge's,
    the identity the ledger's; how it stands to the maker is the report's `judged`."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    rationale: str
    model: str
    family: Family
    criteria_met: list[str] = Field(default_factory=list)
    criteria_missed: list[str] = Field(default_factory=list)
    bad_derivations: list[str] = Field(default_factory=list)


# 1: the report before checks were typed, stored without the key. 2: every check carries its `kind`.
REPORT_SCHEMA_VERSION: Final = 2


class VerificationReport(BaseModel):
    """What gets signed. Content-addressed to the artifact it describes."""

    model_config = ConfigDict(frozen=True)

    artifact_sha256: str
    suite: VerifierSuite
    checks: list[Check] = Field(default_factory=list[Check])
    judge: JudgeOpinion | None = None
    verdict: Verdict
    producer_model: str
    producer_family: Family
    attempt: int = 1
    at: dt.datetime
    schema_version: int = REPORT_SCHEMA_VERSION

    @classmethod
    def stored(cls, raw: dict[str, object]) -> VerificationReport:
        """A report as a row holds it: one stored without `schema_version` is a version 1 report."""
        return cls.model_validate({"schema_version": 1, **raw})

    @property
    def judged(self) -> JudgeFamily:
        """Which seat judged the work, read from the signed models: other family, same family,
        self-checked, or no judge."""
        if self.judge is None:
            return JudgeFamily.NONE
        return JudgeFamily.between(
            maker=self.producer_model,
            maker_family=self.producer_family,
            judge=self.judge.model,
            judge_family=self.judge.family,
        )

    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]


SIGNING_ALGORITHM: Final = "ed25519"


class SignedVerification(BaseModel):
    """The report plus its signature. `signature` covers the canonical JSON of `report`.

    Re-verification recomputes that canonical form and checks the signature - which is why an
    `UPDATE verifications SET report = …` in psql is detectable, and is detected.
    """

    model_config = ConfigDict(frozen=True)

    report: VerificationReport
    signature: str  # base64
    public_key: str  # base64 - the key that signed it, so an auditor needs nothing from us
    algorithm: str = SIGNING_ALGORITHM

    def canonical_payload(self) -> bytes:
        """The exact bytes signed, one definition for signer and verifier; a version 1 report's bytes
        leave out what its shape did not have."""
        if self.report.schema_version == 1:
            legacy: Any = {"schema_version": True, "checks": {"__all__": {"kind"}}}
            return self.report.model_dump_json(exclude_none=False, exclude=legacy).encode()
        return self.report.model_dump_json(exclude_none=False).encode()


class Diagnosis(BaseModel):
    """Why we gave up, written for the human who has to decide what to do next.

    An escalation without a diagnosis is a shrug with a stack trace attached.
    """

    model_config = ConfigDict(frozen=True)

    what_failed: str
    why: str
    attempts: int
    evidence: list[str] = Field(default_factory=list)  # frame paths, check names, quotes
    suggested_next: list[str] = Field(default_factory=list)
