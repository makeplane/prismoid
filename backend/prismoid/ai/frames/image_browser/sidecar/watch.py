# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What an acting verb did to the page, and when the page is done answering it.

A probe in an isolated world (`WORLD`: the page's own scripts can neither see nor touch it) counts
the page's DOM mutations and reads its document, address and scroll. Read before and after an act,
it says what the act did (`change`): `navigated` to another document or address (a `#fragment` jump
keeps it), `changed` the page (a mutation, a scroll), or `none` - the guard's "nothing happened"
without hashing pages, and the model's without a second look. A value set on a field is a property
no observer sees, so a field verb never says `none`, nor a click on a checkbox or a radio
(`checked` is a property too; `reported`). Nothing here is a verb:
`scripts.PROBE_JS` is a fixed constant.

The settle (`BrowserVerbs._settle`) waits for the page's own quiet instead of a fixed sleep: no
mutation for `QUIET_MS` and no request younger than `FRESH_S` in flight (`Requests`), up to
`SETTLE_S` - so a filter's request answers before the next look, and a click that changed nothing
costs about 80 ms. The whole wait never passes `LOAD_S + SETTLE_S` (a page stuck loading, a refresh
loop): the verb then answers `settled: false`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, cast

from .cdp import CdpError
from .scripts import APPEARS_JS, PROBE_JS

WORLD: Final = "prismoid-watch"
QUIET_MS: Final = 80
SETTLE_S: Final = 0.8  # the most a settle waits for the quiet, a navigation's load aside
FRESH_S: Final = 1.5  # a request older than this is a long poll or a stream, never waited on
LOAD_S: Final = 10.0  # a navigation that started is waited out to its load, as ever
# The verbs that set a field's value: a property the MutationObserver never sees, so their `none`
# is unknown, never reported (a typed field read "no change" and invited a retype).
VALUE_VERBS: Final = frozenset({"type", "fill", "select"})
# The roles whose click sets a property the observer never sees either (`checked`): a click on one
# is a field verb (`reported`).
TOGGLE_ROLES: Final = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"})
# What stopped an open, from Chromium's net error, as the fact the browser tool decides on
# (`SiteBlock`'s wire values): the egress proxy refused it, the site reset the stream, or its name
# did not resolve.
PROXY_ERRORS: Final = ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "ERR_PROXY_AUTH")
SITE_BLOCKS: Final = {
    "ERR_HTTP2_PROTOCOL_ERROR": "reset",
    "ERR_NAME_NOT_RESOLVED": "unresolved",
    "ERR_NAME_RESOLUTION_FAILED": "unresolved",
}
# Chromium buffers response bodies for `Network.getResponseBody`, which nothing here reads.
NETWORK_ENABLE: Final[dict[str, Any]] = {
    "maxTotalBufferSize": 0,
    "maxResourceBufferSize": 0,
    "maxPostDataSize": 0,
}

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def probe(call: Call, frame: str | None, *, wait_s: float = 0.0) -> dict[str, Any] | None:
    """The page as the probe reads it - once quiet, when `wait_s` is given - or None when it
    cannot be read (no world for the frame, a document torn down under the read)."""
    value = await _in_world(call, frame, PROBE_JS, [QUIET_MS, round(wait_s * 1000)])
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


async def appears(call: Call, frame: str | None, text: str, cap_s: float) -> bool | None:
    """Whether `text` showed on the page within `cap_s`; None when the page could not be read (a
    document torn down under the wait: the next one is read after its load)."""
    value = await _in_world(call, frame, APPEARS_JS, [text, round(cap_s * 1000)])
    return value if isinstance(value, bool) else None


