# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One approve gate, three scopes: the shape, the titles, and the resolve rules
that keep `always` the creator's alone."""

from __future__ import annotations

from prismoid.schemas.plan import GateKind, GateSpec


def test_gate_spec_defaults_keep_connector_gates_unchanged() -> None:
    spec = GateSpec(kind=GateKind.APPROVE, title="Allow Gmail: gmail_send?")
    assert spec.source == "connector" and spec.host == "" and spec.scopes == []


def test_a_browser_gate_carries_host_and_scopes() -> None:
    spec = GateSpec(
        kind=GateKind.APPROVE,
        title="t",
        source="browser",
        host="sso.vendor.com",
        scopes=["once", "task", "always"],
    )
    assert spec.model_dump(mode="json")["scopes"] == ["once", "task", "always"]


def test_gate_out_offers_always_to_the_creator_only() -> None:
    """Minor (whole-branch review): an editor may accept once/task but `always` 403s - so a
    non-creator viewer is never SHOWN it. The creator sees every scope the gate offers."""
    import datetime as dt
    import uuid

    from prismoid.api.serializers.gates import gate_out
    from prismoid.db.models.gate import Gate
    from prismoid.schemas.plan import GateKind, GateSpec

    gate = Gate(
        id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        spec=GateSpec(
            kind=GateKind.APPROVE,
            title="Allow the browser to open sso.vendor.com?",
            source="browser",
            host="sso.vendor.com",
            scopes=["once", "task", "always"],
        ).model_dump(mode="json"),
        expires_at=None,
        created_at=dt.datetime.now(dt.UTC),
    )
    creator, editor = uuid.uuid4(), uuid.uuid4()
    assert gate_out(gate, viewer_id=creator, creator_actor_id=creator).scopes == ["once", "task", "always"]
    assert gate_out(gate, viewer_id=editor, creator_actor_id=creator).scopes == ["once", "task"]
