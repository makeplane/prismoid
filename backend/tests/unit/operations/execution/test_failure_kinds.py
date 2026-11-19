# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A dead attempt's kind decides its direction and the advice a stopped step shows; the words of
the error never do. Each row of `schemas.failures.ADVICE` is checked through the ladder's own
readers (`infra.booked`, `produce_diagnosis`), and the kinds are named from typed facts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError

from prismoid.ai.agents.run import model_incapable
from prismoid.ai.tools.providers import images
from prismoid.errors import ModelIncapable
from prismoid.operations.execution import infra
from prismoid.operations.execution.finish import produce_diagnosis
from prismoid.operations.execution.infra import Death
from prismoid.schemas.failures import ADVICE, NARROWER, FailureKind
from prismoid.schemas.outputs import ArtifactType

_DOWN = Death(infra=True, counted=True, backoff=True)
_INFRA = Death(infra=True, counted=True, backoff=False)
_MODEL = Death(infra=False, counted=False, backoff=False)

# kind -> (how the death is booked, escalate now, never re-sent, words the first suggestion holds)
_ROWS: dict[FailureKind, tuple[Death, bool, bool, str]] = {
    FailureKind.MODEL_FAILED: (_MODEL, False, False, "fresh attempt"),
    FailureKind.REQUEST_REFUSED: (_MODEL, False, True, "fresh attempt"),
    FailureKind.KEY_REFUSED: (_DOWN, False, False, "refused this deployment's API key"),
    FailureKind.PROVIDER_DOWN: (_DOWN, False, False, "fresh attempt"),
    FailureKind.SERVICE_DOWN: (_INFRA, False, False, "fresh attempt"),
    FailureKind.WALL_STOP: (_INFRA, False, False, "fresh attempt"),
    FailureKind.SITE_UNREACHABLE: (_MODEL, True, False, "could not be reached from the computer"),
    FailureKind.SITE_REFUSES_BROWSER: (_MODEL, True, False, "turns away automated browsers"),
    FailureKind.SITE_CHALLENGED: (_MODEL, True, False, "bot check"),
}


def _advice(failure: FailureKind | None) -> list[str]:
    return produce_diagnosis(None, attempts=1, gotchas=["- x"], failure=failure).suggested_next


def test_every_kind_has_a_row() -> None:
    assert set(_ROWS) == set(FailureKind) == set(ADVICE)


@pytest.mark.parametrize("kind", list(FailureKind))
def test_each_kind_steers_the_ladder_and_the_advice(kind: FailureKind) -> None:
    booked, final, refused, said = _ROWS[kind]
    death = ModelIncapable("the same words for every kind", kind=kind)
    assert infra.booked(death) == booked
    assert bool(death.context.get("final")) is final
    assert bool(death.context.get("refused")) is refused
    advice = _advice(kind)
    assert said in advice[0]
    # No model ran on a refused key: the key is the whole advice, never "a narrower goal".
    assert advice == [advice[0]] if kind is FailureKind.KEY_REFUSED else advice == [advice[0], NARROWER]


def test_a_stop_that_is_no_attempts_death_gets_only_the_general_advice() -> None:
    assert _advice(None) == [NARROWER]


def _http(status: int, body: object = None) -> ModelHTTPError:
    return ModelHTTPError(status, "claude-x", body)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (_http(401), FailureKind.KEY_REFUSED),
        (FallbackExceptionGroup("all", [_http(401), _http(403)]), FailureKind.KEY_REFUSED),
        (FallbackExceptionGroup("all", [_http(401), ValueError("no status")]), FailureKind.KEY_REFUSED),
        (FallbackExceptionGroup("all", [_http(401), _http(529)]), FailureKind.PROVIDER_DOWN),
        # The reason quotes only the first three candidates; the kind reads all of them.
        (
            FallbackExceptionGroup("all", [_http(401), _http(401), _http(403), _http(529)]),
            FailureKind.PROVIDER_DOWN,
        ),
        (FallbackExceptionGroup("all", [_http(400)]), FailureKind.PROVIDER_DOWN),
        (_http(413), FailureKind.REQUEST_REFUSED),
        (_http(529), FailureKind.PROVIDER_DOWN),
    ],
)
def test_a_provider_failure_is_named_by_its_statuses(exc: Exception, kind: FailureKind) -> None:
    assert model_incapable(exc, guarded=True).kind is kind


