# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker frame driver.

**No default network route.** The container is created on an *internal* network with no gateway,
so a process inside it cannot reach the internet even if it wants to - the only way out is the
egress proxy, and the proxy only knows the domains this task was allowed. This is the
difference between a sandbox and a promise: an agent that is *asked* not to call an API and one
that *cannot* are not the same system, and only one of them is safe to run someone else's prompt in.

The docker SDK is synchronous. Every call is pushed to a thread rather than run on the event loop,
because a blocking `container.exec_run` on the loop stalls every other task in the worker - which
under the frontier fan-out is up to `PRISMOID_WORKER_STEP_CONCURRENCY` of them at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import tarfile
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import IO, Any, ClassVar, Literal, cast

import docker
import httpx
import logfire
import orjson
from docker.errors import DockerException, NotFound

from prismoid.ai import config
from prismoid.ai.frames import images, layout
from prismoid.ai.frames.archives import ChunkReader, checkpoint_filter, empty_tar, restore_filter, rewrite
from prismoid.ai.frames.driver import ArchiveTooLarge, ExecResult, Owner
from prismoid.ai.frames.egress import control_headers
from prismoid.ai.frames.image_browser.sidecar.ctl import START_S
from prismoid.ai.frames.layout import (
    AGENT_UID,
    ALL,
    BROWSER_UID,
    INPUTS,
    INSTRUCTIONS,
    MANIFEST,
    SKILLS_RESET,
    STANDARD_PATHS,
    TASK,
)
from prismoid.ai.frames.product import artifact_target
from prismoid.ai.frames.publish import (
    ARGS_PATH,
    PUBLISH_RUNNER,
    RUNNER_PATH,
    publish_args,
    read_manifest,
    validate_publish,
)
from prismoid.errors import PolicyDenied
from prismoid.schemas.machines import (
    LABEL_INSTANCE,
    LABEL_KIND,
    LEGACY_PREFIXES,
    FrameInfo,
    parse_resources,
    parse_rfc3339,
)

NETWORK = "prismoid_frame_net"  # internal: no gateway, no route out

MEMORY_LIMIT = "2g"
CPU_QUOTA = 200_000  # 2 cores (100_000 = 1 core at the default 100ms period)
EXEC_TIMEOUT_S = 120.0  # `exec`'s default bound, and a system read's
KILL_GRACE_S = 5.0  # a command over its bound gets TERM, then KILL this much later


@functools.lru_cache(maxsize=1)
def _client() -> docker.DockerClient:
    """One daemon client per process. `docker.from_env()` negotiates the API
    version with the daemon on construction - a fresh client per step put that round-trip
    (synchronously, on whatever thread called it) on every frame create. The client is a
    requests session under the hood: thread-safe, and it re-establishes pooled connections
    after a daemon restart."""
    return docker.from_env()


def docker_client() -> docker.DockerClient:
    """The process's one daemon client, for a caller that speaks the Docker API beyond this
    driver's verbs: the screen's hijacked exec (`ai/frames/screen.py`)."""
    return _client()


