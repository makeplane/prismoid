# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker filtering proxy: a frame's only way to Docker.

Docker's socket is root-equivalent: a command that reaches it can mount `~` into a container and
walk out of the fence. So the fence admits no Docker socket but this proxy's, one per frame with
the Docker grant (`DOCKER_HOST`), which forwards the Docker API to the user's own daemon under
`docker_policy` - this file is the request path, the lookups that prove an object is the frame's,
and the helper's sockets (`DockerProxies`); `docker_wire.py` reads and writes the API, and
`docker_cleanup.py` stops and removes what a frame's containers leave.

Each connection carries one request, framed by the proxy (`Connection: close` upstream), so
nothing rides past the vetted one; a head that does not arrive in `_HEAD_TIMEOUT_S` ends its
connection, and a proxy serves at most `_MAX_CONNECTIONS` at once. A hijacked stream (attach,
exec start, BuildKit's `/session` and `/grpc`) is relayed raw once the daemon has switched to it.
Every refusal names what was refused and why and never a real host path.

Docker's own egress (image pulls, a container's network) goes out through the daemon's VM, not
the frames' egress proxy: it is not fenced (an honest limit, ADR-078). BuildKit's `/grpc` is its
whole control API, relayed unvetted for `docker build`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlencode

from prismoid.device.docker_cleanup import Unfinished, existing_of, remove_labelled, stop_labelled
from prismoid.device.docker_journal import CAP, Journal
from prismoid.device.docker_policy import (
    FRAME_LABEL,
    TASK_LABEL,
    Verdict,
    reads_body,
    recheck,
    vet_request,
)
from prismoid.device.docker_wire import (
    HEAD_LIMIT,
    LOG,
    TOKEN,
    UPSTREAM_ERRORS,
    BadRequest,
    Head,
    body_length,
    call,
    copy_body,
    created_id,
    daemon_refusal,
    error_bytes,
    forward_head,
    labels_of,
    lookup_path,
    parse_head,
    read_body,
    read_whole,
    relay_answer,
    relay_hijacked,
    relay_response,
    removal_path,
    request_line,
    send_answer,
)
from prismoid.device.frames import DeviceFrameState

_SUN_PATH_MAX: Final = 103  # macOS `sun_path` is 104 bytes, its NUL included
# The helper stopping or starting (`DockerProxies.stop_all`): a container's own grace, and how long
# Docker gets in all. A daemon that is down or slow costs this wait and nothing else.
_QUICK_GRACE_S: Final = 1
STOP_DOCKER_S: Final = 4.0
_HEAD_TIMEOUT_S: Final = 30.0  # a request head (and a vetted body) that has not arrived ends here
_MAX_CONNECTIONS: Final = 64  # per frame: a stream (logs -f, attach) holds one while it runs
_RETRY_EVERY_S: Final = 300.0  # a pending cleanup is tried again this often, besides on demand
_ANONYMOUS: Final = "com.docker.volume.anonymous"  # the label Docker gives a volume it named itself
# Where the Docker CLI lives when the helper's PATH does not say (Docker Desktop, OrbStack, brew).
_DOCKER_CLIS: Final = (
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "~/.orbstack/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
)


def socket_path(home: Path, ref: str) -> Path:
    """A frame's filtering socket, `run/docker-<hash of ref>.sock`: a ref with a task's uuid would
    overflow macOS's 104-byte socket path in the default home."""
    return home / "run" / f"docker-{hashlib.blake2b(ref.encode(), digest_size=8).hexdigest()}.sock"


