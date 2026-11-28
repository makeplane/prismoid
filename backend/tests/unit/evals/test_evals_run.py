# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The golden harness waits for the receipt (ADR-019): a delivered answer's verification is signed
after delivery by the `verify_artifact` job, and its endpoint is a 404 until then. The harness's
own `run_case` against a scripted API - no database, no worker."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from evals import run as harness
from evals.cases.golden import Golden

TASK = "7a1c9a4e-0000-4000-8000-000000000001"
ARTIFACT = "7a1c9a4e-0000-4000-8000-000000000002"


def _api(receipts: list[httpx.Response]) -> httpx.MockTransport:
    frames = [
        {"type": "step.state", "attempt": 1},
        {"type": "artifact.produced", "artifact_id": ARTIFACT},
        {"type": "task.delivered"},
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": TASK})
        if path == f"/v1/tasks/{TASK}":
            return httpx.Response(200, json={"status": "delivered", "budget": {}})
        if path == f"/v1/tasks/{TASK}/events":
            return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames))
        if path == f"/v1/tasks/{TASK}/usage":
            return httpx.Response(200, json={"by_family": {}, "total_usd": "0.10"})
        if path == f"/v1/artifacts/{ARTIFACT}/verification":
            return receipts.pop(0) if receipts else httpx.Response(404, json={"error": {"code": "not_found"}})
        if path == f"/v1/artifacts/{ARTIFACT}/download":
            return httpx.Response(200, json={"url": "https://storage.invalid/memo.md"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return httpx.MockTransport(answer)


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "POLL_S", 0.0)
    monkeypatch.setattr(harness.httpx, "get", lambda *a, **k: httpx.Response(200, text="Revenue rose 12%"))


async def test_a_case_waits_for_its_receipt_and_scores_the_signed_verdict(quick: None) -> None:
    unsigned = httpx.Response(404, json={"error": {"code": "not_found", "message": "not verified yet"}})
    signed = httpx.Response(200, json={"verdict": "pass", "verifier_family": "gpt"})
    async with httpx.AsyncClient(
        transport=_api([unsigned, unsigned, signed]), base_url="http://api"
    ) as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()))
    assert (result.verdict, result.note) == ("pass", "gpt")
    assert result.ok


async def test_a_receipt_that_never_comes_is_said_so_within_the_cases_clock(
    quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "TIMEOUT_S", 0.2)
    async with httpx.AsyncClient(transport=_api([]), base_url="http://api") as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()))
    assert result.verdict == "no receipt" and not result.ok


@pytest.mark.parametrize(("budget", "sent"), [(0.5, {"usd": 0.5}), (None, None)])
async def test_a_golden_task_carries_a_budget_only_when_one_is_given(
    quick: None, budget: float | None, sent: dict[str, float] | None
) -> None:
    posted: list[dict[str, object]] = []
    api = _api([httpx.Response(200, json={"verdict": "pass", "verifier_family": "gpt"})])

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/tasks":
            posted.append(json.loads(request.content))
        return api.handle_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()), budget)
    assert posted[0].get("budget") == sent


@pytest.mark.parametrize("deliverable", ["spreadsheet", ""])
async def test_a_golden_task_carries_its_cases_deliverable_dial(quick: None, deliverable: str) -> None:
    posted: list[dict[str, object]] = []
    api = _api([httpx.Response(200, json={"verdict": "pass", "verifier_family": "gpt"})])

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/tasks":
            posted.append(json.loads(request.content))
        return api.handle_request(request)

    case = Golden(name="model", goal="Build the model", inputs=(), deliverable=deliverable)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        await harness.run_case(client, case)
    assert posted[0].get("deliverable") == (deliverable or None)


async def test_a_task_parked_on_its_budget_is_cancelled_and_said_so(quick: None) -> None:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": TASK})
        if request.url.path == f"/v1/tasks/{TASK}":
            return httpx.Response(200, json={"status": "waiting", "budget": {"usd": "0.50"}})
        if request.url.path == "/v1/gates":
            return httpx.Response(200, json=[{"task_id": TASK, "kind": "budget"}])
        return httpx.Response(200, json={"status": "cancelled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()), 0.5)
    assert (result.verdict, result.note, result.ok) == ("over budget", harness.BUDGET_NOTE, False)
    assert f"POST /v1/tasks/{TASK}/cancel" in calls


async def test_a_task_waiting_on_a_question_is_not_taken_for_over_budget(
    quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "TIMEOUT_S", 0.05)

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": TASK})
        if request.url.path == f"/v1/tasks/{TASK}":
            return httpx.Response(200, json={"status": "waiting", "budget": {}})
        if request.url.path == "/v1/gates":
            return httpx.Response(200, json=[{"task_id": TASK, "kind": "input"}])
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()))
    assert result.verdict == "timeout"


