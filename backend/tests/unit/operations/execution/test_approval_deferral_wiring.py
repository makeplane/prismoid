# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The engine's approval wiring (task 7 review): `source` is "browser" only when EVERY
approval in a batch is browser-sourced (a mixed batch is a CONNECTOR gate, creator-only -
accept approves the whole batch, and a browser call must never smuggle a connector call
past a project editor); `host`/`scopes` apply only to a LONE `browser_open` allowlist ask;
each call's `fingerprint` rides `resume_state` for the resume, and `open_approval`
gets `source`/`host`/`scopes` forwarded untouched. No database, no agent run - `_approval_
deferral` is pure and `_park_on_approval`'s collaborators are monkeypatched."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import execution
from prismoid.operations.execution import ApprovalDeferral
from prismoid.operations.execution import deferrals as ex_deferrals
from prismoid.operations.execution import parks as ex_parks
from prismoid.schemas.park import ParkKind, park_of
from prismoid.schemas.plan import TaskStatus


def _dtr(approvals_meta: list[tuple[str, str, dict[str, Any]]]) -> DeferredToolRequests:
    """`approvals_meta`: (tool_call_id, tool_name, metadata) triples - exactly the shape the
    browser/connector toolsets' `ApprovalRequired(metadata=...)` produces."""
    calls = [ToolCallPart(tool_name=name, args={}, tool_call_id=cid) for cid, name, _ in approvals_meta]
    metadata = {cid: meta for cid, _, meta in approvals_meta}
    return DeferredToolRequests(approvals=calls, metadata=metadata)


class TestApprovalDeferralWiring:
    def test_a_lone_browser_open_allowlist_ask_carries_host_scopes_and_fingerprint(self) -> None:
        output = _dtr(
            [
                (
                    "c1",
                    "browser_open",
                    {
                        "source": "browser",
                        "tool": "browser.open",
                        "host": "sso.vendor.com",
                        "scopes": ["once", "task", "always"],
                        "preview": "open sso.vendor.com/login",
                        "connector_name": "sso.vendor.com",
                        "fingerprint": {"host": "sso.vendor.com"},
                    },
                )
            ]
        )
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "browser"
        assert d.host == "sso.vendor.com"
        assert d.scopes == ["once", "task", "always"]
        assert d.fingerprints == {"c1": {"host": "sso.vendor.com"}}
        assert d.items == [("c1", "sso.vendor.com", "browser.open", "open sso.vendor.com/login")]

    def test_a_browser_danger_click_carries_no_scopes_or_host(self) -> None:
        output = _dtr(
            [
                (
                    "c1",
                    "browser_click",
                    {
                        "source": "browser",
                        "tool": "browser.click",
                        "host": "",
                        "scopes": [],
                        "preview": "click #submit",
                        "connector_name": "example.com",
                        "fingerprint": None,
                    },
                )
            ]
        )
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "browser"
        assert d.host == "" and d.scopes == []
        assert d.fingerprints == {"c1": None}

    def test_two_browser_approvals_carry_no_scopes_even_when_one_is_the_allowlist_ask(self) -> None:
        """Only a LONE allowlist ask gets scopes - a batch of two never singles out one call's
        host for the human to choose a duration over."""
        output = _dtr(
            [
                (
                    "c1",
                    "browser_open",
                    {
                        "source": "browser",
                        "tool": "browser.open",
                        "host": "x.com",
                        "scopes": ["once", "task", "always"],
                    },
                ),
                (
                    "c2",
                    "browser_click",
                    {"source": "browser", "tool": "browser.click", "host": "", "scopes": []},
                ),
            ]
        )
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "browser"
        assert d.host == "" and d.scopes == []

    def test_a_mixed_batch_is_a_connector_gate_creator_only(self) -> None:
        """CRITICAL 1: one connector call anywhere in the batch makes the WHOLE gate
        "connector" - accept approves every call in the batch, so a browser call riding
        alongside a connector one must never let a project editor wave the connector call
        through on a gate that LOOKED like a browser ask."""
        output = _dtr(
            [
                (
                    "c1",
                    "browser_open",
                    {
                        "source": "browser",
                        "tool": "browser.open",
                        "host": "x.com",
                        "scopes": ["once", "task", "always"],
                    },
                ),
                (
                    "c2",
                    "gmail_send",
                    {"source": "connector", "connector_name": "Gmail", "tool": "gmail_send"},
                ),
            ]
        )
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "connector"
        assert d.host == "" and d.scopes == []

    def test_a_connector_only_batch_is_unchanged(self) -> None:
        output = _dtr([("c1", "gmail_send", {"connector_name": "Gmail", "tool": "gmail_send"})])
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "connector"
        assert d.host == "" and d.scopes == []

    def test_a_call_with_no_metadata_at_all_defaults_to_connector(self) -> None:
        """A call whose metadata went missing still gets a keys-only line (the park must not
        depend on it) - and, missing `source`, it reads as "connector", never "browser"."""
        output = DeferredToolRequests(
            approvals=[ToolCallPart(tool_name="gmail_send", args={}, tool_call_id="c1")], metadata={}
        )
        d = ex_deferrals.approval_deferral(output)
        assert d.source == "connector"


