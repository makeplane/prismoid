# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the Prismoid Chrome's layers (`chrome.py`) say to each other and to the outside: the fence it asks
about every request, the relay it tells about a task, and the values a page's state is kept in."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, NamedTuple, Protocol

# A page in no task's window is held to the base list only: `Fence.allows` reads this as its ref.
BASE: Final = "(base)"
# How a page is held, as the browser-wide session reads it: (fenced, the task whose list it is held
# to - `BASE` for the base list). Every page is fenced; a target that is no page is not.
Verdict = tuple[bool, str | None]
BASE_VERDICT: Final[Verdict] = (True, BASE)

# The committed URLs from the top of the page that asked down to the document that asked, or none
# when nothing vouches for it: asked for only by a request the list has already refused, because
# the answer can wait for a commit.
TopOf = Callable[[], Awaitable[tuple[str, ...]]]
# Is the page that asked one the person is driving (`Chrome.hand_over`)? Asked only as `TopOf` is.
HandedOf = Callable[[], Awaitable[bool]]


class Fence(Protocol):
    async def allows(
        self,
        ref: str | None,
        url: str,
        kind: str = "",
        top: TopOf | None = None,
        handed: HandedOf | None = None,
    ) -> bool:
        """May the task `ref` request `url`, of `Fetch`'s resource type `kind`, from the page `top`
        names, which `handed` says a person may be driving? `ref` None: no one task (a service
        worker) - the union of the live lists; `BASE`: a page in no task's window - the base list
        only. A refusal is recorded by the fence. Asynchronous: a loopback port it does not know is
        looked for first."""
        ...

    def listed(self, ref: str, url: str) -> bool:
        """Is this committed URL a site on the task `ref`'s list?"""
        ...

    def public(self, url: str) -> bool:
        """Is this a site, on no list or on one? Never loopback, a private address or a bad URL."""
        ...

    def refused(self, ref: str | None, url: str, reason: str) -> None:
        """Chrome refused a request of `ref`'s without asking: keep it as `allows` keeps its own."""
        ...


class NoFence:
    async def allows(
        self,
        ref: str | None,
        url: str,
        kind: str = "",
        top: TopOf | None = None,
        handed: HandedOf | None = None,
    ) -> bool:
        return False  # a Chrome given no fence lets no task's page reach anything

    def listed(self, ref: str, url: str) -> bool:
        return False

    def public(self, url: str) -> bool:
        return False

    def refused(self, ref: str | None, url: str, reason: str) -> None:
        pass


class Relay(Protocol):
    """A task's agent connection (`cdp_relay.TaskRelay`), as Chrome tells it what concerns it."""

    ref: str

    def on_event(self, msg: dict[str, Any]) -> None:
        """An event on a session it opened, or a browser-wide `Target` event of its task's."""
        ...

    def on_joined(self, info: dict[str, Any]) -> None:
        """A new page joined its task (a tab its page opened), fenced and running."""
        ...

    def on_lost(self, why: str) -> None:
        """Its window, or Chrome itself, is gone: the connection ends."""
        ...


class Document(NamedTuple):
    """A document in a page now, as `Page.frameNavigated` gave it. `parent` is its parent's frame."""

    loader: str
    url: str
    parent: str | None


class Going(NamedTuple):
    """A page's newest navigation, uncommitted: where to, its id in the `Network` announcements, and
    the URL the page last committed, which it is still on if Chrome cancels this. `left` None: there
    is no saying what the page holds, so nothing comes back."""

    url: str
    network: str
    left: str | None


@dataclass(frozen=True, slots=True)
class Paused:
    """One `Fetch.requestPaused`, as far as judging it goes. `page` is the page it belongs to, if
    any, `referrer` the `Referer` the browser gave it, which a page cannot write for another, and
    `network` its id in the `Network` announcements (`networkId`), absent if none was made."""

    request_id: object
    url: str
    kind: str
    page: str | None
    session: str | None
    referrer: str = ""
    network: str = ""
    navigation: bool = False  # its page's own main-frame document
    frame: str = ""  # the frame `Fetch` names
