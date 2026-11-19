# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Performance budgets, asserted.

Performance is a requirement, not a hope - so the budgets are tests, and a regression is a
red build. Each measures **warm p95** over N requests against the running API (the compose baseline the
budgets are quoted at): the first requests warm the connection and the asyncpg pool, then N are timed and
the 95th percentile must sit under the ceiling.

Latency budgets only, and deliberately: the *temporal* budgets (submit→first-event <1 s, submit→planned
<5 s, event→WS <250 ms, frame cold-start <2 s) need a real task doing real model work, and live in the
smoke/load paths, not here where they would spend tokens on every run. What this file pins is the
request-path floor - list, login, capture, task-enqueue - which is what an N+1 or a cold pool blows first.

Run with the API up: `just perf`. Skipped (not failed) if the API is unreachable, so the default unit
suite stays hermetic.
"""

from __future__ import annotations

import json
import os
import pathlib
import statistics
import time
from collections.abc import Iterator

import httpx
import pytest

from prismoid.runner import client as stored

pytestmark = pytest.mark.perf

BASE = "http://localhost:8111"

# The nightly regression gate: a p95 more than 20% over the recorded baseline fails, even if
# it is still under the absolute budget - catching a creeping regression before it reaches the ceiling.
# `PRISMOID_PERF_RECORD=1 just perf` re-records the baseline; the gate reads it otherwise.
BASELINE_PATH = pathlib.Path(os.environ.get("PRISMOID_PERF_BASELINE_PATH", "tests/perf/baseline.json"))
RECORD = os.environ.get("PRISMOID_PERF_RECORD") == "1"
REGRESSION = 1.20  # >20% over baseline is a regression
# …but +20% of a 5 ms baseline is 1 ms, and normal run-to-run jitter is bigger than that - so a pure
# ratio gate on sub-10 ms metrics flaps. The floor gives every metric at least this much absolute slack,
# so jitter passes and only a real regression (the 100 ms sleep drill: 14→123 ms) trips it.
REGRESSION_FLOOR_MS = 12.0


def _baseline() -> dict[str, float]:
    return json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}


@pytest.fixture(scope="module")
def recorder() -> Iterator[dict[str, float]]:
    """Collects each measured p95; writes the baseline on teardown when PRISMOID_PERF_RECORD=1."""
    measured: dict[str, float] = {}
    yield measured
    if RECORD and measured:
        BASELINE_PATH.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")


def _assert_within(label: str, p95: float, budget_ms: float, recorder: dict[str, float]) -> None:
    recorder[label] = round(p95, 2)
    base = _baseline()
    ceiling = budget_ms
    detail = f"budget {budget_ms:.0f}ms"
    if not RECORD and label in base:
        gate = max(base[label] * REGRESSION, base[label] + REGRESSION_FLOOR_MS)  # ratio, floored
        if gate < ceiling:
            ceiling, detail = (
                gate,
                f"baseline {base[label]:.1f}ms +max(20%,{REGRESSION_FLOOR_MS:.0f}ms) = {gate:.1f}ms",
            )
    assert p95 < ceiling, f"{label}: p95 {p95:.1f}ms exceeds {detail}"


# (label, method, path, budget_ms, body, n). Every number is a published budget. `n` is per-budget so the
# mutating endpoints don't pile up rows: a GET can run 40×, a capture fewer.
BUDGETS: list[tuple[str, str, str, float, dict[str, str] | None, int]] = [
    ("GET /v1/tasks (list 50)", "GET", "/v1/tasks?limit=50", 100.0, None, 40),
    ("GET /v1/projects (list)", "GET", "/v1/projects", 100.0, None, 40),
    ("POST /v1/capture (soft intake)", "POST", "/v1/capture", 50.0, {"text": "a quick note to capture"}, 15),
]


def _token() -> str | None:
    return stored.token_for(BASE)


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    token = _token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    c = httpx.Client(base_url=BASE, headers=headers, timeout=10.0)
    try:
        if c.get("/healthz").status_code != 200:
            pytest.skip("API not healthy on :8111 - run `just perf` with the stack up")
    except httpx.HTTPError:
        pytest.skip("API unreachable on :8111 - run `just perf` with the stack up")
    return c


def _p95(
    client: httpx.Client, method: str, path: str, body: dict[str, str] | None, n: int = 40
) -> tuple[float, float]:
    for _ in range(5):  # warm the connection + pool; the budget is the warm p95, not the cold first hit
        client.request(method, path, json=body)
    samples: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        client.request(method, path, json=body)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return statistics.mean(samples), samples[int(0.95 * len(samples)) - 1]


@pytest.mark.parametrize("label,method,path,budget_ms,body,n", BUDGETS, ids=[b[0] for b in BUDGETS])
def test_budget(
    client: httpx.Client,
    recorder: dict[str, float],
    label: str,
    method: str,
    path: str,
    budget_ms: float,
    body: dict[str, str] | None,
    n: int,
) -> None:
    mean, p95 = _p95(client, method, path, body, n)
    print(f"\n  {label:<34} mean {mean:6.1f}ms   p95 {p95:6.1f}ms   budget {budget_ms:.0f}ms")
    _assert_within(label, p95, budget_ms, recorder)


def test_login_argon2_budget(client: httpx.Client) -> None:
    """Login is <200 ms, argon2-dominated. Measured below the failed-login throttle: the endpoint
    locks an IP out (403) after ~10 fails, so only the first few attempts run argon2 (401). We time those
    - the real hashing cost - and skip if a prior run has already tripped the throttle (nothing to measure)."""
    body = {"email": "perf-probe@example.com", "password": "definitely-wrong-password"}
    argon2_ms: list[float] = []
    for _ in range(6):  # stay under the ~10-fail throttle so these reach the hasher, not the lockout
        start = time.perf_counter()
        r = client.post("/v1/auth/login", json=body)
        ms = (time.perf_counter() - start) * 1000
        if r.status_code == 401:  # reached argon2 (unknown user → dummy-verify, no enumeration leak)
            argon2_ms.append(ms)
    if not argon2_ms:
        pytest.skip("failed-login throttle already active - argon2 path not measurable this run")
    worst = max(argon2_ms)
    print(
        f"\n  {'POST /v1/auth/login (argon2)':<34} worst {worst:6.1f}ms   ({len(argon2_ms)} argon2 hits)   budget 200ms"
    )
    # Absolute budget only, no regression gate: argon2 timing swings with host load (95–143 ms observed),
    # and a ±20% band on a noisy metric is a flaky gate. The stable GET/capture p95s carry the regression.
    assert worst < 200.0, f"login argon2 {worst:.1f}ms exceeds the §15 budget of 200ms"