class TestParkOnApprovalForwardsAndPersistsFingerprints:
    async def test_fingerprints_land_in_resume_state_and_source_host_scopes_forward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_open_approval(
            session: object,
            task: object,
            step_id: object,
            items: object,
            *,
            source: str = "connector",
            host: str = "",
            scopes: Any = (),
            asks_because: Any = (),
        ) -> SimpleNamespace:
            captured.update(
                session=session,
                task=task,
                step_id=step_id,
                items=items,
                source=source,
                host=host,
                scopes=list(scopes),
            )
            return SimpleNamespace(id=uuid.uuid4())

        async def fake_advance(session: object, *, task_id: object, to: object) -> None:
            captured["advanced_to"] = to

        from prismoid.operations import gates as gates_ops

        monkeypatch.setattr(gates_ops, "open_approval", fake_open_approval)
        monkeypatch.setattr(execution, "advance", fake_advance)

        deferral = ApprovalDeferral(
            items=[
                ("c1", "sso.vendor.com", "browser.open", "open sso.vendor.com/login"),
                ("c2", "sso.vendor.com", "browser.click", "click #submit"),
            ],
            source="browser",
            host="sso.vendor.com",
            scopes=["once", "task", "always"],
            fingerprints={"c1": {"host": "sso.vendor.com"}, "c2": None},
        )
        task = SimpleNamespace(id=uuid.uuid4())
        step = SimpleNamespace(id=uuid.uuid4(), resume_state=None)

        gate_id = await ex_parks.park_on_approval(
            cast(AsyncSession, object()), cast(Task, task), cast(PlanStep, step), deferral
        )

        assert park_of(step.resume_state).kind is ParkKind.APPROVAL
        assert "approval_calls" not in step.resume_state, "the calls are read off the replayed turn"
        assert captured["source"] == "browser"
        assert captured["host"] == "sso.vendor.com"
        assert captured["scopes"] == ["once", "task", "always"]
        assert captured["items"] == deferral.items
        assert captured["advanced_to"] is TaskStatus.WAITING
        assert gate_id is not None

    async def test_a_connector_deferral_forwards_the_connector_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_open_approval(
            session: object,
            task: object,
            step_id: object,
            items: object,
            *,
            source: str = "connector",
            host: str = "",
            scopes: Any = (),
            asks_because: Any = (),
        ) -> SimpleNamespace:
            captured.update(source=source, host=host, scopes=list(scopes))
            return SimpleNamespace(id=uuid.uuid4())

        async def fake_advance(session: object, *, task_id: object, to: object) -> None:
            return None

        from prismoid.operations import gates as gates_ops

        monkeypatch.setattr(gates_ops, "open_approval", fake_open_approval)
        monkeypatch.setattr(execution, "advance", fake_advance)

        deferral = ApprovalDeferral(items=[("c1", "Gmail", "gmail_send", "to: a@b.c")])
        task = SimpleNamespace(id=uuid.uuid4())
        step = SimpleNamespace(id=uuid.uuid4(), resume_state=None)

        await ex_parks.park_on_approval(
            cast(AsyncSession, object()), cast(Task, task), cast(PlanStep, step), deferral
        )

        assert captured == {"source": "connector", "host": "", "scopes": []}
        assert park_of(step.resume_state).kind is ParkKind.APPROVAL
