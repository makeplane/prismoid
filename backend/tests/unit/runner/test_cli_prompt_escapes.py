# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A gate's words reach the CLI user's terminal printable: no escape sequence survives.

The title, the evidence and the questions are a model's or a server's words, and a hostile page
can steer a model into writing them. An ANSI sequence in the approval would let it repaint or
hide what the person is about to allow, so every such string goes through `terminal.printable`.
The test reads what the prompt hands to `typer` (click strips codes itself only when stdout is
not a terminal, which a person's is).
"""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.runner import client, questions

ESC = "\x1b"
HOSTILE = f"Send the report{ESC}[2K{ESC}[1A to cfo@example.com"


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(questions.typer, "secho", lambda message="", **_: lines.append(str(message)))
    monkeypatch.setattr(questions.typer, "echo", lambda message="", **_: lines.append(str(message)))
    return lines


def test_a_question_renders_without_escape_sequences(written: list[str]) -> None:
    questions.render(
        {
            "title": HOSTILE,
            "context": HOSTILE,
            "questions": [
                {"type": "choice", "prompt": HOSTILE, "options": [{"label": HOSTILE}], "default": HOSTILE}
            ],
        }
    )
    assert not any(ESC in line for line in written)
    assert any("Send the report" in line for line in written)


def test_an_approval_renders_without_escape_sequences(
    written: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions.typer, "prompt", lambda *a, **k: "skip")
    sent: list[Any] = []
    monkeypatch.setattr(client, "request", lambda *a, **k: sent.append((a, k)))
    questions.ask_decision("g1", title=HOSTILE, reason=f"Gmail: send_email\nbody: {HOSTILE}", as_json=False)
    assert not any(ESC in line for line in written)
    assert any("Gmail: send_email" in line for line in written)
