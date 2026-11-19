# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A scripted CDP connection and the pages tests script on it: `FakeCdp` stands in for the sidecar's
pipe, `AX_BUTTON` is a one-button accessibility tree, `ref_for` reads a reference off a snapshot."""

from __future__ import annotations

import asyncio
import base64
import re
import sys
from collections.abc import Callable
from typing import Any

from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
# The sidecar runs as top-level `sidecar` (the frame image's layout); only a path entry gives its CdpError.
sys.path.insert(0, str(SIDECAR))

from sidecar.cdp import CdpError  # noqa: E402

DEFAULT_ANSWERS: dict[str, dict[str, Any]] = {
    "Page.getNavigationHistory": {
        "currentIndex": 0,
        "entries": [{"url": "http://127.0.0.1:8088/index.html", "title": ""}],
    },
    "DOM.getBoxModel": {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}},
    "DOM.resolveNode": {"object": {"objectId": "obj-1"}},
    "DOM.describeNode": {"node": {"attributes": []}},
    "Page.captureScreenshot": {"data": base64.b64encode(b"\xff\xd8\xff").decode()},
    "Runtime.evaluate": {"result": {}},  # no `objectId`: nothing is focused, by default
}


Answer = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


class FakeCdp:
    """A scripted double for `PipeTransport`: `.on`/`.emit` for events (the same contract
    `PipeTransport` offers), `.answer(method, result)` to script a call's reply (or a
    function of the call's params, when the reply depends on which element was asked about),
    and a baseline of sane default replies so a test only has to script what it cares about."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self._answers: dict[str, Answer] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.sessions: list[tuple[str, str | None]] = []  # (method, session_id) of every call
        # Seams for the tabs (a tab the page opens): `hooks[method](session, params)` runs as the
        # call arrives - it may emit events (a tab closing mid-call) or raise; a call on a session
        # in `dead` is refused as Chromium refuses a detached one; `gates[method]` holds the call
        # until the test sets it. An unscripted attach to `Tn` answers session `Sn`.
        self.hooks: dict[str, Any] = {}
        self.dead: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> None:
        """An event, from the page session `session` when given (flattened CDP names its session
        on every page event), or with no session at all."""
        msg: dict[str, Any] = {"method": event, "params": params or {}}
        if session is not None:
            msg["sessionId"] = session
        for h in self._handlers.get(event, []):
            h(msg)

    def sent_on(self, method: str) -> list[str | None]:
        """The session of every call made for `method`, in order."""
        return [s for m, s in self.sessions if m == method]

    def answer(self, method: str, result: Answer, *, on: str | None = None) -> None:
        """Script `method`'s reply - on the session `on` only, when given."""
        self._answers[f"{on}|{method}" if on else method] = result

    async def close(self) -> None:
        pass

    def sent(self, method: str) -> list[dict[str, Any] | None]:
        """Params of every call made for `method`, in order - the assertion surface for a
        fire-and-forget handler (`_fire`) that answers over `.call` with nothing to await."""
        return [params for m, params in self.calls if m == method]

    async def drain(self) -> None:
        """Let any `asyncio.create_task`'d fire-and-forget calls (`Browser._fire`, invoked from
        inside a synchronous event handler) actually run. Each one does at most one `await` of
        this fake's own `.call`, which never itself suspends, so a couple of loop turns is
        always enough - but a few extra costs nothing and stays robust to that changing."""
        for _ in range(5):
            await asyncio.sleep(0)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        self.sessions.append((method, session_id))
        if (gate := self.gates.get(method)) is not None:
            await gate.wait()
        if (hook := self.hooks.get(method)) is not None:
            hook(session_id, params)
        if session_id in self.dead:
            raise CdpError(-32001, "Session with given id not found.")
        answer = self._answers.get(f"{session_id}|{method}", self._answers.get(method))
        if answer is None and method == "Target.attachToTarget":
            return {"sessionId": "S" + str((params or {})["targetId"]).removeprefix("T")}
        if answer is None:
            answer = DEFAULT_ANSWERS.get(method, {})
        return answer(params or {}) if callable(answer) else answer


AX_BUTTON = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "childIds": ["2"]},
    {
        "nodeId": "2",
        "role": {"value": "button"},
        "name": {"value": "Save changes"},
        "backendDOMNodeId": 42,
        "childIds": [],
    },
]


def ref_for(snapshot: dict[str, Any], needle: str) -> str:
    """The reference right before `needle` on its line: the look puts a small form on one line."""
    line = next(line for line in str(snapshot["text"]).splitlines() if needle in line)
    return re.findall(r"\[(e\d+\.\d+)\]", line[: line.index(needle)])[-1]


def script_page(fake: FakeCdp) -> None:
    fake.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "https://plane.so/"}]}
    )
    fake.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F1"}}})
    fake.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})
