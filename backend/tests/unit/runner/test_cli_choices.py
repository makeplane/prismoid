# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The CLI's option choices (`runner/cli/choices.py`) hold the same words as the request fields
they fill, so `--help` never offers a value the server refuses, nor hides one it takes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, get_args

import pytest
from typer.testing import CliRunner

from prismoid.ai.tools.browser import SCOPES
from prismoid.api.serializers.project_invites import ProjectInviteIn
from prismoid.api.serializers.projects import MemberIn
from prismoid.api.serializers.tasks import TaskCreateIn
from prismoid.runner import client
from prismoid.runner.cli.choices import (
    ApproveScope,
    Clarify,
    Deliverable,
    DocFormat,
    MemberRole,
    Mode,
    Size,
)
from prismoid.runner.main import app


def _words(enum: type[StrEnum]) -> set[str]:
    return {m.value for m in enum}


def _literal(model: Any, field: str) -> set[str]:
    annotation = model.model_fields[field].annotation
    return {a for a in _flatten(annotation) if isinstance(a, str)}


def _flatten(annotation: Any) -> list[Any]:
    annotation = getattr(annotation, "__value__", annotation)  # a `type X = ...` alias
    args = get_args(annotation)
    return [x for a in args for x in _flatten(a)] if args else [annotation]


def test_task_choices_match_the_task_request() -> None:
    assert _words(Clarify) == _literal(TaskCreateIn, "clarify")
    assert _words(DocFormat) == _literal(TaskCreateIn, "deliverable_format")
    assert _words(Size) == _literal(TaskCreateIn, "scale")
    # `council` is a legacy wire value the server still accepts; the CLI never offers it.
    assert _words(Mode) == _literal(TaskCreateIn, "mode") - {"council"}
    # `board` is the CLI's word for `collection`, and travels as it.
    assert {d.wire for d in Deliverable} == _literal(TaskCreateIn, "deliverable")


def test_role_and_scope_choices_match_their_requests() -> None:
    assert _words(MemberRole) == _literal(ProjectInviteIn, "role")
    pattern = MemberIn.model_fields["role"].metadata[0].pattern
    assert pattern == "^(" + "|".join(sorted(_words(MemberRole), reverse=True)) + ")$"
    assert _words(ApproveScope) == set(SCOPES)


def test_help_lists_the_choices() -> None:
    out = CliRunner().invoke(app, ["do", "--help"], env={"COLUMNS": "200", "TERM": "dumb"}).output
    for words in ("standard|fleet", "auto|never|always", "md|docx|pdf|html"):
        assert words in out, out


@pytest.mark.parametrize(
    ("args", "sent"),
    [
        (["--deliverable", "board"], {"deliverable": "collection"}),
        (["--deliverable", "deck", "--format", "pdf"], {"deliverable": "deck", "deliverable_format": "pdf"}),
        (["--size", "auto"], {"scale": "auto"}),
        ([], {}),
    ],
)
def test_do_sends_a_chosen_value_as_its_wire_word(
    monkeypatch: pytest.MonkeyPatch, args: list[str], sent: dict[str, str]
) -> None:
    bodies: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        if path == "/healthz":
            return {"device": False}
        bodies.append(kw["body"])
        return {"task_id": "t1"}

    monkeypatch.setattr(client, "request", fake_request)
    r = CliRunner().invoke(app, ["do", "goal", "--no-wait", "--json", *args])
    assert r.exit_code == 0, r.output
    (body,) = bodies
    assert {k: body[k] for k in ("deliverable", "deliverable_format", "scale") if k in body} == sent
    assert (body["mode"], body["clarify"]) == ("standard", "auto")


def test_a_value_outside_the_choices_never_reaches_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "request", lambda *a, **kw: pytest.fail("sent a request"))
    r = CliRunner().invoke(app, ["do", "goal", "--size", "huge"])
    assert r.exit_code == 2 and "huge" in r.output
