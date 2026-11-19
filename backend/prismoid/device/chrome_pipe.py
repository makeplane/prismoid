# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome's process and its DevTools pipe: launching it, reading what it says, sending it
commands, and what one Chrome life knows (`chrome.py` says what it is all for).

The base of the `Chrome` layers. `start` launches on the pipe, the reading loop cuts messages at
their NULs (one too large to keep is dropped as it passes, `_Frames`), and `send` and `call` are the
one way a command reaches Chrome. What a message means is `Chrome._dispatch`'s.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple
from urllib.parse import unquote, urlsplit

from prismoid.device.chrome_types import Document, Fence, Going, NoFence, Relay, Verdict

# One logger for the Chrome, however many files it is told in.
LOG = logging.getLogger("prismoid.device.chrome")

CHROME_DIR = "chrome"
MAX_MESSAGE: Final = 64 * 1024 * 1024  # a full-page screenshot, base64, fits
LAUNCH_FLAGS: Final = (
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    # Chrome's own traffic - updates, safe browsing, sync, component updates - is off rather than
    # allowlisted: the proxy holds Chrome to the tasks' lists, and `CHROME_BASE` stays empty.
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    # Chrome's component extensions keep background pages and workers that start with it and sit
    # outside the inner layer (`_BROWSER_ATTACH`), and an extension installed in this profile would
    # too. Off: nothing of Prismoid's is an extension, and the PDF viewer has no background page, so
    # it stays (probed against Chrome 156).
    "--disable-component-extensions-with-background-pages",
    "--disable-extensions",
    # A window behind another one (the person's own, or a task's) stays a page that renders: hidden,
    # it runs no animation frame, and a pointer move waits on one. Measured at `-n 5`.
    "--disable-backgrounding-occluded-windows",
    # Requests the browser or the network service makes itself, which `Fetch` never pauses: uploads
    # (Reporting, NEL), and what a script can ask of it (Background Fetch, FedCM's config, a payment
    # method's manifest). On Chrome 156 each reached a host holding an asset's pass, but NEL, which
    # was not seen to send and shares Reporting's uploader. The back/forward cache restores a page
    # with no Document request for `_on_committed` to see.
    "--disable-features=OptimizationHints,MediaRouter,Reporting,NetworkErrorLogging,FedCm,BackForwardCache,WebPayments",
    "--disable-blink-features=BackgroundFetch,FedCm",
    "--metrics-recording-only",
    "--disable-domain-reliability",
    # WebRTC's UDP would leave beside the proxy, past both layers: only proxied connections.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    # Chrome sends loopback past a proxy by default, and `Fetch` never pauses a WebSocket's
    # handshake: a page's socket to any port on this Mac (Prismoid's own) would pass both layers. So
    # loopback goes to the proxy as well, which refuses it - the fail-closed floor.
    "--proxy-bypass-list=<-loopback>",
    # No window of its own: every window is a task's.
    "--no-startup-window",
)
ALL_REQUESTS: Final = [{"urlPattern": "*"}]
HELD: Final = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
# What the browser-wide session attaches: pages and the workers no page owns. A page's own iframes
# and workers are attached under the page's session, so they are its task's.
_BROWSER_ATTACH: Final = {
    **HELD,
    "filter": [{"type": "page"}, {"type": "service_worker"}, {"type": "shared_worker"}, {"exclude": True}],
}
# What Chrome's environment keeps of the helper's: never a database URL, a model key or a token.
_ENV_KEYS: Final = frozenset({"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG"})
_READ: Final = 1024 * 1024  # off the pipe at a time
_ENDS_KEPT: Final = 4096  # of each end of a message too large to read
# What the ends of such a message say. Chrome writes these fields before and after anything a page
# chose, and a page's quote in between is escaped, so none of this can be a page's own words.
_DECODER: Final = json.JSONDecoder()
_READ_INTO: Final = frozenset({"params", "params.request"})  # no deeper: a page names what is there
_CALL_S: Final = 30.0
_START_S: Final = 15.0
GONE: Final = "the Prismoid Chrome closed"
TOO_LARGE: Final = (
    "a request too large for Prismoid's fence to read: its body can be 9 to 27 MiB, by what it holds"
)

