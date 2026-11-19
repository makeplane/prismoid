# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The request fence: every request Chrome pauses is judged here, and answered, once. A layer of
`Chrome`; `Fence.allows` (`cdp_relay.RequestFence`) says yes or no, and what cannot be judged fails
closed. The proxy's own challenge is answered here too (`_on_auth`).
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Final

from prismoid.device.chrome_pages import ChromePages
from prismoid.device.chrome_pipe import LOG
from prismoid.device.chrome_types import Going, Paused, Verdict
from prismoid.device.egress import SEED_URLS

NO_ONES_WORKERS: Final = ("service_worker", "shared_worker")  # no one task's: held to the union
_AUTH_KEPT: Final = 256  # requests remembered as given the proxy credential (the loop guard)


class ChromeRequests(ChromePages):
    """Every paused request gets one answer, and the proxy's challenge gets its credential."""

    def _on_auth(self, params: dict[str, Any]) -> None:
        """Our proxy's challenge gets the `chrome` principal once per request (a refused credential
        is cancelled, never looped); any other - a site's own sign-in - is the person's to answer."""
        request_id = params.get("requestId")
        challenge = params.get("authChallenge") or {}
        response: dict[str, Any]
        if challenge.get("source") == "Proxy" and challenge.get("origin") in (
            self._proxy_origin,
            self._socket_proxy,
        ):
            if request_id in self._auth_tried:
                response = {"response": "CancelAuth"}
            else:
                self._auth_tried[str(request_id)] = None
                if len(self._auth_tried) > _AUTH_KEPT:
                    del self._auth_tried[next(iter(self._auth_tried))]
                user, password = self._credential
                response = {"response": "ProvideCredentials", "username": user, "password": password}
        else:
            response = {"response": "Default"}
        self._fire("Fetch.continueWithAuth", {"requestId": request_id, "authChallengeResponse": response})

    def _on_paused(self, session: str | None, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        url = str((params.get("request") or {}).get("url", ""))
        kind = str(params.get("resourceType", ""))
        if url in SEED_URLS:  # to the proxy, which refuses it: it is here for the 407 on the way
            self._fire("Fetch.continueRequest", {"requestId": request_id}, session)
            return
        frame = str(params.get("frameId"))
        page = self._page_of.get(session) if session is not None else frame
        # The browser-wide session names no page, so a frame is one only if it is a page's target:
        # a same-process iframe's frame is no target, and no target's end would ever clean up after it.
        top_level = frame == page if session is not None else self._info.get(frame, {}).get("type") == "page"
        if kind == "Document" and top_level:  # its top-level URL is unknown until this commits
            going = self._navigating.get(frame)
            left = self._tops.pop(frame, None) or (going.left if going is not None else None)
            self._navigating[frame] = Going(url, str(params.get("networkId") or ""), left)
            self._landed.setdefault(frame, asyncio.Event()).clear()
        headers = (params.get("request") or {}).get("headers") or {}
        referrer = next((str(v) for k, v in headers.items() if k.lower() == "referer"), "")
        navigation = kind == "Document" and top_level
        network = str(params.get("networkId") or "")
        paused = Paused(request_id, url, kind, page, session, referrer, network, navigation, frame)
        if session is None:
            # The browser-wide session pauses everything, for the proxy's challenge - and is the
            # second look at a page's own requests: a tab opened with no opener (a Cmd-click) starts
            # its navigation before its own session's `Fetch` is on, and only this session sees it.
            # Such a request waits for its page's verdict.
            self._auth_tried.pop(str(request_id), None)
            verdict = self._verdicts.get(frame)
            if verdict is None:
                target = self._info.get(frame, {}).get("type")
                if target in NO_ONES_WORKERS:
                    # A worker's first requests can outrun its own session's `Fetch`, under load, and
                    # come here alone (Chrome 156). Left to the proxy, a live pass would let them by.
                    self._judge(paused, (True, None))
                    return
                if target == "iframe" or not url.startswith(("http:", "https:")) or self._framed(frame):
                    self._fire("Fetch.continueRequest", {"requestId": request_id})  # its page's judged it
                    return
                # Every page is held from its attach, which comes first on this pipe, and its
                # iframes are told of, so this should never fire: what it would mean is a request
                # nothing fenced. Past here is only the proxy, which a live pass widens.
                LOG.warning("a request of %s %s reached the browser session with no verdict", target, frame)
                self._judge(paused, (True, None))
            elif verdict.done():
                self._judge(paused, verdict.result())
            else:
                self._spawn(self._judge_later(paused, verdict))
            return
        # A session we fenced and no longer know (it is closing) fails closed.
        known = session in self._fenced
        self._judge(paused, (True, self._fenced[session]) if known else (True, ""))

    def _framed(self, frame: str) -> bool:
        return any(frame in frames for frames in self._parents.values())

    def _judge(self, paused: Paused, verdict: Verdict) -> None:
        fenced, owner = verdict
        if not fenced:
            self._fire("Fetch.continueRequest", {"requestId": paused.request_id}, paused.session)
        elif owner == "":
            self._answer(paused, False)
        else:
            self._spawn(self._decide(paused, owner))

    async def _decide(self, paused: Paused, owner: str | None) -> None:
        try:
            allowed = await self._fence.allows(
                owner,
                paused.url,
                paused.kind,
                partial(self._pages_of, paused),
                partial(self._handed_on, paused, owner),
            )
        except Exception:  # a fence that cannot decide refuses
            LOG.exception("the request fence could not decide")
            allowed = False
        self._answer(paused, allowed)

    def _answer(self, paused: Paused, allowed: bool) -> None:
        if allowed:
            self._fire("Fetch.continueRequest", {"requestId": paused.request_id}, paused.session)
        else:
            self._fire(
                "Fetch.failRequest",
                {"requestId": paused.request_id, "errorReason": "BlockedByClient"},
                paused.session,
            )

    async def _judge_later(self, paused: Paused, verdict: asyncio.Future[Verdict]) -> None:
        self._judge(paused, await verdict)
