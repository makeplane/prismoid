# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Which page and which document asked (ADR-078 decisions 19 and 21): what each page holds, kept
from Chrome's own word, and the hand-off that reads it. `chrome.py` says why each rule is here.
`PASS_S` is the hand-off's ceiling.

A layer of `Chrome`. The order of the checks in `_on_committed`, `_pages_of`, `hand_over`,
`_handed_on` and `_carries` is the fence: each one is what a later one relies on.

A page that ever held what a fenced command can write marks every page it opened with a window
reference (`_written`), for as long as that page is open, whatever it shows then: a popup's
`about:blank` runs its opener's script, and Chrome names no origin for it (`securityOrigin` is `://`
in `Page.frameNavigated`, on Chrome 156, with the command's script running in it).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from prismoid.device.chrome_pipe import LOG, ChromeError, ChromePipe
from prismoid.device.chrome_types import BASE, Document, Going, Paused

PASS_S: Final = 30 * 60.0  # a hand-off's longest, on the wall clock, whoever is at the window
_PASS_TICK_S: Final = 15.0  # how often it is looked at: a timer stops while a Mac sleeps, a clock does not
_BACK_S: Final = 5.0  # a page sent back from off the list: past this it is closed
_HOME_S: Final = 10.0  # a page off the list on its way to a listed one: past this it is sent back
_ASKED_KEPT: Final = 4096  # requests remembered with the document that announced them
_COMMIT_S: Final = 2.0  # an asset held for a commit: past this the page is in no known state
_WRITTEN: Final = ("data", "file", "filesystem")  # schemes whose page is whatever its URL says


def _same_origin(one: str, other: str) -> bool:
    try:
        a, b = urlsplit(one), urlsplit(other)
        return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port) and bool(a.hostname)
    except ValueError:
        return False


def _drop_children(documents: dict[str, Document], frame: str) -> None:
    """A frame has a new document: what the old one held is gone, down to its last descendant."""
    gone = [other for other, document in documents.items() if document.parent == frame]
    for child in gone:
        _drop_children(documents, child)
        del documents[child]