async def _in_world(call: Call, frame: str | None, script: str, args: list[Any]) -> Any:
    """`script`'s answer in this sidecar's isolated world of `frame`; None when it cannot run there."""
    if frame is None:
        return None
    try:
        world = await call("Page.createIsolatedWorld", {"frameId": frame, "worldName": WORLD})
        if (context := world.get("executionContextId")) is None:
            return None
        out = await call(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": script,
                "executionContextId": context,
                "arguments": [{"value": arg} for arg in args],
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
    except CdpError:
        return None
    result: dict[str, Any] = out.get("result") or {}
    return result.get("value")


def change(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> str | None:
    """What the act did, between two probes; None when either could not be read - an unknown is
    never reported as `none`."""
    if before is None or after is None:
        return None
    if before.get("doc") != after.get("doc") or _document(before) != _document(after):
        return "navigated"
    moved = any(before.get(k) != after.get(k) for k in ("n", "x", "y", "url"))
    return "changed" if moved else "none"


def _document(probed: Mapping[str, Any]) -> str:
    return str(probed.get("url") or "").split("#", 1)[0]


def reported(
    verb: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    tab: object,
    settled: bool,
    role: str = "",
) -> dict[str, Any]:
    """What an acting verb's result says of the page: `change` when it is known (a tab move is a
    navigation; a field verb's `none` is not known, nor a click's on a `role` in `TOGGLE_ROLES`),
    and `settled: false` when the settle's cap ran out on a page still loading."""
    seen = "navigated" if tab else change(before, after)
    field = verb in VALUE_VERBS or (verb == "click" and role in TOGGLE_ROLES)
    known = seen is not None and not (seen == "none" and field)
    return {**({"change": seen} if known else {}), **({} if settled else {"settled": False})}


def left(verb: str, result: Mapping[str, Any], before: Mapping[str, Any] | None) -> bool:
    """Whether an act that succeeded left the page its references belong to: an open, a back off the
    first page, a tab move or `change` navigated; unknown, the addresses before and after decide."""
    if verb == "open" or (verb == "back" and not result.get("note")) or result.get("tab"):
        return True
    if "change" in result:
        return result["change"] == "navigated"
    was = _document(before) if before else ""
    return bool(was) and was != _document(result)


class Requests:
    """The page tab's requests in flight, each with when it started on the event loop's clock.
    Only a young one holds the page (`fresh`): one older than `FRESH_S` is a long poll, a stream
    or a beacon that may never end."""

    def __init__(self, page: Callable[[], str | None] = lambda: None) -> None:
        self._page = page  # the page tab's session: a tab in the background never holds the page
        self._open: dict[str, float] = {}
        self._ended = asyncio.Event()

    def seen(self, msg: Mapping[str, Any]) -> None:
        """A `Network` event: a request of the page's tab sent, or one that finished or failed."""
        if msg.get("sessionId") not in (None, self._page()):
            return
        params: Mapping[str, Any] = msg.get("params") or {}
        if msg.get("method") == "Network.requestWillBeSent":
            self.sent(params)
        else:
            self.ended(params)

    def sent(self, params: Mapping[str, Any], now: float | None = None) -> None:
        if isinstance(rid := params.get("requestId"), str):
            self._open[rid] = asyncio.get_running_loop().time() if now is None else now

    def ended(self, params: Mapping[str, Any]) -> None:
        if self._open.pop(str(params.get("requestId")), None) is not None:
            self._ended.set()

    def fresh(self, now: float) -> bool:
        self._open = {rid: at for rid, at in self._open.items() if now - at < FRESH_S}
        return bool(self._open)

    def clear(self) -> None:
        """The page moved to another tab: its requests are not this one's."""
        self._open = {}

    async def wait(self, timeout: float, *, or_: asyncio.Event) -> None:  # noqa: ASYNC109 - the settle's own deadline
        """Until a request ends, `or_` is set (a navigation started), or `timeout`."""
        self._ended.clear()
        waits = {asyncio.ensure_future(self._ended.wait()), asyncio.ensure_future(or_.wait())}
        try:
            await asyncio.wait(waits, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in waits:
                pending.cancel()


def open_failed(error_text: str) -> dict[str, Any]:
    """A failed open's result: the words the model reads and, when Chromium's code names it, the wall."""
    if any(code in error_text for code in PROXY_ERRORS):
        return {"ok": False, "error": "egress refused", "blocked": "egress"}
    failed = {"ok": False, "error": f"navigation failed: {error_text or 'the page did not load'}"}
    code = error_text.removeprefix("net::")
    return {**failed, "blocked": SITE_BLOCKS[code]} if code in SITE_BLOCKS else failed
