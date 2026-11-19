# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the browser-step wiring tests, shared by the `test_engine_browser_wiring*` files: a fake
frame that records its calls, and the recording facts a publish writes. A plain module, not a test
file."""

from __future__ import annotations

from typing import Any

from prismoid.ai.frames.driver import ExecResult


class FakeFrame:
    def __init__(
        self,
        *,
        finalize_ok: bool = True,
        actions: int = 3,
        acting: int | None = None,
        fail_publish: bool = False,
        copy_exit_code: int = 0,
    ) -> None:
        self.recording_dir = "/artifacts/recording-1"
        self.attempt = 1
        self.calls: list[tuple[Any, ...]] = []
        self._finalize_ok = finalize_ok
        self._actions = actions
        self._acting = actions if acting is None else acting
        self._fail_publish = fail_publish
        self._copy_exit_code = copy_exit_code

    async def browser(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("browser", cmd))
        if cmd["verb"] == "finalize":
            return (
                {"ok": True, "actions": self._actions, "acting": self._acting}
                if self._finalize_ok
                else {"ok": False, "error": "boom"}
            )
        return {"ok": True}

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        self.calls.append(("exec", command))
        return ExecResult(
            exit_code=self._copy_exit_code, stdout="", stderr="" if not self._copy_exit_code else "no"
        )

    async def publish(self, path: str, type: str, title: str, sources: Any = ()) -> None:
        self.calls.append(("publish", path, type, title, tuple(sources)))
        if self._fail_publish:
            raise RuntimeError("publish refused")


def facts() -> tuple[list[tuple[str, str]], Any]:
    log: list[tuple[str, str]] = []

    async def fact(phase: str, label: str, *, started: float | None = None) -> None:
        log.append((phase, label))

    return log, fact


def wiring_copy_dest(frame: FakeFrame) -> str:
    command = next(c[1] for c in frame.calls if c[0] == "exec")
    return str(command).split("&&")[-1].split()[-1]