class ChromePages(ChromePipe):
    """What each page holds now, by Chrome's word, and whom a hand-off gave it to."""

    def _on_committed(self, session: str | None, frame: dict[str, Any]) -> None:
        """A task's session says what committed, and so which documents the page holds now. A main
        frame's commit that is not where the newest navigation is going is an older one landing
        late, and says nothing of the page that is about to be here, so the URL stays unknown. A
        navigation with no Document request (a blob, an about:blank) is its own proof. A main frame's
        commit starts the page's documents again, so what the last one held is gone, and an iframe
        counts only under a parent that is still there: one left mid-load adds nothing."""
        page = self._page_of.get(session or "")
        if page is None:
            if self._fenced.get(session or "") == BASE and not frame.get("parentId"):
                self._no_fence_saw(BASE, session, frame)
            return
        url, loader = str(frame.get("url", "")), str(frame.get("loaderId", ""))
        task = self._page_task.get(page, "")
        if parent := frame.get("parentId"):
            if not self._public(url):  # under a parent that is gone too: it is running
                self._written(page, task)
            self._on_iframe_committed(
                page, str(frame.get("id")), str(parent), Document(loader, url, str(parent))
            )
            return
        if self._fence_of.get(page) != session:
            return
        if not self._public(url):  # even one the newest navigation does not explain: it is running
            self._written(page, task)
        going = self._navigating.get(page)
        if going is None and page not in self._handed.get(task, ()):
            self._no_fence_saw(task, session, frame)
        fell = str(frame.get("unreachableUrl") or "")
        if not fell:
            self._fell_from.pop(page, None)
        elif going is not None and going.left is not None:
            self._fell_from[page] = going.left
        if going is not None and going.url != url:
            self._unexplained(page, going, fell)
            return
        self._navigating.pop(page, None)
        self._tops[page] = url
        if self._fence.listed(task, url):
            self._last_listed[page] = url
        self._documents[page] = {page: Document(loader, url, None)}
        self._parents.pop(page, None)
        self._tell(loader)
        if (landed := self._landed.get(page)) is not None:
            landed.set()

    def _on_iframe_committed(self, page: str, frame: str, parent: str, document: Document) -> None:
        self._parents.setdefault(page, {})[frame] = parent
        documents = self._documents.get(page, {})
        if parent in documents and document.loader:
            _drop_children(documents, frame)
            documents[frame] = document
            self._tell(document.loader)

    def _unexplained(self, page: str, going: Going, fell: str) -> None:
        """A main frame's commit that is not where the newest navigation is going: that navigation's
        own error page, which is the end of it, or an older one landing late."""
        if fell.partition("#")[0] != going.url:
            self._navigating[page] = going._replace(left=None)  # the page is on neither, now
            return
        del self._navigating[page]
        if (landed := self._landed.get(page)) is not None:
            landed.set()

    def _written(self, page: str, task: str) -> None:
        """`page` holds what a fenced command can write: its pass is over, and every page it opened
        is marked, as is whatever it opens from now on (`_marked`). A page steers those through a
        window reference, and a blank one runs the script it was handed."""
        self._wrote.add(page)
        self._mark_opened(page)
        self.take_back(task)

    def _mark_opened(self, page: str) -> None:
        for opened, info in self._info.items():
            if info.get("openerId") == page and info.get("canAccessOpener") and opened not in self._tainted:
                self._tainted.add(opened)
                self._mark_opened(opened)

    def _marked(self, info: dict[str, Any]) -> bool:
        """A page, as it is born, was opened with a window reference by one that held such a
        document, or by one such a page opened. A `noopener` tab holds no reference."""
        opener = info.get("openerId")
        return bool(info.get("canAccessOpener")) and (opener in self._wrote or opener in self._tainted)

    def _no_fence_saw(self, ref: str, session: str | None, frame: dict[str, Any]) -> None:
        """A page landed off its list on a navigation `Fetch` never paused. A tab or a window with no
        opener starts its first navigation before anything can be said to its session, the service
        worker bypass included, so a worker in the shared profile can answer it past every fence,
        with no request to refuse. So the page is taken off it, and that is kept as a refusal."""
        url = str(frame.get("url", ""))
        if self._off_list(ref, url) and not frame.get("unreachableUrl"):
            self._fence.refused(ref, url, "a service worker answered for a site off the list")
            self._fire("Page.navigate", {"url": "about:blank"}, session)

    def _on_abandoned(self, session: str | None, params: dict[str, Any]) -> None:
        """Chrome cancelled a request. If that was the page's newest navigation (a 204, a download, a
        `window.stop()`), nothing will commit and the page stayed where it was: the URL it last
        committed is true again, for its documents as `_pages_of` and the rest of the rules find
        them, none of which this touches. Chrome's word, on the fence's session. A navigation that
        failed is not cancelled: its error page commits, and tells that itself."""
        page = self._page_of.get(session or "")
        if page is None or self._fence_of.get(page) != session or not params.get("canceled"):
            return
        going = self._navigating.get(page)
        if going is None or not going.network or going.network != params.get("requestId"):
            return
        del self._navigating[page]
        if going.left is not None:
            self._tops[page] = going.left
        if (landed := self._landed.get(page)) is not None:
            landed.set()

    def _on_frame_life(self, session: str | None, params: dict[str, Any]) -> None:
        """`Page.frameAttached` names a parent, and an iframe is born with no document: whose it is,
        is what its first request is judged by. Removed (`Page.frameDetached`), it takes its document
        with it, down to its last descendant. Not a `swap`: that frame lives on in another process,
        and its commit may already have been heard."""
        page, frame = self._page_of.get(session or ""), str(params.get("frameId"))
        if page is None:
            return
        if "parentFrameId" in params:
            self._parents.setdefault(page, {})[frame] = str(params["parentFrameId"])
            self._tell(frame)
            return
        if frame == page or params.get("reason") == "swap":
            return
        self._parents.get(page, {}).pop(frame, None)
        if frame in (documents := self._documents.get(page, {})):
            _drop_children(documents, frame)
            del documents[frame]

    def _on_announced(self, session: str | None, params: dict[str, Any]) -> None:
        """`Network` names who asked, as `Fetch` cannot: this session's own request, a worker's by
        the session, a document's by its frame and loader. Only a task's sessions are heard, and an
        agent's own never: the record of who asked is Chrome's to write and not a page's."""
        if session not in self._page_of or not isinstance(request := params.get("requestId"), str):
            return
        document = (str(params.get("frameId", "")), str(params.get("loaderId", "")))
        self._asked[request] = None if session in self._workers else document
        if len(self._asked) > _ASKED_KEPT:
            del self._asked[next(iter(self._asked))]
        self._tell(request)

    def _tell(self, news: str) -> None:
        if (heard := self._news.pop(news, None)) is not None:
            heard.set()

    async def _news_of(self, news: str, known: Callable[[], bool]) -> bool:
        """Is it `known`, or does it become so within the wait? Events on two channels: which comes
        first is Chrome's business, so a question that has no answer yet waits a little for one."""
        if not known():
            heard = self._news.setdefault(news, asyncio.Event())
            if len(self._news) > _ASKED_KEPT:  # news that never came is never told
                del self._news[next(iter(self._news))]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(heard.wait(), _COMMIT_S)
        return known()

    async def _document_of(self, paused: Paused) -> tuple[str, str] | None:
        """The (frame, loader) that announced this request. None for a worker of any kind: a
        dedicated one's request is paused on its page's session under its page's frame, and it
        outlives that page, so a worker a fenced command's page left running would otherwise be
        excused by the listed page that replaced it. Its session has no document, and that is the
        whole of the decision: a `Referer` or a `Sec-Fetch` header is the worker's own server's to
        shape, so neither can say. None too for a request nothing announced."""
        if not paused.network:
            return None
        await self._news_of(paused.network, lambda: paused.network in self._asked)
        return self._asked.get(paused.network)

    async def _pages_of(self, paused: Paused) -> tuple[str, ...]:
        """The committed URLs from the top of the page a request is from down to its own document,
        given only to a request of a document in that page now. Every document between counts: a
        listed page may frame a task's loopback server, and a `srcdoc` inside that frame has no
        origin to say so, so it is known by what holds it (`cdp_relay.pages_on_list`). Mid-swap
        there is no top to give, so the request waits for the commit, and a waited-for URL is good
        only for a request whose `Referer` is that page's own, and one with no `Referer` is sent
        away, since nothing then says which page it was."""
        page = paused.page or ""
        if (asked := await self._document_of(paused)) is None:
            return ()
        frame, loader = asked
        top = self._tops.get(page)
        if top is None and (landed := self._landed.get(page)) is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(landed.wait(), _COMMIT_S)
            top = self._tops.get(page)
            if top is None or not _same_origin(paused.referrer, top):
                return ()
        if top is None:
            return ()

        def here() -> bool:
            document = self._documents.get(page, {}).get(frame)
            return bool(loader) and document is not None and document.loader == loader

        # A main frame's document is known by now. An iframe's commit can be behind its first asset.
        if not here() and (frame == page or not await self._news_of(loader, here)):
            return ()
        return (top, *self._between(page, frame))

    async def _framing(self, paused: Paused) -> tuple[str, ...]:
        """The committed URLs from the top of the page down to the parent of the iframe this
        Document request is for. The document it asks for is not born until the request is let go,
        so `_pages_of` could only wait for what the request itself holds up: it is its parent's
        request. Never a worker's, and never one nothing announced, and every document between
        counts as in `_pages_of`."""
        page, frame = paused.page or "", paused.frame
        if await self._document_of(paused) is None:
            return ()

        def parent() -> str | None:
            return self._parents.get(page, {}).get(frame)

        if not await self._news_of(frame, lambda: parent() in self._documents.get(page, {})):
            return ()
        top, holder = self._tops.get(page), parent()
        return () if top is None or holder is None else (top, *self._between(page, holder))

    def hand_over(self, ref: str) -> None:
        """The task's agent stopped for its person: until `take_back`, each
        of its pages on a listed site may go wherever the person takes it, as may a page it opens.
        A page on loopback is not handed: a fenced command writes that page, and it could carry the
        command's data anywhere. While any page of the task holds such a document, or was opened by
        one that did (`_written`), nothing is handed, now or when it leaves: a page steers whatever
        it opened, or what opened it, through a window reference, with no person there, and those
        links outlive what Chrome tells of them. Nor is one with no known top handed, nor a tab with
        no opener: only a page the task drives is, by `_owner`, never one merely in its window. A
        page on an error page, its person having clicked before the agent stopped, goes back to the
        listed page it fell from, never on to where it was refused: the agent can write that URL. A
        second call changes no ceiling and takes nothing away: the person may be off the list by
        then, and a page dropped there would be refused mid sign-in."""
        if not self._unwritten(p for p, task in self._page_task.items() if task == ref):
            self.take_back(ref)
            return
        driven = [page for page, task in self._owner.items() if task == ref]
        handed = self._handed.setdefault(ref, set())
        handed.update(
            page
            for page in driven
            if (top := self._tops.get(page)) is not None and self._fence.listed(ref, top)
        )
        for page in driven:
            back = self._fell_from.get(page)
            if page not in self._tops and back is not None and self._fence.listed(ref, back):
                handed.add(page)
                self._fire("Page.navigate", {"url": back}, self._fence_of.get(page))
        if ref not in self._pass_began:
            began = self._pass_began[ref] = self.clock()
            self._spawn(self._ceiling(ref, began))

    def take_back(self, ref: str) -> None:
        """The task's agent is acting again, or was never parked, or nobody is left to be parked on:
        its list alone holds again. A page the pass left on a site off that list goes back to the
        last listed page it was on, or to about:blank: what a person's sign-in shows there must not
        reach the agent, and if it needs that site it asks, as for any other. So does a page of the
        task's that no pass carried, since the agent reads those too. A page on a listed site stays
        where it is, and so does one on the way to a listed site from one, and a page coming home
        from off the list is waited for (`_comes_home`): until it lands it holds what it left."""
        if ref not in self._pass_began:
            return
        self._end_pass(ref)
        LOG.info("the hand-off of %s is over", ref)
        pages = [page for page, task in self._owner.items() if task == ref]
        stranded = {page: back for page in pages if (back := self._way_back(ref, page)) is not None}
        coming = [page for page in pages if page not in stranded and self._coming_home(ref, page)]
        staying = [page for page in pages if page not in stranded and page not in coming]
        framing = [page for page in staying if self._frames_off(ref, page)]
        if stranded or coming or framing:
            done = self._going_back[ref] = asyncio.Event()
            self._spawn(self._send_back(ref, stranded, coming, framing, done))

    def _end_pass(self, ref: str) -> None:
        self._pass_began.pop(ref, None)
        self._handed.pop(ref, None)

    def _pass_live(self, ref: str) -> bool:
        """Inside the ceiling. A clock that went backwards says nothing of how long it has been, and
        what cannot be told is over."""
        began = self._pass_began.get(ref)
        return began is not None and 0 <= self.clock() - began < PASS_S

    async def _ceiling(self, ref: str, began: float) -> None:
        """`_carries` stops at the ceiling by itself; this ends the pass there with no request asked
        of it, so a page is sent back with the person gone. The gate stays open, for them."""
        while self._pass_began.get(ref) == began:
            await asyncio.sleep(_PASS_TICK_S)
            if self._pass_began.get(ref) == began and not self._pass_live(ref):
                self.take_back(ref)

    def _way_back(self, ref: str, page: str) -> str | None:
        """Where a page goes when its pass ends, or None for where it is, or is being taken. One in
        no known state goes back. A site is off the list by the list the pass ended under."""
        going = self._navigating.get(page)
        here = going.url if going is not None else self._tops.get(page)
        if here is not None and not self._off_list(ref, here):
            return None
        return self._back_to(ref, page)

    def _back_to(self, ref: str, page: str) -> str:
        last = self._last_listed.get(page)
        return last if last is not None and self._fence.listed(ref, last) else "about:blank"

    def _coming_home(self, ref: str, page: str) -> bool:
        """It is on its way, and what it holds until it lands is off the list, or unknown."""
        going = self._navigating.get(page)
        return going is not None and (going.left is None or self._off_list(ref, going.left))

    def _off_list(self, ref: str, url: str) -> bool:
        site = url.partition(":")[2] if url.lower().startswith("blob:") else url
        return self._fence.public(site) and not self._fence.listed(ref, site)

    def shown(self, ref: str, url: str) -> tuple[str, str | None]:
        """What the agent may read of a URL: as it is, or of a site off its list, the host and path
        alone, with that host for its title (else None: the title may stay). A history keeps a
        sign-in's URL whole, a code in its query and a token in its fragment."""
        if not self._off_list(ref, url):
            return url, None
        parts = urlsplit(url.partition(":")[2] if url.lower().startswith("blob:") else url)
        host = parts.hostname or ""
        where = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, where, parts.path, "", "")), host

    async def _send_back(
        self,
        ref: str,
        stranded: dict[str, str],
        coming: list[str],
        framing: list[str],
        done: asyncio.Event,
    ) -> None:
        try:
            await asyncio.gather(
                *(self._return(ref, page, url) for page, url in stranded.items()),
                *(self._comes_home(ref, page) for page in coming),
                *(self._drop_frames(ref, page) for page in framing),
            )
        finally:
            done.set()
            if self._going_back.get(ref) is done:
                del self._going_back[ref]

    async def _comes_home(self, ref: str, page: str) -> None:
        """A page off the list with a listed site on its way: a sign-in that lands keeps its page,
        and nothing may read what it holds until then. One that does not land by `_HOME_S`, or that
        Chrome cancels (a 204, a `window.stop()`, Esc), is where it was, and goes back."""
        if (landed := self._landed.get(page)) is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(landed.wait(), _HOME_S)
        if page in self._navigating or self._way_back(ref, page) is not None:
            await self._return(ref, page, self._back_to(ref, page))
        else:
            await self._drop_frames(ref, page)

    def _frames_off(self, ref: str, page: str) -> list[str]:
        """The iframes of a page holding a site off the list, parents before what they hold."""
        documents = self._documents.get(page, {})
        return [f for f, d in documents.items() if f != page and self._off_list(ref, d.url)]

    async def _drop_frames(self, ref: str, page: str) -> None:
        """A listed page stays, with what the person typed in it, and what a sign-in framed there
        goes: a screenshot reaches a frame in another process, a name in a session frame with it.
        Not a reload, which would lose a solved captcha. A frame that will not go takes its page."""
        for frame in self._frames_off(ref, page):
            if frame not in self._frames_off(ref, page):
                continue  # went with its parent
            session = next((s for s, f in self._iframes.items() if f == frame), self._fence_of.get(page))
            with contextlib.suppress(ChromeError, TimeoutError):
                said = await self.call(
                    "Page.navigate",
                    {"url": "about:blank", "frameId": frame},
                    session=session,
                    timeout=_BACK_S,
                )
                news = str(said.get("loaderId"))
                await self._news_of(news, lambda f=frame: f not in self._frames_off(ref, page))
        if self._frames_off(ref, page):
            LOG.warning("a frame left off the list would not go: closing its page")
            self._fire("Target.closeTarget", {"targetId": page})

    async def _return(self, ref: str, page: str, url: str) -> None:
        """`Page.navigate` answers once the page has committed or failed. A listed site too slow to
        wait for, or one that sends nothing back any more (a 204 is an `errorText`, and the page
        stays), gives way to about:blank, and a page that will not leave at all (a `beforeunload`
        dialog with nobody to answer it) is closed: the agent's next read must not find it. Each
        try starts clean: whatever navigation was under way, this replaces it."""
        if (session := self._fence_of.get(page)) is None:
            return
        for there in dict.fromkeys((url, "about:blank")):
            self._navigating.pop(page, None)
            with contextlib.suppress(ChromeError, TimeoutError):
                said = await self.call("Page.navigate", {"url": there}, session=session, timeout=_BACK_S)
                news = str(said.get("loaderId"))
                if not said.get("errorText") and await self._news_of(news, lambda: self._left(ref, page)):
                    return
        LOG.warning("a page left off the list would not go back: closing it")
        self._fire("Target.closeTarget", {"targetId": page})

    def _left(self, ref: str, page: str) -> bool:
        top = self._tops.get(page)
        return page not in self._navigating and top is not None and not self._off_list(ref, top)

    def going_back(self, ref: str) -> asyncio.Event | None:
        """Set once the pages `take_back` sent back have left; None: none is on its way."""
        return self._going_back.get(ref)

    async def _handed_on(self, paused: Paused, owner: str | None) -> bool:
        """Is this a request of a page the person is driving? Its own navigation, an iframe's on a
        document in it now, or a request of a document in it now, with no loopback frame on the way
        down. Never a worker's."""

        def handed() -> bool:
            return self._carries(owner or "", paused.page or "")

        if not handed():
            return False
        if paused.navigation:
            return True
        framing = paused.kind == "Document"
        pages = await (self._framing(paused) if framing else self._pages_of(paused))
        return handed() and bool(pages) and all(self._public(page) for page in pages)

    def _carries(self, ref: str, page: str) -> bool:
        """Handed, and nothing in the task a command wrote: a frame can navigate its top or open a
        popup, and another page can steer this one (`hand_over`)."""
        task = (p for p, owner in self._page_task.items() if owner == ref)
        return page in self._handed.get(ref, ()) and self._pass_live(ref) and self._unwritten(task)

    def _unwritten(self, pages: Iterable[str]) -> bool:
        """No page here holds what a command can write, nor is one on its way to it (a page's first
        assets can beat word of its commit), nor did such a page open it (`_written`)."""

        def clean(page: str) -> bool:
            going = self._navigating.get(page)
            documents = self._documents.get(page, {}).values()
            return (
                page not in self._tainted
                and (going is None or self._public(going.url))
                and all(self._public(document.url) for document in documents)
            )

        return all(clean(page) for page in pages)

    def _public(self, url: str) -> bool:
        """A document with no origin of its own (about:blank, srcdoc) rides on what holds it. A blob
        has its maker's origin; `data:` and `file:` have no host, and whoever wrote the URL wrote the page."""
        scheme = urlsplit(url).scheme.lower()
        if scheme == "blob":
            return self._public(url.partition(":")[2])
        return scheme not in _WRITTEN and (scheme not in ("http", "https") or self._fence.public(url))

    def _between(self, page: str, frame: str) -> tuple[str, ...]:
        """The URLs of the frames from under `page`'s top down to `frame`."""
        documents = self._documents.get(page, {})
        urls: list[str] = []
        while frame != page and (document := documents.get(frame)) is not None:
            urls.append(document.url)
            frame = document.parent or page
        return tuple(reversed(urls))