def roots_of(frame: DeviceFrameState) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What the frame's containers may mount, as real paths: its grants and its work folder (all),
    and of them its read-only grants."""
    ro = tuple(frame.granted("ro"))
    rw = (*frame.granted("rw"), os.path.realpath(frame.paths.work))
    return tuple(dict.fromkeys((*rw, *ro))), ro


@dataclass(frozen=True)
class _Vetted:
    """A request the policy passed, its body read if the policy reads it."""

    head: Head
    method: str
    target: str
    length: int | None
    verdict: Verdict


class _Proxy:
    def __init__(
        self,
        ref: str,
        path: Path,
        upstream: str,
        task_id: str,
        roots: Sequence[str],
        *,
        journal: Journal,
        read_only: Sequence[str] = (),
        lock: asyncio.Lock | None = None,
        volumes: asyncio.Lock | None = None,
        protected: Sequence[str] = (),
    ) -> None:
        self.ref, self.path, self.upstream, self.task_id = ref, path, upstream, task_id
        self.roots, self.read_only = tuple(roots), tuple(read_only)
        self.protected = tuple(protected)  # what a container never writes (`docker_policy`)
        self.journal = journal
        # The frame's lock (`DockerProxies`): what makes an object, and the journal with it, runs
        # under it, so a destroy or a re-serve never interleaves with a create.
        self.lock = lock or asyncio.Lock()
        # The helper's one lock on volume names, across frames: a name looked up as absent is made
        # and journaled before any other frame's proxy can make it.
        self.volumes = volumes or asyncio.Lock()
        self.closed = False
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if len(os.fsencode(self.path)) > _SUN_PATH_MAX:
            raise ValueError("the Docker proxy's socket path is too long for macOS: move the Prismoid home")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise ValueError("the Docker proxy's socket path is taken by something that is not a socket")
            self.path.unlink()  # a socket a dead helper left
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=HEAD_LIMIT)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        self.closed = True
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):  # a stream still open (logs -f, attach) ends here
                writer.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1)
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._writers) >= _MAX_CONNECTIONS:
            with contextlib.suppress(ConnectionError):
                writer.write(error_bytes(503, "too many Docker connections open for this task: close some"))
                await writer.drain()
            writer.close()
            return
        self._writers.add(writer)
        try:
            await self._one(reader, writer)
        except BadRequest as e:
            with contextlib.suppress(ConnectionError):
                writer.write(error_bytes(e.status, str(e)))
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass  # either side went away
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = parse_head(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S))
        method, target = request_line(head)
        length = body_length(head)
        vetted_body = reads_body(method, target)
        body = await asyncio.wait_for(read_body(reader, length), _HEAD_TIMEOUT_S) if vetted_body else b""
        path = target.partition("?")[0]
        verdict = vet_request(
            method,
            target,
            body,
            ref=self.ref,
            task_id=self.task_id,
            roots=self.roots,
            read_only=self.read_only,
            protected=self.protected,
        )
        if not verdict.allow:
            LOG.info("docker proxy %s refused %s %s: %s", self.ref, method, path, verdict.reason)
            await send_answer(writer, 403, verdict.reason)
            return
        LOG.debug("docker proxy %s: %s %s", self.ref, method, path)
        makes = verdict.creates is not None or any(kind == "volume-new" for kind, _ in verdict.owned)
        async with self.lock if makes else contextlib.nullcontext():
            if makes and self.closed:
                await send_answer(writer, 503, "this task's Docker socket has just closed: try again")
                return
            if verdict.creates and (refusal := await self._room()):
                await send_answer(writer, 403, refusal)
                return
            async with self.volumes if verdict.creates == "volume" else contextlib.nullcontext():
                vetted = _Vetted(head, method, target, length, verdict)
                await self._prove_and_forward(reader, writer, vetted)

    async def _prove_and_forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, vetted: _Vetted
    ) -> None:
        """What the request makes and what it names, proved the frame's; only then does it go on."""
        verdict = vetted.verdict
        new = True  # what the create makes is new (a volume's name may already exist)
        if verdict.creates == "volume":
            refusal, new = await self._volume_create(verdict.body or b"{}")
            if refusal is not None:
                await send_answer(writer, *refusal)
                return
        resolved: tuple[str, str] | None = None  # the object the path names, by its id
        for kind, name in verdict.owned:
            refusal, ident = await self._check(kind, name)
            if refusal is not None:
                await send_answer(writer, *refusal)
                return
            resolved = resolved or (kind, ident)
        await self._forward(reader, writer, vetted, new=new, resolved=resolved)

    async def _forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        vetted: _Vetted,
        *,
        new: bool,
        resolved: tuple[str, str] | None,
    ) -> None:
        verdict, head, method, target = vetted.verdict, vetted.head, vetted.method, vetted.target
        upgrade = None
        if verdict.raw and "upgrade" in head.tokens("connection") and (asked := head.values("upgrade")):
            if not TOKEN.match(asked[0]):
                raise BadRequest("a malformed Upgrade header")
            upgrade = asked[0]
        up_reader, up_writer = await asyncio.open_unix_connection(self.upstream, limit=HEAD_LIMIT)
        try:
            sent_target = verdict.target or target
            if reads_body(method, target):  # a body read whole goes as the policy encoded it, never as sent
                sent = verdict.body or b"{}"
                up_writer.write(
                    forward_head(method, sent_target, head, length=len(sent), upgrade=upgrade) + sent
                )
            else:
                up_writer.write(
                    forward_head(method, sent_target, head, length=vetted.length, upgrade=upgrade)
                )
                await copy_body(reader, up_writer, vetted.length)
            await up_writer.drain()
            if verdict.raw:
                await relay_hijacked(reader, writer, up_reader, up_writer)
            elif verdict.creates:
                await self._created(writer, up_reader, verdict.creates, new=new)
            elif method == "DELETE" and resolved is not None:
                await self._removed(writer, up_reader, resolved)
            else:
                await relay_response(reader, writer, up_reader)
        finally:
            up_writer.close()

    async def _room(self) -> str | None:
        """None while the frame's journal has room for one more object; past `CAP`, the ids of
        what is gone are pruned first."""
        if self.journal.count(self.ref) < CAP:
            return None
        with contextlib.suppress(*UPSTREAM_ERRORS, Unfinished):
            before = self.journal.entry(self.ref)
            self.journal.keep(self.ref, await existing_of(self.upstream, self.ref), before)
        if self.journal.count(self.ref) < CAP:
            return None
        return f"this task already has {CAP} Docker objects: remove some (docker rm, docker network rm) first"

    async def _volume_create(self, body: bytes) -> tuple[tuple[int, str] | None, bool]:
        """A named volume create (under the frame's lock and the volume-name lock): Docker answers
        201 for a name that exists, so an existing one is never the create's. One that wears this
        frame's label (only a create through this proxy puts it there) is the frame's again; any
        other is refused. Returns (the refusal, or None; whether the create makes it new). `body` is
        the policy's encoding: its keys are Docker's spelling."""
        try:
            name = json.loads(body).get("Name") or ""
        except (ValueError, AttributeError):
            name = ""
        if not name:
            return None, True  # Docker names it: new
        status, doc = await call(self.upstream, "GET", lookup_path("volume", str(name)))
        if status == 404:
            return None, True
        if status != 200 or not isinstance(doc, dict):
            return daemon_refusal(status, doc, f"volume {name}"), False
        if labels_of("volume", doc).get(FRAME_LABEL) != self.ref:  # pyright: ignore[reportUnknownArgumentType]
            return (
                403,
                f"the volume {name} already exists and is not this task's: choose another name",
            ), False
        return None, False

    async def _check(self, kind: str, name: str) -> tuple[tuple[int, str] | None, str]:
        """(None, its id) when `name` is this frame's own, else (the answer to give, ""): the
        daemon's own 404, or a 403 saying it is not this task's. A container or a network is the
        frame's when it is journaled and wears its label; a volume also when it wears the label
        (only a create through this proxy puts it there: it is journaled again) or is an anonymous
        one a journaled container of the frame mounts. A `volume-new` that is absent is made, and
        journaled - or removed again, and refused, when it cannot be. A `container-start` is vetted
        again as the daemon holds it (`recheck`), and what it reaches must be the frame's too."""
        if kind == "exec":
            status, doc = await call(self.upstream, "GET", f"/exec/{quote(name, safe='')}/json")
            if status != 200 or not isinstance(doc, dict):
                return daemon_refusal(status, doc, f"exec {name}"), ""
            kind, name = "container", str(doc.get("ContainerID", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        noun = "volume" if kind.startswith("volume") else kind.removesuffix("-start")
        if kind == "volume-new":
            async with self.volumes:  # no other frame's proxy makes this name meanwhile
                status, doc = await call(self.upstream, "GET", lookup_path(noun, name))
                if status == 404:
                    return await self._make_volume(name), name
        else:
            status, doc = await call(self.upstream, "GET", lookup_path(noun, name))
        if status != 200 or not isinstance(doc, dict):
            return daemon_refusal(status, doc, f"{noun} {name}"), ""
        labels = labels_of(noun, doc)  # pyright: ignore[reportUnknownArgumentType]
        ident = str(doc.get("Name" if noun == "volume" else "Id", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        mine = labels.get(FRAME_LABEL) == self.ref
        if noun == "volume" and mine:
            owned = self.journal.holds(self.ref, "volumes", ident) or (
                await self._journaled("volume", ident, new=False) is None
            )
        elif noun == "volume":
            owned = _ANONYMOUS in labels and await self._mounted_by_mine(ident)
        else:
            owned = mine and self.journal.holds(self.ref, f"{noun}s", ident)  # a label alone is forgeable
        if not owned:
            return (
                403,
                f"{noun} {name} is not this task's: Prismoid's Docker proxy acts only on what this task created",
            ), ""
        if kind == "container-start":
            why, reached = recheck(  # pyright: ignore[reportUnknownArgumentType]
                doc,  # pyright: ignore[reportUnknownArgumentType]
                name,
                roots=self.roots,
                read_only=self.read_only,
                protected=self.protected,
            )
            if why is not None:
                return (403, why), ""
            for other, other_name in reached:
                if (refusal := (await self._check(other, other_name))[0]) is not None:
                    return refusal, ""
        return None, ident

    async def _make_volume(self, name: str) -> tuple[int, str] | None:
        """A volume a container create names that is absent, made labelled and journaled (under the
        volume-name lock): None, or the answer to give."""
        labels = {TASK_LABEL: self.task_id, FRAME_LABEL: self.ref}
        status, doc = await call(self.upstream, "POST", "/volumes/create", {"Name": name, "Labels": labels})
        if status != 201:
            return daemon_refusal(status, doc, f"volume {name}")
        if (refusal := await self._made_right("volume", name, new=True)) is not None:
            return refusal
        return await self._journaled("volume", name, new=True)

    async def _mounted_by_mine(self, volume: str) -> bool:
        """An anonymous volume is the frame's when one of its journaled containers mounts it (compose
        hands a recreated container its predecessor's)."""
        wanted = json.dumps({"label": [f"{FRAME_LABEL}={self.ref}"], "volume": [volume]})
        status, listed = await call(
            self.upstream, "GET", f"/containers/json?{urlencode({'all': '1', 'filters': wanted})}"
        )
        mine = set(self.journal.entry(self.ref).get("containers", []))
        return (
            status == 200
            and isinstance(listed, list)
            and any(
                isinstance(c, dict) and c.get("Id") in mine
                for c in listed  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            )
        )

    async def _made_right(self, kind: str, ident: str, *, new: bool) -> tuple[int, str] | None:
        """None when what a create just made is what the policy sent: it wears this frame's and this
        task's labels, and a container passes the policy again as the daemon holds it (`recheck`).
        Otherwise the answer to give, and what this call made (`new`) is removed again. A volume
        without the labels was never this call's (Docker answers 201 for a name that exists)."""
        try:
            status, doc = await call(self.upstream, "GET", lookup_path(kind, ident))
        except UPSTREAM_ERRORS:
            status, doc = 0, None
        if status != 200 or not isinstance(doc, dict):
            code, why = 502, f"Prismoid's Docker proxy could not look at the new {kind}"
        else:
            labels = labels_of(kind, doc)  # pyright: ignore[reportUnknownArgumentType]
            if (labels.get(FRAME_LABEL), labels.get(TASK_LABEL)) == (self.ref, self.task_id):
                if kind != "container":
                    return None
                why, _ = recheck(  # pyright: ignore[reportUnknownArgumentType]
                    doc,  # pyright: ignore[reportUnknownArgumentType]
                    ident[:12],
                    roots=self.roots,
                    read_only=self.read_only,
                    protected=self.protected,
                )
                if why is None:
                    return None
            elif kind == "volume":
                return 403, f"the volume {ident} already exists and is not this task's: choose another name"
            else:
                why = f"the new {kind} does not wear this task's labels"
            code = 403
        if new:
            with contextlib.suppress(*UPSTREAM_ERRORS):
                await call(self.upstream, "DELETE", removal_path(kind, ident))
        return code, f"{why}{', so it was removed' if new else ''}"

    async def _journaled(self, kind: str, ident: str, *, new: bool) -> tuple[int, str] | None:
        """Journal `ident`: None once it is, else the answer to give. Every id the journal takes
        counts toward `CAP`. What cannot be journaled, when this very call made it (`new`), is removed
        again, since the helper could never clean up after it - one that existed before is left."""
        refusal: tuple[int, str] | None = None
        if not self.journal.holds(self.ref, f"{kind}s", ident) and (full := await self._room()):
            refusal = 403, full
        else:
            try:
                self.journal.record(self.ref, f"{kind}s", ident)
                return None
            except OSError as e:
                LOG.error("docker proxy %s could not journal %s %s: %s", self.ref, kind, ident[:12], e)
                refusal = 500, f"Prismoid could not record the {kind}"
        if new:
            with contextlib.suppress(*UPSTREAM_ERRORS):
                await call(self.upstream, "DELETE", removal_path(kind, ident))
        code, why = refusal
        return code, f"{why}, {'so it was removed' if new else 'it was left as it was'}: try again"

    async def _created(
        self, writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader, kind: str, *, new: bool
    ) -> None:
        """A create's answer: the object is looked at (`_made_right`) and its id journaled before the
        client hears of it. One that fails either is refused (and removed again only when this
        call made it)."""
        data, status, body = await read_whole(up_reader)
        if status == 201 and (ident := created_id(body)) is not None:
            refusal = await self._made_right(kind, ident, new=new)
            if refusal is None:
                refusal = await self._journaled(kind, ident, new=new)
            if refusal is not None:
                await send_answer(writer, *refusal)
                return
        await relay_answer(writer, data, up_reader)

    async def _removed(
        self, writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader, resolved: tuple[str, str]
    ) -> None:
        """A removal's answer: once Docker has removed it, the object leaves the journal."""
        data, status, _ = await read_whole(up_reader)
        kind, ident = resolved
        if 200 <= status < 300 and ident:
            noun = "volume" if kind.startswith("volume") else kind.removesuffix("-start")
            with contextlib.suppress(OSError):
                self.journal.forget(self.ref, f"{noun}s", ident)
        await relay_answer(writer, data, up_reader)


async def serve(
    ref: str,
    socket_path: Path,
    upstream: str,
    task_id: str,
    roots: Sequence[str],
    *,
    read_only: Sequence[str] = (),
) -> None:
    """Serve one frame's filtering socket at `socket_path` (0600) until cancelled, forwarding to the
    user's Docker socket `upstream` (`find_upstream`), its journal beside it."""
    journal = Journal(socket_path.parent)
    proxy = _Proxy(ref, socket_path, upstream, task_id, roots, journal=journal, read_only=read_only)
    await proxy.start()
    try:
        await asyncio.get_running_loop().create_future()
    finally:
        await proxy.close()


def find_upstream() -> str | None:
    """The user's Docker socket: the endpoint `docker context inspect` names (so `DOCKER_HOST` and
    the current context are honoured), when it is a Unix socket. None with no Docker here. Asked
    afresh each time: `DockerProxies` keeps what it found."""
    cli = shutil.which("docker") or next(
        (p for p in map(os.path.expanduser, _DOCKER_CLIS) if os.access(p, os.X_OK)), None
    )
    if cli is None:
        return None
    try:
        out = subprocess.run(
            [cli, "context", "inspect"], capture_output=True, text=True, timeout=10, check=True
        )
        host = str(json.loads(out.stdout)[0]["Endpoints"]["docker"]["Host"])
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError):
        return None
    if not host.startswith("unix://"):
        LOG.warning("Docker's endpoint is not a Unix socket: the Docker grant cannot reach it")
        return None
    return host.removeprefix("unix://")


class DockerProxies:
    """The helper's filtering sockets: one per frame with the Docker grant, serving the frame's
    grants as they are now (`ensure` on every command and every regrant), and the journal of what
    each made (`docker_journal`). A frame's changes are serial under its lock (`lock`), which lives
    as long as the helper and which the helper also holds across a frame's destroy and its
    making: a frame destroyed under it is marked so, and never served again until made anew
    (`revive`). Pending cleanups are tried again at the start, on demand and every
    `_RETRY_EVERY_S`."""

    def __init__(self, home: Path, upstream: Callable[[], str | None] | None = None) -> None:
        self.home = home
        self.journal = Journal(home / "run")
        self._find = upstream or find_upstream
        self._found: str | None = None  # the user's Docker socket, once found
        self._proxies: dict[str, _Proxy] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.volume_names = asyncio.Lock()  # across every frame (`_Proxy.volumes`)
        self._destroyed: set[str] = set()
        self._cleaning: set[Path] = set()
        self.retrying: asyncio.Task[None] | None = None  # the pending cleanups being tried again
        self._timer: asyncio.Task[None] | None = None

    async def upstream(self) -> str | None:
        if self._found is None:
            self._found = await asyncio.to_thread(self._find)
        return self._found

    def lock(self, ref: str) -> asyncio.Lock:
        return self._locks[ref]

    def revive(self, ref: str) -> None:
        """The frame is made (again), under its lock: it may be served."""
        self._destroyed.discard(ref)

    async def ensure(self, frame: DeviceFrameState) -> None:
        """Serve `frame`'s socket for its grants now, or none when it has no Docker grant or has
        been destroyed. Without Docker here the socket is not served, and Docker commands fail to
        connect."""
        try:
            async with self._locks[frame.ref]:
                await self._ensure(frame)
        finally:
            self.kick()

    async def _ensure(self, frame: DeviceFrameState) -> None:
        if not frame.spec.docker or frame.ref in self._destroyed:
            await self._close(frame.ref)
            return
        roots, read_only = roots_of(frame)
        running = self._proxies.get(frame.ref)
        protected = (*frame.protected_git, *frame.plants.later())
        if running and (running.task_id, running.roots, running.read_only, running.protected) == (
            frame.spec.task_id,
            roots,
            read_only,
            protected,
        ):
            return
        await self._close(frame.ref)
        if (upstream := await self.upstream()) is None:
            LOG.warning("frame %s has the Docker grant but no Docker socket was found", frame.ref)
            return
        path = socket_path(self.home, frame.ref)
        lock = self._locks[frame.ref]
        proxy = _Proxy(
            frame.ref,
            path,
            upstream,
            frame.spec.task_id,
            roots,
            journal=self.journal,
            read_only=read_only,
            lock=lock,
            volumes=self.volume_names,
            protected=protected,
        )
        try:
            await proxy.start()
        except (OSError, ValueError) as e:
            LOG.warning("frame %s's Docker socket could not be served: %s", frame.ref, e)
            return
        self._proxies[frame.ref] = proxy

    async def _close(self, ref: str) -> None:
        if (proxy := self._proxies.pop(ref, None)) is not None:
            await proxy.close()

    async def close(self, ref: str) -> None:
        async with self._locks[ref]:
            await self._close(ref)

    async def stop(self, ref: str) -> None:
        """The Machine is going to sleep: the frame's journaled containers stop. One that made
        none never wakes Docker."""
        if self.journal.entry(ref).get("containers") and (upstream := await self.upstream()):
            await stop_labelled(ref, upstream, self.journal, self._locks[ref])
        self.kick()

    async def stop_all(self, refs: Collection[str]) -> None:
        """The helper is stopping, or a new one is starting after one that may have stopped nothing:
        a container has no fence and writes its grants, so none outlives its helper. All at once and
        inside `STOP_DOCKER_S`, whatever Docker does. A frame that made none never wakes Docker."""
        mine = [ref for ref in refs if self.journal.entry(ref).get("containers")]
        if not mine:
            return
        try:
            async with asyncio.timeout(STOP_DOCKER_S):
                if (upstream := await self.upstream()) is not None:
                    await asyncio.gather(
                        *(
                            stop_labelled(ref, upstream, self.journal, self._locks[ref], _QUICK_GRACE_S)
                            for ref in mine
                        )
                    )
        except TimeoutError:
            LOG.warning("Docker did not stop %d frames' containers in time", len(mine))

    async def published(self, ref: str) -> set[tuple[str, int]]:
        """What the frame's journaled running containers publish on the device, each (address,
        port): a compose dev server is reached through Docker. The policy keeps a published port on
        loopback; one the daemon lists on no address is on both. One that made no container never
        wakes Docker; Docker away is nothing."""
        mine = set(self.journal.entry(ref).get("containers", []))
        if not mine or (upstream := await self.upstream()) is None:
            return set()
        label = json.dumps({"label": [f"{FRAME_LABEL}={ref}"]})
        try:
            status, listed = await call(upstream, "GET", f"/containers/json?{urlencode({'filters': label})}")
        except UPSTREAM_ERRORS as e:
            LOG.debug("no Docker daemon answers for %s's ports: %s", ref, e)
            return set()
        published: set[tuple[str, int]] = set()
        for item in listed if status == 200 and isinstance(listed, list) else []:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, dict) or item.get("Id") not in mine:  # pyright: ignore[reportUnknownMemberType]
                continue
            if (item.get("Labels") or {}).get(FRAME_LABEL) != ref:  # pyright: ignore[reportUnknownMemberType]
                continue
            for bound in item.get("Ports") or []:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if not isinstance(bound, dict) or bound.get("Type") != "tcp":  # pyright: ignore[reportUnknownMemberType]
                    continue
                public, address = bound.get("PublicPort"), bound.get("IP")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(public, int):
                    named = address if isinstance(address, str) and address else None
                    published |= {(a, public) for a in ([named] if named else ["127.0.0.1", "::1"])}
        return published

    async def end(self, ref: str) -> Path | None:
        """The frame has ended (its lock held by the caller): its socket goes, it is never served
        again until made anew, and its journal is retired - the cleanup `clean` then does."""
        self._destroyed.add(ref)
        await self._close(ref)
        return self.journal.retire(ref)

    async def clean(self, ref: str, pending: Path | None) -> None:
        """Remove what a retired journal names - now, or, with Docker away, at a later try. A frame
        that made nothing never wakes Docker."""
        if pending is not None and (upstream := await self.upstream()) is not None:
            await self._clean(ref, pending, upstream)
        self.kick()

    async def destroy(self, ref: str) -> None:
        async with self._locks[ref]:
            pending = await self.end(ref)
        await self.clean(ref, pending)

    async def _clean(self, ref: str, pending: Path, upstream: str) -> None:
        if pending in self._cleaning:
            return
        self._cleaning.add(pending)
        try:
            if await remove_labelled(ref, upstream, self.journal.read(pending)):
                self.journal.done(pending)
        finally:
            self._cleaning.discard(pending)

    async def retry_pending(self, live: Collection[str] | None = None) -> None:
        """Try every pending cleanup again. At the helper's start, `live` names the frames it has:
        a journal of any other frame (gone while the helper was down) is retired and cleaned too.
        Docker is looked for, and asked, once per try - not once per pending cleanup."""
        try:
            if live is not None:
                self._retire_all_but(live)
            if not (pending := self.journal.pending()) or (upstream := await self.upstream()) is None:
                return
            try:
                await call(upstream, "GET", "/_ping")
            except UPSTREAM_ERRORS:
                LOG.debug("no Docker daemon answers: %d cleanups wait for it", len(pending))
                return
            for ref, path in pending:
                await self._clean(ref, path, upstream)
        except Exception:
            LOG.exception("the pending Docker cleanups could not be tried")

    def _retire_all_but(self, live: Collection[str]) -> None:
        for ref in self.journal.refs():
            if ref not in live:
                self.journal.retire(ref)

    def start(self, live: Collection[str]) -> None:
        """The helper's start: the cleanups an earlier life left are tried, in the background, and
        then again every `_RETRY_EVERY_S` while any is pending."""
        self.retrying = asyncio.create_task(self.retry_pending(set(live)))
        self._timer = asyncio.create_task(self._every())

    async def _every(self) -> None:
        while True:
            await asyncio.sleep(_RETRY_EVERY_S)
            self.kick()  # never raises: the timer lives as long as the helper

    def kick(self) -> None:
        """Docker was just needed: a pending cleanup is tried again, in the background. A failure
        to look is logged, never raised into the command (or the timer) that kicked."""
        try:
            if (self.retrying is None or self.retrying.done()) and self.journal.pending():
                self.retrying = asyncio.create_task(self.retry_pending())
        except Exception:
            LOG.exception("could not look for pending Docker cleanups")

    async def close_all(self) -> None:
        for task in (self.retrying, self._timer):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for ref in list(self._proxies):
            await self.close(ref)