class DockerFrame:
    """One container, one task. Satisfies `FrameDriver` and `deps.FrameLike`."""

    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "archive"
    driver: ClassVar[str] = "docker"
    paths = STANDARD_PATHS

    def __init__(
        self,
        task_id: str,
        profile: str = "base",
        attempt: int = 1,
        *,
        name: str | None = None,
        recording: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.profile = profile
        self.attempt = attempt
        self.name = name or f"frame-{task_id}"
        self._recording = recording or str(attempt)
        self.ip: str = ""
        self._container: Any = None
        self.egress_version = 0
        self.egress_hosts: list[str] = []

    @property
    def ref(self) -> str:
        return self.name

    @property
    def recording_dir(self) -> str:
        return f"{layout.ARTIFACTS}/recording-{self._recording}"

    # ── lifecycle ─────────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        """Boot the container on the internal frame network. It is born with an egress principal
        even with no domains, so a later `allow` can widen it; it never raises `EgressUnavailable`."""
        start = time.perf_counter()
        domains = allow_domains or []
        stamped = dict(labels or {})
        instance = config.current().worker.instance
        if stamped or instance:
            # The dev instance rides on every labelled frame, so `inventory()` shows each worker
            # only its own instance's frames and two worktrees never reap each other's Machines.
            stamped[LABEL_INSTANCE] = instance
        await asyncio.to_thread(self._create_sync, domains, stamped)

        # Register the allowlist with the proxy, keyed by this frame's IP on the frame network.
        # Pushed, not pulled: a proxy that had to ask the database on every request would put a
        # query on the path of every HTTP call an agent makes, and would need database credentials
        # inside the one process whose whole job is to be exposed.
        await asyncio.to_thread(self._register_egress, domains, egress_version)
        logfire.info(
            "frame created {name} in {ms:.0f}ms, egress {domains}",
            name=self.name,
            ms=(time.perf_counter() - start) * 1000,
            domains=domains or "(none - no route out)",
        )

    def _register_egress(self, domains: list[str], version: int) -> None:
        assert self._container is not None
        self._container.reload()
        self.ip = self._container.attrs["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
        try:
            # A frame's birth is a new principal on a possibly reused IP: a frame removed without
            # its own `destroy()` (the reconciler's destroy by reference, a worker killed mid-step,
            # an operator's `docker rm`) left its list and version on the proxy, and a versioned
            # push would 409 into them.
            self._forget_egress()
            if self._post_allow(domains, version) is None:
                self.egress_version, self.egress_hosts = version, sorted(domains)
        except (httpx.HTTPError, RuntimeError) as exc:
            # Fail CLOSED, loudly: a frame whose list never reached the proxy has none there, which
            # the proxy treats as deny-everything.
            logfire.error("could not register egress allowlist: {err}", err=str(exc))

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        """REPLACE the running frame's allowlist, versioned - the same control-plane
        call `create()` makes at boot, re-issued live so a mid-run consent widens the frame that
        asked without a restart.

        Unpinned, the push is one past the driver's synced version - or, when it has synced none
        (`egress_version == 0`, a driver found by `bind()`), an UNVERSIONED push that the proxy
        applies without touching its version, so the row's version stays authoritative. Every
        versioned push that is not newer (locally, or a 409 from the proxy) returns False, logged,
        and is never retried here: a slow push must not overwrite a newer list. A 409 carries the
        version the proxy holds, and the driver adopts it, so a caller that owns the list (the
        Machine's holder) can push once past it - a pinned push that lost stays lost. Any other
        non-2xx from the proxy raises RuntimeError."""
        if version is None and self.egress_version == 0:
            if await asyncio.to_thread(self._post_allow, list(domains), None) is not None:
                return False
            self.egress_hosts = sorted(domains)
            return True
        version = self.egress_version + 1 if version is None else version
        if version <= self.egress_version:
            logfire.warn(
                "stale egress push dropped for {name}: v{v} <= v{cur}",
                name=self.name,
                v=version,
                cur=self.egress_version,
            )
            return False
        held = await asyncio.to_thread(self._post_allow, list(domains), version)
        if held is not None:
            self.egress_version = max(self.egress_version, held)
            return False
        self.egress_version, self.egress_hosts = version, sorted(domains)
        return True

    def _post_allow(self, domains: list[str], version: int | None) -> int | None:
        """Push one list (`version=None`: unversioned). None when it applied; on a 409 (a stale
        version) the version the proxy holds, as its refusal says (0 if it does not)."""
        if not self.ip:
            raise RuntimeError("frame has no network address yet; create() first")
        response = httpx.post(
            f"{config.current().worker.egress_control_url}/allow",
            json={"ip": self.ip, "domains": domains, "version": version},
            headers=control_headers(),
            timeout=5.0,
        )
        if response.status_code == 409:
            held = _held_version(response)
            logfire.warn(
                "egress proxy refused a stale v{v} for {name} (holds v{held})",
                v=version,
                name=self.name,
                held=held,
            )
            return held
        if response.status_code >= 300:
            raise RuntimeError(f"egress control refused: {response.status_code}")
        return None

    def _forget_egress(self) -> None:
        httpx.delete(
            f"{config.current().worker.egress_control_url}/allow/{self.ip}",
            headers=control_headers(),
            timeout=5.0,
        )

    def _create_sync(self, allow_domains: list[str], labels: dict[str, str]) -> None:
        client = _client()
        self._ensure_network(client)
        image = ensure_image(self.profile)

        # **Reap the orphan first.** A worker killed mid-step leaves its container behind, and the
        # container name is derived from (task, step) - so the recovered attempt collides with the
        # corpse of the one that died, and Docker refuses with a name conflict. Recovery must be
        # able to assume nothing about the world it woke up in.
        try:
            client.containers.get(self.name).remove(force=True)
            logfire.warn("reaped an orphaned frame {name}", name=self.name)
        except NotFound:
            pass

        # The proxy is the ONLY route out. `no_proxy` is deliberately empty: a frame that can
        # bypass the proxy for "just localhost" can bypass it for anything that resolves there.
        proxy = config.current().worker.egress_proxy_url
        env = {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "PRISMOID_TASK_ID": self.task_id,
            "PRISMOID_ALLOW_DOMAINS": ",".join(allow_domains),
        }

        # Per-profile limits (`PRISMOID_MACHINES_RESOURCES`); a profile the dial does not name
        # keeps the historical 2 cores / 2 GiB / 512 pids.
        res = parse_resources(config.current().machines.resources).get(self.profile)
        mem_limit = f"{res.mem_mb}m" if res else MEMORY_LIMIT
        cpu_quota = int(res.cpus * 100_000) if res else CPU_QUOTA
        pids = res.pids if res else 512

        self._container = client.containers.run(
            image,
            name=self.name,
            detach=True,
            network=NETWORK,
            environment=env,
            labels=labels,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            pids_limit=pids,  # a fork bomb inside the frame is the frame's problem, not the host's
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],  # an agent needs no capabilities to write a memo
            auto_remove=False,
            command=["sleep", "infinity"],
        )

    @staticmethod
    def _ensure_network(client: docker.DockerClient) -> None:
        """The frame network is **compose's** (`deploy/compose.yaml`), not ours.

        One place per kind: the network is infrastructure, it is declared where the rest of the
        infrastructure is declared, and the egress proxy is attached to it there. A driver that
        also creates it gives you two owners for one resource - and compose will refuse to adopt a
        network it did not make, which is exactly how this was found.

        We only check that it exists, and say so loudly if it does not.
        """
        try:
            client.networks.get(NETWORK)
        except NotFound:
            raise RuntimeError(
                f"the frame network {NETWORK!r} does not exist - run `just up` "
                "(it is declared in deploy/compose.yaml, along with the egress proxy that guards it)"
            ) from None

    async def bind(self, *, start: bool = True) -> bool:
        """Attach to the frame named `ref` - a Machine's frame between steps, or a parked step's
        (ADR-063).

        When `ask_user` pauses a step, its frame is left alive - a stopped agent in an idle
        container costs ~zero CPU - so that the resume finishes the *same* run against the *same*
        files, in well under the half-second budget, instead of rebuilding a sandbox and losing
        everything the agent wrote before it asked. The container name is deterministic
        (`frame-{task}-{step}-{attempt}`), so re-finding it is a lookup, not a guess.

        A container that is there but stopped (a Docker or host restart, an operator's `docker
        stop`) is STARTED, and the call answers True: it has no volume, so `/work` and `/browser`
        live in its writable layer, which a stop keeps. Only its processes are lost. Its address may
        be new, and whatever the proxy holds for that address is forgotten - it reaches nothing
        until its caller pushes the list. It raises `RuntimeError` if the container is gone - which,
        after a park, means something reaped it, and resuming into a fresh empty frame would
        silently drop the agent's work - or dead, or exits as it starts: a failure to surface, not
        to paper over. Any other Engine error is raised as itself, never read as gone.
        """
        started = await asyncio.to_thread(self._bind_sync, start)
        if started and self.ip:
            try:
                await asyncio.to_thread(self._forget_egress)
            except httpx.HTTPError as exc:  # the caller's push replaces the list either way
                logfire.warn("could not forget a restarted frame's egress: {err}", err=str(exc))
        logfire.info("frame bound {name}{how}", name=self.name, how=" (started again)" if started else "")
        return started

    def _bind_sync(self, start: bool) -> bool:
        """Find the container, starting it if it stopped; True when it had to be started."""
        try:
            self._container = _client().containers.get(self.name)
            status = self._container.status
            if status != "running":
                if not start or status in ("dead", "removing"):
                    raise RuntimeError(f"frame {self.name} is {status}, not running")
                (self._container.unpause if status == "paused" else self._container.start)()
                self._container.reload()
                if self._container.status != "running":
                    raise RuntimeError(f"frame {self.name} is {self._container.status} after a start")
        except NotFound as gone:
            raise RuntimeError(f"frame {self.name} vanished during the park - cannot resume") from gone
        networks = self._container.attrs["NetworkSettings"]["Networks"]
        self.ip = networks.get(NETWORK, {}).get("IPAddress", "")
        return status != "running"

    async def reset_mounts(self) -> None:
        await asyncio.to_thread(self._reset_sync)

    def _reset_sync(self) -> None:
        """Two owners, two execs. `/task` is root's and 0555, and the frame's root holds no
        capability (`cap_drop=["ALL"]`), so it unlocks what it owns, empties it and locks it again -
        the agent can write there at no point. `/skills/<name>` is the agent's (`materialise_skills`
        makes it), which a root without `CAP_DAC_OVERRIDE` cannot empty."""
        assert self._container is not None, "frame not created"
        locked = f"{TASK} {INPUTS}"
        mounts = (
            f"chmod u+w {locked} && find {INPUTS} -mindepth 1 -delete && rm -f {' '.join(INSTRUCTIONS)}; "
            f"done=$?; chmod 0555 {locked}; exit $done"
        )
        for command, uid in ((mounts, 0), (SKILLS_RESET, AGENT_UID)):
            out = self._exec_as(uid, ["/bin/sh", "-c", command])
            if out.exit_code != 0:
                said = (out.output or b"").decode(errors="replace").strip()[:200]
                raise RuntimeError(f"the mounts of {self.name} could not be reset: {said}")

    async def freeze(self) -> None:
        """`kill -1` as the agent: all it may signal but the caller and PID 1, never the browser user's
        sidecar. Not via `exec`, whose bounding `timeout` is the agent's and would be stopped too."""
        await asyncio.to_thread(self._exec_as, AGENT_UID, ["/bin/sh", "-c", "kill -STOP -1"])

    async def thaw(self) -> None:
        await asyncio.to_thread(self._exec_as, AGENT_UID, ["/bin/sh", "-c", "kill -CONT -1"])

    async def destroy(self) -> None:
        await asyncio.to_thread(self._destroy_sync)

    async def close(self) -> None:
        """Nothing is held open past a call (`FrameDriver.close`)."""

    def _destroy_sync(self) -> None:
        if self.ip:
            with contextlib.suppress(httpx.HTTPError):
                self._forget_egress()
        # Forget the address too: Docker hands it to the next frame, and a late `allow()` on this
        # driver must raise "create() first", never write into whoever holds it now.
        self.ip, self.egress_version, self.egress_hosts = "", 0, []
        try:
            client = _client()
            container = self._container or client.containers.get(self.name)
            container.remove(force=True)
        except (NotFound, DockerException):
            pass
        self._container = None

    async def denials(self) -> list[dict[str, str]]:
        """What the proxy refused for this frame. The worker journals these as `tool.called
        ok=false` - an egress denial that is invisible gets debugged for an hour.

        Async: the sync `httpx.get` ran ON the event loop after every step - up
        to 5 s of stalled loop when the proxy hung, on the path between the answer and the
        user."""
        try:
            body = (await asyncio.to_thread(self._denials_sync)).json()
        except httpx.HTTPError:
            return []
        return [d for d in body.get("denials", []) if d.get("ip") == self.ip]

    def _denials_sync(self) -> httpx.Response:
        return httpx.get(
            f"{config.current().worker.egress_control_url}/denials", headers=control_headers(), timeout=5.0
        )

    # ── work ──────────────────────────────────────────────────────────────────────────────────

    async def exec(self, command: str, timeout: float = EXEC_TIMEOUT_S) -> ExecResult:  # noqa: ASYNC109
        """Run a command inside the frame under `timeout(1)`: past `timeout` its whole process group is
        killed in the frame, so nothing it started outlives the call. Journalled by the caller."""
        bounded = ["timeout", f"--kill-after={KILL_GRACE_S:g}", f"{timeout:g}", "/bin/bash", "--noprofile"]
        start, backstop = time.perf_counter(), timeout + 2 * KILL_GRACE_S
        out = await self._run([*bounded, "--norc", "-c", command], backstop)
        elapsed = time.perf_counter() - start
        if out.exit_code in (124, 137) and timeout <= elapsed < backstop:
            return ExecResult(out.exit_code, out.stdout, f"{out.stderr}timed out after {timeout:g}s; killed")
        return out

    async def _run(self, argv: list[str], bound_s: float = EXEC_TIMEOUT_S) -> ExecResult:
        """`argv` with no shell, bounded like `exec`: a FIFO the agent made never holds a thread."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._run_sync, argv), timeout=bound_s)
        except TimeoutError:
            return ExecResult(exit_code=124, stdout="", stderr=f"timed out after {bound_s:g}s")

    def _run_sync(self, argv: list[str]) -> ExecResult:
        """`argv` as the agent's user in `/work`, with no shell to parse any of its words."""
        assert self._container is not None, "frame not created"
        out = self._container.exec_run(argv, demux=True, workdir="/work")
        stdout_b, stderr_b = out.output
        return ExecResult(
            exit_code=out.exit_code,
            stdout=(stdout_b or b"").decode(errors="replace"),
            stderr=(stderr_b or b"").decode(errors="replace"),
        )

    def _exec_as(self, uid: int, argv: list[str]) -> Any:
        """A system command as `uid`: argv, or a plain `sh -c`, never a login shell (the `exec`
        contract in `driver.py`). Root runs with its own HOME, as sandbar's `execAsRoot` does: an
        exec inherits the image's HOME, `/work`, and nothing root runs may read a file from it."""
        assert self._container is not None, "frame not created"
        home = {"HOME": "/root"} if uid == 0 else None
        return self._container.exec_run(argv, user=str(uid), environment=home)

    async def write_file(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent") -> None:
        """The real one. `write_file` is the text convenience on top of it.

        **Attachments are not always text.** A user's source document can be a PDF, and a `str`
        round-trip through `.encode()`/`cat` mangles it into something that still *looks* like a
        file - the worst kind of corruption, because only the parser three steps later finds out.

        **`owner` is not cosmetic, and the default is the interesting one.** `put_archive` writes
        each file with the uid recorded in the tar member, and `TarInfo`'s default is 0 - root. So
        every file the system copied into the frame was root-owned, and the agent could not edit
        *its own scratch files* with a shell: write `/work/build.py`, run a formatter over it, get
        `Permission denied`. Nothing had noticed, because nothing had tried.

        `owner="system"` is the deliberate opposite: root, and (with `mode=0o444`) beyond the
        agent's reach. That is how a source stays a source - an agent that can rewrite what it was
        given can launder a hallucination into a verified fact.
        """
        await asyncio.to_thread(self._write_sync, path, data, mode, owner)

    def _write_sync(self, path: str, data: bytes, mode: int, owner: Owner) -> None:
        assert self._container is not None
        directory, _, filename = path.rpartition("/")
        uid = AGENT_UID if owner == "agent" else 0

        # **Create the parent first.** `put_archive` copies *into* a directory and does not make one -
        # so writing `/work/solution/test.sh` before `/work/solution` exists is a 404 two layers away,
        # which is exactly how a step's first file write failed. `mkdir -p` as the file's owner, so
        # the agent owns the directories it works in (and can `shell` into them), and the system owns
        # the read-only ones it is given. Idempotent, so a repeat write costs nothing.
        if directory and directory != "/":
            self._exec_as(uid, ["mkdir", "-p", directory])

        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            info.mode = mode
            info.mtime = time.time()  # a real mtime: the step marker's `find -newer` relies on it
            info.uid = info.gid = uid
            info.uname = info.gname = owner if owner == "agent" else "root"
            tar.addfile(info, io.BytesIO(data))
        stream.seek(0)
        self._container.put_archive(directory or "/", stream.read())

    async def read_file(self, path: str) -> str:
        """The file as text, read as the agent's user; a path is one argv word, never shell."""
        result = await self._run(["cat", "--", path])
        if not result.ok:
            raise FileNotFoundError(f"{path}: {result.stderr.strip()}")
        return result.stdout

    async def read_bytes(self, path: str) -> bytes:
        """Raw bytes out of the frame over `write_bytes`'s tar transport: the only correct read of a
        binary artifact, since `read_file` decodes text and would corrupt what harvest hashes."""
        return await asyncio.to_thread(self._read_bytes_sync, path)

    def _read_bytes_sync(self, path: str) -> bytes:
        assert self._container is not None
        try:
            stream, _ = self._container.get_archive(path)
        except NotFound:
            raise FileNotFoundError(path) from None
        buf = io.BytesIO(b"".join(stream))
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            member = next(iter(tar.getmembers()), None)
            if member is not None and member.isdir():
                raise FileNotFoundError(f"{path}: is a directory")
            if member is None or not member.isfile():
                raise FileNotFoundError(path)
            extracted = tar.extractfile(member)
            return extracted.read() if extracted is not None else b""

    async def list_dir(self, path: str) -> list[str]:
        """Entry names as `ls -1` gives them; a directory that is not there lists as nothing."""
        result = await self._run(["ls", "-1", "--", path])
        return [line for line in result.stdout.splitlines() if line]

    # ── the Machine's checkpoint ──────────────────────────────────────────────────────────────

    async def archive(self, path: str, out: IO[bytes], *, limit: int | None = None) -> None:
        await asyncio.to_thread(self._archive_to, path, out, limit)

    def _archive_to(self, path: str, out: IO[bytes], limit: int | None) -> None:
        assert self._container is not None, "frame not created"
        try:
            chunks, _ = self._container.get_archive(path)
        except NotFound:
            empty_tar(out)
            return
        rewrite(io.BufferedReader(ChunkReader(_counted(chunks, limit))), out, checkpoint_filter(path))

    async def unpack(self, path: str, src: IO[bytes]) -> None:
        await asyncio.to_thread(self._unpack_from, path, src)

    def _unpack_from(self, path: str, src: IO[bytes]) -> None:
        """Filter into a spooled temp file, then hand the daemon a stream. `put_archive` is a
        Docker Engine operation, not a namespaced exec, so it writes exactly what the headers
        say - which is why `restore_filter` stamps them first. **Before the write, never after
        with `exec_run`:** the container runs `cap_drop=["ALL"]`, so its root holds none of
        `CAP_CHOWN`/`CAP_FOWNER`/`CAP_DAC_OVERRIDE`, and a post-hoc `chown -R` there always
        fails."""
        assert self._container is not None, "frame not created"
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            rewrite(src, spool, restore_filter(path))
            spool.seek(0)
            self._container.put_archive("/", spool)

    # ── publish ───────────────────────────────────────────────────────────────────────────────

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        """**The only door**.

        Validates the extension against the artifact-type registry, hashes the bytes, copies them
        into `/artifacts`, and appends a manifest entry. Nothing outside `/artifacts` + the manifest
        is harvestable - so this is not a convenience method, it is the **entire boundary** between
        "a file the agent made" and "a deliverable the product will sign".

        The type check is at the door on purpose. An agent that publishes a `.txt` as a
        `spreadsheet` would send it to the spreadsheet verifier, which would fail to open it and
        report a corrupt workbook - a confusing failure two subsystems away from the mistake.
        Here, it is one clear sentence.

        **Injection-proof by construction** (see `ai/frames/publish.py`): the runner is a static
        program, the model-authored strings travel as a JSON args file over the tar transport,
        and the exec is a fixed argv with no shell - a quote in a title is just a character,
        never a refused publish.
        """
        validate_publish(type, path)
        args = publish_args(path, artifact_target(path), MANIFEST, type, title, list(sources))
        await self.write_bytes(RUNNER_PATH, PUBLISH_RUNNER.encode(), mode=0o444, owner="system")
        await self.write_bytes(ARGS_PATH, orjson.dumps(args), mode=0o444, owner="system")
        await asyncio.to_thread(self._publish_exec)

    def _publish_exec(self) -> None:
        assert self._container is not None
        out = self._container.exec_run(["python3", RUNNER_PATH, ARGS_PATH], demux=True)
        if out.exit_code != 0:
            stderr = (out.output[1] or b"").decode(errors="replace")
            raise PolicyDenied(f"publish failed: {stderr.strip()[:200]}")

    async def manifest(self) -> dict[str, Any]:
        """Read `/artifacts/manifest.json`. What harvest reads."""
        return await read_manifest(self)

    # ── the browser sidecar ──────────────────────────────────────────────────────────────

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        if self.profile != "browser":
            return {"ok": False, "error": "no browser in this frame"}
        timeout = config.current().computer_use.command_timeout_s if timeout is None else timeout
        try:
            # A call may cold-start the sidecar first (`ctl.START_S`), then run its verb.
            return await asyncio.wait_for(
                asyncio.to_thread(self._browser_sync, command), timeout=timeout + START_S + 5
            )
        except TimeoutError:
            return {"ok": False, "error": f"sidecar timed out after {timeout}s"}

    def _browser_sync(self, command: dict[str, Any]) -> dict[str, Any]:
        """Every error is a result - never a crashed thread. `ctl.py` never writes to
        stderr (every one of its own error paths prints JSON to stdout), so a reply that fails to
        parse is diagnosed from the stdout tail, not stderr. Docker SDK errors (a dead container,
        a daemon hiccup) and the `assert` below are both just `Exception` from here out, caught
        the same way - the caller never sees a raised exception out of `browser()`."""
        try:
            assert self._container is not None, "frame not created"
            # The two Engine calls an exec needs and no third: `exec_run` also inspects the exit
            # code, which `ctl.py` never uses (it exits 0 and answers on stdout).
            api = self._container.client.api
            run = api.exec_create(
                self._container.id,
                ["python3", "-m", "sidecar.ctl"],
                user=str(BROWSER_UID),
                environment={
                    "SIDECAR_CMD": json.dumps(command),
                    "PYTHONPATH": "/opt/plane/browser",
                    "SIDECAR_RECORDING": self.recording_dir,
                    "SIDECAR_SNAPSHOT_CHARS": str(config.current().worker.browser_snapshot_chars),
                    "SIDECAR_HEADED_UA": "1" if config.current().worker.browser_headed_ua else "",
                    "HOME": "/home/browser",
                },
                workdir="/home/browser",
            )
            stdout_b, _stderr_b = api.exec_start(run["Id"], demux=True)
            try:
                reply = json.loads((stdout_b or b"").decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                tail = (stdout_b or b"").decode(errors="replace")[-400:]
                return {"ok": False, "error": f"sidecar exec failed: {tail}"}
            if not isinstance(reply, dict):
                return {"ok": False, "error": "sidecar reply was not an object"}
            return cast("dict[str, Any]", reply)
        except Exception as e:
            return {"ok": False, "error": f"sidecar exec failed: {type(e).__name__}"}


def _held_version(response: httpx.Response) -> int:
    """The version a 409 says the proxy holds (`{"version": n}`); 0 when the body does not say."""
    try:
        held = response.json().get("version")
        return held if isinstance(held, int) and held > 0 else 0
    except ValueError:
        return 0


def _counted(chunks: Iterable[bytes], limit: int | None) -> Iterator[bytes]:
    """The daemon's archive stream, abandoned past `limit` bytes (`ArchiveTooLarge`)."""
    seen = 0
    for chunk in chunks:
        seen += len(chunk)
        if limit is not None and seen > limit:
            raise ArchiveTooLarge(seen)
        yield chunk


def inventory() -> list[FrameInfo]:
    """Every frame this daemon has that is THIS instance's: labelled with our instance, or
    - for the default instance only - wearing an older unlabelled name. Another dev instance's frames are
    invisible here, so its reconciler and ours never destroy each other's Machines. A stopped
    container is listed too (`all=True`): its files are intact, and a bind starts it."""
    mine = config.current().worker.instance
    out: list[FrameInfo] = []
    # `ignore_removed`: `list` inspects each container it found, and a frame destroyed mid-listing
    # (a step ending, the sweep) is simply no longer there - never an error out of the listing.
    for container in _client().containers.list(all=True, ignore_removed=True):
        name: str = container.name or ""
        try:
            labels = dict(cast("dict[str, str]", container.labels or {}))
            created = parse_rfc3339(container.attrs["Created"])
        except (KeyError, DockerException):
            continue  # its inspect data went with it
        if LABEL_KIND in labels:
            if labels.get(LABEL_INSTANCE, "") != mine:
                continue
        elif mine or not name.startswith(LEGACY_PREFIXES):
            continue
        out.append(FrameInfo(ref=name, labels=labels, created_at=created, driver=DockerFrame.driver))
    return out


def ensure_image(profile: str = "base") -> str:
    """`images.ensure_image` on this process's daemon client. Returns the tag."""
    return images.ensure_image(_client(), profile)


__all__ = [
    "ALL",
    "BROWSER_UID",
    "NETWORK",
    "DockerFrame",
    "docker_client",
    "ensure_image",
]
