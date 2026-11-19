# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A command past its bound is stopped inside the frame, and its result says so.

The real kill is proven against Docker in `tests/integration/frames/test_frame.py`; here a stand-in
container answers the way `timeout(1)` does, so the honesty of the result is checked alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame


@dataclass
class _Container:
    """Answers every exec with `exit_code` and some output, keeping each argv it ran."""

    exit_code: int
    ran: list[list[str]] = field(default_factory=list[list[str]])

    def exec_run(self, argv: list[str], **_: Any) -> Any:
        self.ran.append(argv)
        return type("Out", (), {"exit_code": self.exit_code, "output": (b"partial\n", b"")})()


def _frame(container: _Container, monkeypatch: pytest.MonkeyPatch) -> DockerFrame:
    frame = DockerFrame(task_id="t")
    monkeypatch.setattr(frame, "_container", container)
    return frame


async def test_a_command_past_its_bound_runs_under_timeout_and_says_it_was_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero bound has always elapsed by the time the exec answers, with no clock to wait on."""
    container = _Container(exit_code=124)

    result = await _frame(container, monkeypatch).exec("sleep 999", timeout=0)

    assert container.ran[0][0] == "timeout", "the bound is enforced in the frame, not by abandoning a thread"
    assert result.exit_code == 124
    assert result.stdout == "partial\n", "what it printed before the stop is kept"
    assert "timed out after 0s; killed" in result.stderr


async def test_a_command_that_exits_124_on_its_own_is_not_called_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _frame(_Container(exit_code=124), monkeypatch).exec("exit 124", timeout=60)

    assert result.exit_code == 124
    assert "timed out" not in result.stderr