def test_the_words_of_an_error_never_decide() -> None:
    """The bite: a 529 whose body quotes a 401 and an unreachable site is still an outage, and its
    advice is an outage's; a parser over the message would call it a refused key or a dead site."""
    lying = {"error": "status_code: 401 - the site could not be reached from here"}
    death = model_incapable(_http(529, lying), guarded=False)
    assert "status_code: 401" in str(death) and "could not be reached from here" in str(death)
    assert death.kind is FailureKind.PROVIDER_DOWN
    advice = _advice(death.kind)
    assert not any("API key" in line or "from the computer" in line for line in advice)
    assert produce_diagnosis(None, attempts=1, gotchas=[str(death)], failure=death.kind).what_failed == (
        "Prismoid could not finish this part of the task"
    )


# ── the image wires: a fallback decided by the provider's typed error ──────────────────────────


def _wire(protocol: str) -> Any:
    wire = images.wire_for(protocol)
    assert wire is not None
    return wire


def _candidate(protocol: str) -> images.ImageCandidate:
    async def emit(units: dict[str, int]) -> None:
        return None

    return images.ImageCandidate("p", "m", protocol, None, "key", emit)


def _bad_request(param: str | None) -> Exception:
    from openai import BadRequestError

    request = httpx2.Request("POST", "http://images.test/v1/images/generations")
    body = {"message": "size is not one we draw", "param": param}
    return BadRequestError("refused", response=httpx2.Response(400, request=request), body=body)


@pytest.mark.parametrize(("param", "calls"), [("size", 2), (None, 2), ("prompt", 1)])
async def test_an_openai_wire_drops_size_only_when_the_refusal_may_be_about_it(
    monkeypatch: pytest.MonkeyPatch, param: str | None, calls: int
) -> None:
    sent: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> Any:
        sent.append(kwargs)
        if len(sent) == 1:
            raise _bad_request(param)
        return SimpleNamespace(data=[])

    fake = SimpleNamespace(images=SimpleNamespace(generate=generate))
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_: fake)
    if calls == 1:
        with pytest.raises(Exception, match="refused"):
            await _wire("openai_images")(_candidate("openai_images"), "a cat", "square", 1)
    else:
        assert await _wire("openai_images")(_candidate("openai_images"), "a cat", "square", 1) == []
        assert "size" in sent[0] and "size" not in sent[1]
    assert len(sent) == calls


@pytest.mark.parametrize(("code", "falls_through"), [(404, True), (400, True), (403, False)])
async def test_a_google_predict_the_api_refuses_falls_through_to_generate_content(
    monkeypatch: pytest.MonkeyPatch, code: int, falls_through: bool
) -> None:
    from google.genai import errors

    content: list[str] = []

    async def generate_images(**_: Any) -> Any:
        raise errors.ClientError(code, {"error": {"code": code, "message": "x", "status": "S"}})

    async def generate_content(**kwargs: Any) -> Any:
        content.append(kwargs["model"])
        return SimpleNamespace(candidates=[])

    models = SimpleNamespace(generate_images=generate_images, generate_content=generate_content)
    monkeypatch.setattr(
        "google.genai.Client", lambda **_: SimpleNamespace(aio=SimpleNamespace(models=models))
    )
    if falls_through:
        assert await _wire("google_images")(_candidate("google_images"), "a cat", "square", 1) == []
        assert content == ["m"]
    else:
        with pytest.raises(errors.ClientError):
            await _wire("google_images")(_candidate("google_images"), "a cat", "square", 1)
        assert content == []


# ── what the person reads: product words, never the retry's brief ─────────────────────────────

_RETRY_BRIEF = (
    "- **nothing was published.** the contract required a spreadsheet and nothing was published - "
    "write the file to /work and call publish(type=...)"
)
_ENGINE_WORDS = ("/work", "publish(", "type=", "maker", "operator", "agent", "contract", "/skills/", "/task/")


@pytest.mark.parametrize("kind", [None, *FailureKind])
def test_a_stopped_step_tells_the_person_why_in_product_words(kind: FailureKind | None) -> None:
    """The rungs' findings are written for the next attempt (paths, tool calls); the card a person reads
    says what did not happen and why, from the death's kind, and keeps those findings as evidence."""
    diagnosis = produce_diagnosis(ArtifactType.SPREADSHEET, attempts=3, gotchas=[_RETRY_BRIEF], failure=kind)
    said = " ".join([diagnosis.what_failed, diagnosis.why, *diagnosis.suggested_next])
    assert not any(word in said for word in _ENGINE_WORDS), said
    assert diagnosis.why and diagnosis.evidence == [_RETRY_BRIEF]
    if kind is None or not ADVICE[kind].final:
        assert diagnosis.what_failed == "Prismoid could not make the spreadsheet"
