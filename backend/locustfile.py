# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mixed-load model for the API (§15): 70% list-reads, 20% project-reads, 10% captures.

    just load          # 50 users, 30s, against :8111

The mix is deliberately read-heavy and its one write is a **capture** — soft intake that runs no model
work (Rule 9: nothing executes until promote). A load test whose 10% wrote `POST /v1/tasks` would enqueue
five hundred real frontier-model tasks and measure the worker's queue, not the request path's p95 under
concurrency, which is what §15 budgets are about. Captures exercise a real write (a row, a transaction)
without setting the fleet on fire.

Budgets to hold under 50 concurrent users (§15): list endpoints p95 <100 ms, capture p95 <50 ms.
"""

from __future__ import annotations

import json
import pathlib

from locust import HttpUser, between, task


def _token() -> str:
    return str(json.loads((pathlib.Path.home() / ".prismoid" / "credentials").read_text())["token"])


class PrismoidUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.client.headers["Authorization"] = f"Bearer {_token()}"

    @task(70)
    def list_tasks(self) -> None:
        self.client.get("/v1/tasks?limit=50", name="GET /v1/tasks")

    @task(20)
    def list_projects(self) -> None:
        self.client.get("/v1/projects", name="GET /v1/projects")

    @task(10)
    def capture(self) -> None:
        self.client.post("/v1/capture", json={"text": "load-test note"}, name="POST /v1/capture")