Reply = Callable[[dict[str, Any]], None]


class ChromeError(RuntimeError):
    """The Prismoid Chrome could not start, refused a call, or went away."""


class ChromeMissing(ChromeError):
    """No Chrome where `PRISMOID_LOCAL_CHROME_PATH` says; the message says so, and how to fix it."""


class ChromeRefused(ChromeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def missing(chrome_path: str) -> str | None:
    """Why the Prismoid Chrome cannot start from `chrome_path`, or None: the one sentence the helper's
    frame door and `prismoid up` both say."""
    if os.path.isfile(chrome_path) and os.access(chrome_path, os.X_OK):
        return None
    return (
        f"Google Chrome was not found at {chrome_path}, and coding and browser tasks need it: install Google "
        "Chrome, or set PRISMOID_LOCAL_CHROME_PATH to its executable "
        "(…/Google Chrome.app/Contents/MacOS/Google Chrome)"
    )


def chrome_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The little of `environ` Chrome is given: who and where the user is, the PATH, the locale."""
    return {k: v for k, v in environ.items() if k in _ENV_KEYS or k.startswith("LC_")}


def _launch(path: str, args: list[str]) -> tuple[int, int, int]:
    """Start Chrome on the DevTools pipe - its fd 3 reads our commands, its fd 4 writes its answers -
    and return (pid, the fd we read, the fd we write). `posix_spawn` places the two fds (no
    `preexec_fn` in a threaded process), and `setsid` makes Chrome its own group, stopped as one."""
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    # Moved above 4 first, so placing one at 3 never overwrites the other before it is placed at 4.
    high = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in (to_chrome_r, from_chrome_w)]
    os.close(to_chrome_r)
    os.close(from_chrome_w)
    try:
        pid = os.posix_spawn(
            path,
            [path, *args],
            chrome_env(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_DUP2, high[0], 3),
                (os.POSIX_SPAWN_DUP2, high[1], 4),
            ],
            setsid=True,
        )
    except OSError:
        os.close(from_chrome_r)
        os.close(to_chrome_w)
        raise
    finally:
        for fd in high:
            os.close(fd)
    return pid, from_chrome_r, to_chrome_w


def _ignore(_answer: dict[str, Any]) -> None:
    pass


class _TooLarge(NamedTuple):
    """A message past `MAX_MESSAGE`: its two ends, and how long it was."""

    head: bytes
    tail: bytes
    size: int


class _Ends:
    """What the two ends of a message too large to keep say, in whatever order Chrome wrote its
    fields. Walked by depth, never searched for: a page's quotes are escaped, but a page chooses the
    keys of `request.headers`, so a name counts only at its own depth."""

    def __init__(self, message: _TooLarge) -> None:
        self.head = message.head.decode(errors="replace")
        self.tail = message.tail.decode(errors="replace")
        self.read: dict[str, object] = {}
        with contextlib.suppress(ValueError, IndexError):  # the cut, wherever it fell
            self._members(1, "")
        # `}}`: `params` ends the message, so no field past it can have been out of sight.
        self.closed_at_params = self.tail.endswith("}}")

    def _members(self, at: int, within: str) -> int:
        """Read an object's members from just past its `{`; the index past its `}`."""
        while self.head[at] != "}":
            at += self.head[at] == ","
            key, at = _DECODER.raw_decode(self.head, at)
            name, at = f"{within}{key}", at + 1  # past the colon
            if name in _READ_INTO and self.head[at] == "{":
                at = self._members(at + 1, f"{name}.")
            else:
                self.read[name], at = _DECODER.raw_decode(self.head, at)
        return at + 1

    def field(self, name: str) -> str | None:
        """`name`, dotted from the top of the message: as the head read it, or else as the tail ends
        with it. None when neither end holds it."""
        if (value := self.read.get(name)) is not None:
            return str(value)
        *path, key = name.split(".")
        for found in reversed(list(re.finditer(rf'"{key}":(?:"([^"\\]+)"|(\d+))', self.tail))):
            if self._closes(found.end(), len(path) + 1):
                return found[1] or found[2]
        return None

    def _closes(self, at: int, depth: int) -> bool:
        """From `at` the tail holds only whole members and the `}` of each of `depth` objects, and
        then it ends: so what stands just before `at` is exactly that deep."""
        try:
            while depth:
                if self.tail[at] == "}":
                    depth, at = depth - 1, at + 1
                elif self.tail[at] == ",":
                    _, at = _DECODER.raw_decode(self.tail, at + 1)
                    _, at = _DECODER.raw_decode(self.tail, at + 1)
                else:
                    return False
        except (ValueError, IndexError):
            return False
        return at == len(self.tail)


class _Frames:
    """The DevTools pipe cut at its NULs. A message past `limit` stops being kept as it arrives, so
    no size is too large to get past: only its two ends come out."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self._head: bytes | None = None  # set: the message arriving is past the limit
        self._tail = b""
        self._size = 0

    def feed(self, data: bytes) -> Iterator[bytes | _TooLarge]:
        start = 0
        while (end := data.find(b"\0", start)) >= 0:
            self._keep(data[start:end])
            yield self._whole()
            start = end + 1
        self._keep(data[start:])

    def _keep(self, piece: bytes) -> None:
        if self._head is None:
            self._buffer += piece
            if len(self._buffer) <= self._limit:
                return
            self._head, self._tail = bytes(self._buffer[:_ENDS_KEPT]), bytes(self._buffer[-_ENDS_KEPT:])
            self._size = len(self._buffer)
            self._buffer.clear()
            return
        self._size += len(piece)
        self._tail = (self._tail + piece)[-_ENDS_KEPT:]

    def _whole(self) -> bytes | _TooLarge:
        if self._head is None:
            whole = bytes(self._buffer)
            self._buffer.clear()
            return whole
        over = _TooLarge(self._head, self._tail, self._size)
        self._head, self._tail, self._size = None, b"", 0
        return over


class ChromePipe(abc.ABC):
    """One Chrome process at a time on the DevTools pipe, and what its life knows."""

    def __init__(
        self,
        home: Path,
        chrome_path: str,
        proxy: str,
        *,
        fence: Fence | None = None,
        socket_proxy: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`proxy` is the helper's egress proxy with the `chrome` principal's credential in it
        (`DeviceEgress.chrome_url`): only its origin reaches Chrome's argv; the credential answers
        the proxy's challenges over CDP. `socket_proxy` is that proxy's second door, for `http:` and
        every WebSocket (`DeviceEgress.chrome_socket_origin`); without one, all goes to `proxy`."""
        self.home, self.chrome_path = home, chrome_path
        parts = urlsplit(proxy)
        self._proxy_origin = f"{parts.scheme}://{parts.hostname}:{parts.port}"
        self._socket_proxy = socket_proxy
        self._credential = (unquote(parts.username or ""), unquote(parts.password or ""))
        self._closed: set[str] = set()  # tasks ended: no window for them, whichever Chrome this is
        self._fence: Fence = fence or NoFence()
        self.pid: int | None = None
        self._lock = asyncio.Lock()  # one start or stop at a time
        self._creating = asyncio.Lock()  # a window being made: its first page is claimed under it
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._jobs: set[asyncio.Task[None]] = set()
        self._relays: dict[str, set[Relay]] = {}
        self._writer: asyncio.WriteTransport | None = None
        self._reader: asyncio.ReadTransport | None = None
        self._pump: asyncio.Task[None] | None = None
        self._reaper: asyncio.Task[None] | None = None
        self._no_more = False  # the helper is stopping (`no_more_launches`)
        self.clock = clock  # the wall clock: monotonic time stops while a Mac sleeps
        self._reset()

    def _reset(self) -> None:
        """What one Chrome life knows; none of it outlives the pipe it came on."""
        self._next_id = 0
        self._pending: dict[int, Reply] = {}
        self._info: dict[str, dict[str, Any]] = {}  # every target Chrome has told of
        self._owner: dict[str, str] = {}  # a task's page -> its frame ref, in the order they joined
        self._attached: set[str] = set()  # the sessions of targets being fenced, until they detach
        self._fenced: dict[str, str | None] = {}  # a fence session -> its owner (None: no one task)
        self._fence_of: dict[str, str] = {}  # a fenced page -> its fence session
        self._page_task: dict[str, str] = {}  # a fenced page -> the task whose list it is held to
        self._page_of: dict[str, str] = {}  # a task's fence session, a child's too -> its page
        self._iframes: dict[str, str] = {}  # the fence session of an iframe in its own process -> it
        self._tops: dict[str, str] = {}  # a page -> its committed top-level URL; absent: unknown
        self._fell_from: dict[str, str] = {}  # a page on an error page -> the top it fell from
        self._navigating: dict[str, Going] = {}  # a page -> its newest navigation, uncommitted
        self._landed: dict[str, asyncio.Event] = {}  # a page -> set when that navigation commits
        self._documents: dict[str, dict[str, Document]] = {}  # a page -> its documents now, by frame
        self._parents: dict[str, dict[str, str]] = {}  # a page -> each iframe's parent frame
        self._wrote: set[str] = set()  # the pages that ever held what a command can write
        self._tainted: set[str] = set()  # the pages such a page opened, with a window reference
        self._handed: dict[str, set[str]] = {}  # a task in a hand-off -> the pages its person drives
        self._pass_began: dict[str, float] = {}  # a task in a hand-off -> when, by `clock`
        self._last_listed: dict[str, str] = {}  # a page -> the last top it held on its list
        self._going_back: dict[str, asyncio.Event] = {}  # a task -> set when its pages have left
        self._workers: set[str] = set()  # the sessions of a task's workers, which have no document
        # A request -> the (frame, loader) that announced it; None: a worker's.
        self._asked: dict[str, tuple[str, str] | None] = {}
        self._news: dict[str, asyncio.Event] = {}  # what `_news_of` is waiting to hear
        self._verdicts: dict[str, asyncio.Future[Verdict]] = {}  # every page held at its start
        self._task_windows: dict[int, str] = {}  # a window a task's page is in -> that task
        self._claims: dict[str, str] = {}  # a window's first page, being made -> its task
        self._ready: dict[str, asyncio.Future[None]] = {}  # a claimed page, until it is fenced
        # task -> (the page a tab is being opened from, that tab once it is fenced)
        self._opening: dict[str, tuple[str, asyncio.Future[str]]] = {}
        self._windows: dict[str, str] = {}  # task -> its window's first page
        self._bound: dict[str, Relay] = {}  # a session a relay opened -> that relay
        # Requests given the credential once (a refused one is not looped), the newest kept.
        self._auth_tried: dict[str, None] = {}
        self._seeded = False  # this Chrome has the credential for the socket door

    @property
    def running(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ── the process and the pipe ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the Prismoid Chrome unless it runs (idempotent), and never once the helper is stopping."""
        async with self._lock:
            if self._no_more:
                raise ChromeError(GONE)
            if self.running:
                return
            await self._let_go()
            if why := missing(self.chrome_path):
                raise ChromeMissing(why)
            profile = self.home / CHROME_DIR
            profile.mkdir(parents=True, exist_ok=True)
            profile.chmod(0o700)
            # The disk cache too: on macOS it would otherwise go to ~/Library/Caches/<the profile's
            # path under Application Support>, outside the read-denied home - for the default home,
            # inside the agent's own cache root.
            args = [
                *LAUNCH_FLAGS,
                f"--user-data-dir={profile}",
                f"--disk-cache-dir={profile / 'cache'}",
                f"--proxy-server={self._proxy_rules()}",
            ]
            try:
                pid, read_fd, write_fd = _launch(self.chrome_path, args)
            except OSError as e:
                raise ChromeError(f"Chrome at {self.chrome_path} could not start: {e.strerror}") from None
            self.pid, self._reaper = pid, None
            try:
                await self._connect(read_fd, write_fd)
            except BaseException as e:
                self._gone(GONE)
                await (self._reaper if self._reaper is not None else self._reap(pid))
                if isinstance(e, TimeoutError) or (isinstance(e, ChromeRefused) and e.message == GONE):
                    raise ChromeError(
                        f"Chrome at {self.chrome_path} exited or went silent as it started: is another "
                        f"Chrome using {profile}?"
                    ) from None
                if isinstance(e, ChromeError):
                    raise ChromeError(f"Chrome at {self.chrome_path} did not start: {e}") from None
                raise

    async def _let_go(self) -> None:
        """The last Chrome ends here, and not under the next: a pipe still closing is ended now, and
        its reader and its process are waited out, since a Chrome still exiting holds the profile
        and hands a new one straight back to it."""
        self._gone(GONE)
        for last in (self._pump, self._reaper):
            if last is not None:
                await asyncio.wait({last})
        self._pump = self._reaper = None

    def _proxy_rules(self) -> str:
        """Chrome's proxy per URL scheme. It sends a WebSocket to the `socks` entry when there is
        one, and that entry may name an http proxy: so `socks` is the socket door, and no SOCKS is
        spoken. Without it a WebSocket would share `https:`'s proxy, and with it an asset's open
        HTTP/2 session."""
        if self._socket_proxy is None:
            return self._proxy_origin
        https, sockets = urlsplit(self._proxy_origin).netloc, urlsplit(self._socket_proxy).netloc
        return f"http={sockets};https={https};socks={self._socket_proxy}"

    async def connect(self, read_fd: int, write_fd: int) -> None:
        """Take over a Chrome already on the DevTools pipe: `start`'s second half, and a test's way
        to put a scripted Chrome here."""
        async with self._lock:
            await self._connect(read_fd, write_fd)

    async def _connect(self, read_fd: int, write_fd: int) -> None:
        loop = asyncio.get_running_loop()
        stream = asyncio.StreamReader(limit=_READ)
        self._reader, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stream), os.fdopen(read_fd, "rb", 0)
        )
        self._writer, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(write_fd, "wb", 0))
        self._pump = asyncio.create_task(self._read(stream))
        await self.call(
            "Fetch.enable", {"handleAuthRequests": True, "patterns": ALL_REQUESTS}, timeout=_START_S
        )
        # A download lands in ~/Downloads, outside every grant: none, for a task or anyone else.
        await self.call("Browser.setDownloadBehavior", {"behavior": "deny"}, timeout=_START_S)
        await self.call("Target.setDiscoverTargets", {"discover": True}, timeout=_START_S)
        await self.call("Target.setAutoAttach", _BROWSER_ATTACH, timeout=_START_S)

    async def _read(self, stream: asyncio.StreamReader) -> None:
        """The one reading loop. Only the pipe ending ends it, whatever comes down it. No next
        Chrome starts until this has ended (`_let_go`), so what it ends is always its own."""
        frames = _Frames(MAX_MESSAGE)
        try:
            while chunk := await stream.read(_READ):
                for message in frames.feed(chunk):
                    self._take(message)
        except (ConnectionError, OSError):
            pass
        finally:
            self._gone(GONE)

    def _take(self, message: bytes | _TooLarge) -> None:
        """One message off the pipe: no message, however bad, ends the reading loop."""
        try:
            if isinstance(message, _TooLarge):
                self._too_large(message)
            else:
                self._dispatch(json.loads(message))
        except Exception:
            LOG.exception("a message from Chrome could not be handled")

    def _gone(self, why: str) -> None:
        """The pipe ended - Chrome exited, crashed, or was closed: every call in flight fails, every
        relay ends, Chrome is reaped, and the next browser frame launches a new one. Idempotent."""
        if self._writer is None:
            return
        for transport in (self._writer, self._reader):
            if transport is not None:
                transport.close()  # the reader's end is an EOF: the reading loop ends
        self._writer = self._reader = None
        pending, ready = self._pending, self._ready
        relays = [r for rs in self._relays.values() for r in rs]
        self._relays = {}
        self._reset()
        for reply in pending.values():
            reply({"error": {"code": -32000, "message": why}})
        for waiting in ready.values():
            if not waiting.done():
                waiting.set_exception(ChromeError(why))
        for relay in relays:
            relay.on_lost(why)
        if self.pid is not None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap(self.pid))

    async def _reap(self, pid: int) -> None:
        """Wait out a Chrome that is going (5 s), then kill what is left of its group."""
        with contextlib.suppress(ChildProcessError):
            for _ in range(100):
                if os.waitpid(pid, os.WNOHANG)[0]:  # noqa: ASYNC222 - WNOHANG never blocks
                    break
                await asyncio.sleep(0.05)
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
                await asyncio.to_thread(os.waitpid, pid, 0)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)  # a renderer outliving its browser

    def no_more_launches(self) -> None:
        """The helper is stopping: a dial or a window asked for now is refused, and starts nothing."""
        self._no_more = True

    async def close(self) -> None:
        """The helper is stopping: Chrome closes (flushing its cookies), and dies if it will not."""
        async with self._lock:
            if self.running:
                with contextlib.suppress(ChromeError, TimeoutError):
                    await self.call("Browser.close", timeout=3.0)
            self._gone(GONE)
            for task in filter(None, (self._pump, self._reaper)):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._pump = self._reaper = None
            for job in tuple(self._jobs):
                job.cancel()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        job = asyncio.get_running_loop().create_task(work)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    # ── calls ────────────────────────────────────────────────────────────────────────────────

    def send(self, method: str, params: dict[str, Any] | None, session: str | None, reply: Reply) -> None:
        """One command; `reply` gets Chrome's answer (`result` or `error`) on the reading loop, in
        Chrome's own order among the events - so a relay passing it on keeps that order."""
        if not self.running:
            reply({"error": {"code": -32000, "message": GONE}})
            return
        assert self._writer is not None
        self._next_id += 1
        msg: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session is not None:
            msg["sessionId"] = session
        self._pending[self._next_id] = reply
        self._writer.write(json.dumps(msg).encode() + b"\0")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: str | None = None,
        timeout: float = _CALL_S,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def reply(answer: dict[str, Any]) -> None:
            if fut.done():
                return
            if error := answer.get("error"):
                fut.set_exception(ChromeRefused(int(error.get("code", 0)), str(error.get("message", ""))))
            else:
                fut.set_result(answer.get("result") or {})

        sent = self._next_id + 1
        self.send(method, params, session, reply)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(sent, None)

    def _fire(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        self.send(method, params, session, _ignore)

    @abc.abstractmethod
    def _dispatch(self, msg: dict[str, Any]) -> None:
        """One whole message off the pipe, in Chrome's order."""

    def _too_large(self, message: _TooLarge) -> None:
        """A message `_Frames` would not keep. An answer fails its call, so nobody waits for it for
        ever. A request is refused on the session it was paused on, or its challenge cancelled: never
        excused, and never left paused. Any other event goes unheard: each of those can only leave
        something unknown, and every unknown refuses. Its fields are read wherever Chrome wrote them
        (`_Ends`). A `Fetch` request whose id or session cannot be told stays paused and says so: it
        is never excused, and no refusal goes to a session that is a guess."""
        LOG.warning("a message of %d bytes from Chrome was too large to read", message.size)
        ends = _Ends(message)
        if (call := ends.field("id")) is not None:
            if (reply := self._pending.pop(int(call), None)) is not None:
                reply({"error": {"code": -32000, "message": "Chrome's answer was too large to read"}})
            return
        method = ends.field("method")
        if method is not None and method not in ("Fetch.requestPaused", "Fetch.authRequired"):
            return
        request_id, where = ends.field("params.requestId"), ends.field("sessionId")
        if method is None or request_id is None or (where is None and not ends.closed_at_params):
            LOG.error("a request too large to read could not be named, and stays paused")
            return
        if method == "Fetch.authRequired":
            self._fire(
                "Fetch.continueWithAuth",
                {"requestId": request_id, "authChallengeResponse": {"response": "CancelAuth"}},
                where,
            )
            return
        self._fire("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}, where)
        if where in self._fenced:  # the browser-wide session's look at it is the same request's
            self._fence.refused(self._fenced[where], ends.field("params.request.url") or "", TOO_LARGE)
