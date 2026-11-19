# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`ConnectorSpec` - the one secret field, masked everywhere (ADR-020).

The spec crosses the persistence-free boundary into the engine and rides on `TaskDeps`, which
means it is one careless `print(deps)`/span capture away from a log. This file pins the
property that makes that harmless: nothing the model exposes carries the token.
"""

from __future__ import annotations

import uuid

from pydantic import SecretStr

from prismoid.schemas.connectors import (
    PREVIEW_TOTAL_CAP,
    PREVIEW_VALUE_CAP,
    ConnectorSpec,
    ConnectorToolInfo,
    ToolPolicy,
    args_preview,
    default_tool_policy,
    effective_policies,
)
from prismoid.schemas.ids import ConnectorId

SECRET = "Bearer wildly-secret-token-value"


def _spec() -> ConnectorSpec:
    return ConnectorSpec(
        id=ConnectorId(uuid.uuid4()),
        slug="gmail",
        name="Gmail",
        url="https://mcp.example.com/mcp",
        tool_names=("send_email", "search"),
        instructions="Prefer drafts over sends.",
        authorization=SecretStr(SECRET),
    )


def test_repr_str_and_dumps_never_carry_the_token():
    spec = _spec()
    for face in (repr(spec), str(spec), spec.model_dump_json(), str(spec.model_dump())):
        assert SECRET not in face
        assert "wildly-secret" not in face


def test_the_engine_can_still_read_it_at_the_one_legal_line():
    assert _spec().authorization is not None
    assert _spec().authorization.get_secret_value() == SECRET  # pyright: ignore[reportOptionalMemberAccess]


def test_spec_is_frozen():
    spec = _spec()
    try:
        spec.slug = "other"
    except Exception:
        return
    raise AssertionError("ConnectorSpec must be frozen - it is shared across a step's toolsets")


# ── the policy dial - ONE place, pinned ───────────────────────────────────────────────────────


def _tool(name: str, *, read_only: bool | None = None, destructive: bool | None = None) -> ConnectorToolInfo:
    return ConnectorToolInfo(name=name, read_only=read_only, destructive=destructive)


def test_the_default_dial_is_allow_connecting_was_the_consent():
    """The default is allow - connecting was the consent: a connected service's tools run,
    annotations or not, a tool labelled both read-only and destructive included.
    `ask`/`off` stay one PATCH away per tool."""
    assert default_tool_policy(_tool("search", read_only=True)) is ToolPolicy.ALLOW
    assert default_tool_policy(_tool("send", read_only=False)) is ToolPolicy.ALLOW
    assert default_tool_policy(_tool("mystery")) is ToolPolicy.ALLOW
    assert default_tool_policy(_tool("wipe", read_only=False, destructive=True)) is ToolPolicy.ALLOW
    assert default_tool_policy(_tool("odd", read_only=True, destructive=True)) is ToolPolicy.ALLOW


def test_effective_policies_override_wins_and_drift_is_harmless():
    tools = [_tool("search", read_only=True), _tool("send"), _tool("grown_since")]
    out = effective_policies({"send": "ask", "dropped_tool": "off", "search": "off"}, tools)
    assert out == {
        "search": ToolPolicy.OFF,  # an override beats the default
        "send": ToolPolicy.ASK,  # an owner's ask stands - the gate still fires where set
        "grown_since": ToolPolicy.ALLOW,  # a tool the server grew gets the default
    }
    assert "dropped_tool" not in out, "an override for a vanished tool stops mattering"


def test_effective_policies_swallows_an_invalid_stored_value():
    out = effective_policies({"send": "yolo"}, [_tool("send")])
    assert out == {"send": ToolPolicy.ALLOW}


def test_args_preview_shows_what_will_run_and_says_what_it_left_out():
    """Accepting runs the whole call, so the approval shows a long body whole and says how much
    of a longer one it left out - a benign first line cannot hide the rest."""
    preview = args_preview({"to": "a@b.c", "body": "x" * 1_500, "subject": "hello"})
    assert "x" * 1_500 in preview and "not shown" not in preview
    assert "to: a@b.c" in preview and "subject: hello" in preview
    longer = args_preview({"body": "x" * (PREVIEW_VALUE_CAP + 50)})
    assert longer.endswith("… (50 more characters not shown)")


def test_args_preview_flattens_newlines_and_marks_the_cut():
    many = {f"k{i:02}": "v" * 1_000 for i in range(20)}
    preview = args_preview(many)
    assert preview.endswith("more characters not shown)") and len(preview) <= PREVIEW_TOTAL_CAP + 60
    assert "\n\n" not in preview
    assert args_preview({"a": "line1\nline2"}) == "a: line1 line2"


def test_args_preview_shows_the_owner_no_invisible_characters():
    """A bidi override or a zero-width character must not make an approval card read unlike the call."""
    spoof = "evil\u202e\u200bmoc.elpmaxe@ada\u2066\ufeff"
    preview = args_preview({"to\u200b": spoof})
    assert preview == "to: evilmoc.elpmaxe@ada"