async def test_a_case_that_delivered_nothing_says_why(quick: None) -> None:
    diagnosis = {
        "what_failed": "lead produced no result",
        "why": "the model refused the request",
        "attempts": 3,
    }
    frames = [{"type": "step.state", "attempt": 3}, {"type": "escalation.raised", "diagnosis": diagnosis}]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": TASK})
        if request.url.path == f"/v1/tasks/{TASK}":
            return httpx.Response(200, json={"status": "failed", "budget": {}})
        if request.url.path == f"/v1/tasks/{TASK}/events":
            return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames))
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()))
    assert (result.verdict, result.attempts) == ("no artifact", 3)
    assert result.note == "lead produced no result: the model refused the request"


async def test_a_case_answered_in_a_reply_says_so(quick: None) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": TASK})
        if request.url.path == f"/v1/tasks/{TASK}":
            return httpx.Response(200, json={"status": "delivered", "budget": {}})
        if request.url.path == f"/v1/tasks/{TASK}/events":
            frames = [{"type": "step.state", "attempt": 1}, {"type": "task.delivered"}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames))
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        result = await harness.run_case(client, Golden(name="memo", goal="Write the memo", inputs=()))
    assert (result.verdict, result.note, result.ok) == ("no artifact", harness.REPLY_NOTE, False)


def test_a_term_in_the_render_sidecar_counts_for_a_binary_deliverable() -> None:
    sheet = '{"kind":"sheet","sheets":[{"rows":[["Revenue",41.6]]}]}'
    assert harness.missing_terms(("41.6", "22.9"), "PK\x03\x04", sheet) == ("22.9",)


def test_a_failed_receipt_names_the_check_that_failed_not_only_the_missing_judge() -> None:
    """`qoq-growth  fail  (no judge)` read as "failed because no judge" - it failed `numbers:grounded`.
    A missing cross-family judge never fails a receipt (the deterministic suite is the floor), so
    the note says which check did, and the judged count still reads the judge alone."""
    failed = {
        "verdict": "fail",
        "verifier_family": None,
        "checks": [
            {"name": "bytes:sha256", "status": "pass"},
            {"name": "numbers:grounded", "status": "fail"},
        ],
    }
    assert harness.receipt_note(failed) == "no judge · failed numbers:grounded"
    assert (
        harness.receipt_note({"verdict": "pass", "verifier_family": "anthropic", "checks": []}) == "anthropic"
    )
    assert not harness.judged("no judge · failed numbers:grounded")
    assert harness.judged("anthropic · failed numbers:grounded")


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "task.delivered", "seq": 9},
        {"type": "task.status", "status": "failed"},
        {"type": "task.status", "status": "cancelled"},
        {"type": "task.status", "status": "running"},
        {"type": "step.state", "state": "failed", "detail": "task.delivered"},
    ],
)
def test_the_replay_stops_exactly_where_the_cli_follow_stops(frame: dict[str, object]) -> None:
    """The eval reader and `prismoid tasks events --follow` answer one question - has this task stopped? -
    so they share one answer: a frame that ends the CLI's follow ends the replay, and nothing else does
    (a step's text that merely mentions a terminal event name is not one)."""
    from prismoid.runner import stream

    line = f"data: {json.dumps(frame)}"
    assert harness.is_terminal(line) is stream.task_stopped(frame)
    assert not harness.is_terminal("event: task.delivered")
    assert not harness.is_terminal(": heartbeat")


def test_golden_gates_on_its_floor_alone_without_a_baseline() -> None:
    run = {"a": True, "b": True, "c": False, "d": False}
    assert harness.golden_gate(run, None, floor=0.5) == ""
    assert harness.golden_gate(run, None, floor=0.9) == "50% is below the 90% floor"


def test_golden_fails_a_drop_against_its_baseline_but_not_a_flicker() -> None:
    """One trial per case flickers: a run may lose up to GOLDEN_SLACK of the cases both runs share, and
    one that loses more names them. Cases only one run has are held by the floor alone."""
    baseline = {f"c{n}": True for n in range(6)} | {"gone": False}
    flicker = {f"c{n}": n >= 2 for n in range(6)} | {"new": False}
    assert harness.GOLDEN_SLACK == 2
    assert harness.golden_gate(flicker, baseline, floor=0.0) == ""
    drop = {f"c{n}": n >= 3 for n in range(6)}
    assert harness.golden_gate(drop, baseline, floor=0.0) == (
        "3 of the 6 shared cases pass, the baseline passed 6 (now failing: c0, c1, c2)"
    )


def test_a_golden_baseline_is_an_earlier_reports_cases(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cases": {"memo": {"ok": True}, "sheet": {"ok": False}}}), encoding="utf-8")
    assert harness.baseline_cases(str(report)) == {"memo": True, "sheet": False}
    assert harness.baseline_cases("") is None
    with pytest.raises(SystemExit, match="cannot read --baseline"):
        harness.baseline_cases(str(report.with_name("missing.json")))
