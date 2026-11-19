# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The sandbar frame driver - Prismoid's frames, run by the sandbar daemon over HTTP (ADR-013).

`sandbar` is an independent sandbox daemon (repo root `sandbar/`, written in Go). Its shipping
backend is Docker: each sandbox is a runc container on the host's kernel, a separate process
boundary from the worker but not a separate kernel. Its Firecracker microVM backend (its own
kernel in a KVM guest) is experimental and not for serving users. Prismoid never links sandbar's
code: it speaks sandbar's HTTP API, so the isolation technology is a *deployment* choice, not a
code dependency, and the same daemon serves other projects unchanged.

This class satisfies the SAME `FrameDriver` protocol as `DockerFrame`, so nothing above it
notices the swap. The division of labour is deliberate:

* **sandbar owns the sandbox primitives** - create, exec, read/write files, tar archive/extract,
  destroy. That is all a sandbox IS, and it is all sandbar's core interface exposes.
* **Prismoid owns the sandbox's *meaning*** - `publish` (the one harvest door) and
  `manifest` are Prismoid concepts, so they are composed HERE from `exec`, byte-for-byte the same
  script the Docker driver runs. A sandbox does not know what an artifact is; the platform does.

The frame layout (`/task /work /artifacts /skills`), the owner/mode discipline, and the
checkpoint set are identical to the Docker driver - they are the product's contract, not the
backend's.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import IO, Any, ClassVar, Final, Literal, cast

import httpx
import logfire

from prismoid.ai import config
from prismoid.ai.frames.archives import checkpoint_filter, restore_filter, rewrite
from prismoid.ai.frames.driver import (
    ArchiveTooLarge,
    EgressUnavailable,
    ExecResult,
    FrameUnreachable,
    Owner,
)
from prismoid.ai.frames.image_browser.sidecar.ctl import START_S
from prismoid.ai.frames.layout import (
    ARTIFACTS,
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
from prismoid.errors import PathRefused, PolicyDenied
from prismoid.schemas.machines import (
    LABEL_INSTANCE,
    LABEL_KIND,
    LEGACY_PREFIXES,
    FrameInfo,
    parse_resources,
    parse_rfc3339,
)

# A checkpoint moves through a temp file that spills to disk past this, so memory stays bounded
# however large `/work` is.
_SPOOL_BYTES = 8 * 1024 * 1024

# A record's `state`: `missing` is the one that means
# gone - the record outlived its container. A `stopped` sandbox still holds its files (a
# container's writable layer survives a stop), so `bind()` starts it again. `unknown` (the backend
# could not say) and an absent state (an older sandbard) are never read as gone.
_GONE: Final = "missing"


def _state(record: Any) -> str | None:
    state = cast("dict[str, Any]", record).get("state") if isinstance(record, dict) else None
    return state if isinstance(state, str) else None


# sandbar's stale refusal names the version it holds as JSON (`error.version`).
# A sandbar from before the field names it only in its message ("stale: egress version N is not
# newer than M"); the pattern reads M. Tolerated for one release, then removed.
_STALE = re.compile(r"is not newer than (\d+)")

# A path a live sandbox does not hold is sandbar's 404 `error.code: "no_such_path"` (since
# 2026-10, RR-missing-path); any other 404 is the sandbox gone. A sandbar from before the code
# answered 502 with the Engine's words (a file read) or ls's (a directory read). Tolerated for
# one release, then removed.
_OLD_MISSING: Final = ("Could not find the file", "No such file or directory")


class SandbarFrame:
    """One microVM sandbox, one task. Satisfies `FrameDriver` and `deps.FrameLike`.

    Talks to `sandbard` over HTTP. Every method is async and the HTTP client is already async,
    which is the point of putting the sandbox behind a daemon: the blocking work happens in another
    process (or another machine), not on our event loop. The one local CPU work - rewriting a
    checkpoint tar through the `archives` filters - runs off-loop.
    """

    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "archive"
    driver: ClassVar[str] = "sandbar"
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
        self.sandbox_id = name or f"frame-{task_id}"
        self._recording = recording or str(attempt)
        self.egress_version = 0
        self.egress_hosts: list[str] = []
        self._client = httpx.AsyncClient(
            base_url=config.current().worker.sandbar_url,
            headers=_auth_headers(),
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
        )

    @property
    def ref(self) -> str:
        return self.sandbox_id

    @property
    def recording_dir(self) -> str:
        return f"{ARTIFACTS}/recording-{self._recording}"

    # ── lifecycle ─────────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        """Boot the sandbox, **deny-by-default**: with no `allow_domains` it has no route out at
        all (`network=none`: the docker backend's egress is a bridge with a direct route out), and
        no egress principal - it can never be widened (`allow` raises
        `EgressUnavailable`). With domains AND a sandbar daemon running its egress proxy
        (SANDBAR_EGRESS=on), it reaches exactly those domains through the allowlist proxy and
        every refusal is on `denials()`; `egress_version` is the version that list carries, and
        a later `allow` must be newer. The docker backend enforces this today;
        firecracker egress is a roadmap item (docs/firecracker.md) - until then a frame that pins
        the firecracker backend must not pass allow_domains. `labels` ride on the sandbox's spec,
        stamped with the dev instance, so `inventory()` can judge it; the profile's
        resources (`settings.machines.resources`) size it."""
        cfg = config.current()
        if self.profile == "browser":
            image = cfg.worker.sandbar_browser_image
            if not image:
                raise RuntimeError(
                    "PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE is not set; the browser profile has "
                    "no sandbar image yet"
                )
        else:
            image = cfg.worker.sandbar_image
        domains = allow_domains or []
        stamped = dict(labels or {})
        if stamped or cfg.worker.instance:
            # The dev instance rides on every labelled frame (the Docker driver's rule), so
            # `inventory()` shows each worker only its own instance's frames.
            stamped[LABEL_INSTANCE] = cfg.worker.instance
        res = parse_resources(cfg.machines.resources).get(self.profile)
        spec: dict[str, object] = {
            "backend": cfg.worker.sandbar_backend,
            "image": image,
            "allow_domains": domains,
            "labels": stamped,
        }
        if res is not None:
            spec["cpus"] = max(1, round(res.cpus))
            spec["mem_mb"] = res.mem_mb
        await self._post(
            "/v1/sandboxes", json={"id": self.sandbox_id, "spec": spec, "egress_version": egress_version}
        )
        self.egress_version, self.egress_hosts = egress_version, sorted(domains)

    async def bind(self, *, start: bool = True) -> bool:
        """Re-bind to a frame a parked step or a Machine left (a resume), the Docker
        driver's contract. A sandbar sandbox persists by id until destroyed, so bind confirms it is
        still there: a 404, or a `state` of missing, raises `RuntimeError` (gone - the caller wakes
        the Machine from its checkpoint). A `stopped` sandbox (a `docker stop`, a host restart)
        still holds its files, so bind starts it, binds and answers True - only its processes are
        new; with `start=False` it raises `RuntimeError` instead. A start whose container went is
        gone too, and any other failed start raises the HTTP error (one infra attempt, the files
        kept). A state sandbar could not read, or a 200 whose body is not a JSON object (a proxy's
        page, a truncated reply), raises `FrameUnreachable` (one infra attempt, nothing rebuilt). A
        record with no state (a daemon from before it) binds as it always did."""
        resp = await self._client.get(f"/v1/sandboxes/{self.sandbox_id}")
        if resp.status_code == 404:
            raise RuntimeError(f"sandbar frame {self.sandbox_id} is gone")
        resp.raise_for_status()
        try:
            record = resp.json()
        except ValueError:
            record = None
        if not isinstance(record, dict):
            raise FrameUnreachable(f"sandbar's reply about frame {self.sandbox_id} is not a record")
        state = _state(record)
        if state == _GONE:
            raise RuntimeError(f"sandbar frame {self.sandbox_id} is {state}, not running")
        if state == "unknown":
            raise FrameUnreachable(f"sandbar cannot say whether frame {self.sandbox_id} runs")
        if state != "stopped":
            return False
        if not start:
            raise RuntimeError(f"sandbar frame {self.sandbox_id} is stopped, not running")
        started = await self._client.post(f"/v1/sandboxes/{self.sandbox_id}/start")
        if started.status_code == 404:
            raise RuntimeError(f"sandbar frame {self.sandbox_id} is gone; it was stopped")
        started.raise_for_status()
        return True

    async def destroy(self) -> None:
        # Forget the synced allowlist: the id is deterministic (`machine-<id>-<epoch>`), and the
        # closed client below makes a late `allow()` raise instead of reaching a recreated sandbox.
        self.egress_version, self.egress_hosts = 0, []
        try:
            resp = await self._client.delete(f"/v1/sandboxes/{self.sandbox_id}")
            if resp.status_code not in (200, 404):
                resp.raise_for_status()
        except httpx.HTTPError as exc:  # teardown must never raise into a finally block
            logfire.warn("sandbar destroy failed for {id}: {err}", id=self.sandbox_id, err=str(exc))
        finally:
            await self._client.aclose()

    async def close(self) -> None:
        """Nothing is held open past a call (`FrameDriver.close`)."""

    # ── exec ──────────────────────────────────────────────────────────────────────────────────

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        data = await self._post(
            f"/v1/sandboxes/{self.sandbox_id}/exec",
            json={"command": command, "timeout_s": timeout},
        )
        return ExecResult(
            exit_code=int(data["exit_code"]),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
        )

    # ── files ─────────────────────────────────────────────────────────────────────────────────

    async def write_file(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode(), owner="agent")

    async def read_file(self, path: str) -> str:
        return (await self.read_bytes(path)).decode(errors="replace")

    async def read_bytes(self, path: str) -> bytes:
        """A path that is not there raises `FileNotFoundError`, the Docker driver's contract: the
        `read_file` tool answers the model with it instead of the step dying (RR-missing-path)."""
        resp = await self._client.get(f"/v1/sandboxes/{self.sandbox_id}/files", params={"path": path})
        self._raise_for_read(resp, path)
        return resp.content

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent") -> None:
        resp = await self._client.put(
            f"/v1/sandboxes/{self.sandbox_id}/files",
            params={"path": path, "mode": f"{mode:o}", "owner": owner},
            content=data,
        )
        self._raise_if_refused(resp, path)
        resp.raise_for_status()

    async def list_dir(self, path: str) -> list[str]:
        """A directory that is not there lists as nothing - the Docker driver's `ls … || true` and
        the fake frame's - so no caller sees the swap. Every other failure raises. An empty one is
        nothing too, `"entries": null` included (a sandbar from before it sent `[]`)."""
        resp = await self._client.get(f"/v1/sandboxes/{self.sandbox_id}/dir", params={"path": path})
        try:
            self._raise_for_read(resp, path)
        except FileNotFoundError:
            return []
        return list(resp.json().get("entries") or [])

    def _raise_if_refused(self, resp: httpx.Response, path: str) -> None:
        """sandbar's file door said no to the PATH: a 400 (empty, relative, NUL or unclean path, an
        unknown owner) or a 403 `forbidden_path` (the sandbox's own permissions). A policy refusal
        naming the path, which the tools hand to the model; never an `HTTPStatusError` that fails
        the step, and never `FrameUnreachable`, since sandbar answered."""
        if resp.status_code == 400 or (resp.status_code == 403 and _err_code(resp) == "forbidden_path"):
            raise PathRefused(f"{path}: sandbar refused the path ({_err(resp)})")

    def _raise_for_read(self, resp: httpx.Response, path: str) -> None:
        """What a refused file or directory read means: a path the sandbox does not hold is
        `FileNotFoundError` (sandbar's message names the path); every other failure raises as it
        always did.

        Any other 404 is the sandbox gone (a restarted sandbard forgets every sandbox), and it
        surfaces as `exec`'s 404 does (`_post`): `PolicyDenied`, which `execution` counts as one
        failed attempt whose retry re-binds or wakes the Machine. Never `bind()`'s
        `RuntimeError` here: inside an agent run nothing catches one, and it fails the task."""
        if resp.status_code < 300:
            return
        self._raise_if_refused(resp, path)
        reason = _err(resp)
        if resp.status_code == 404:
            if _err_code(resp) == "no_such_path":
                raise FileNotFoundError(reason)
            raise PolicyDenied(f"sandbar 404: {reason}")
        if resp.status_code == 502 and any(words in reason for words in _OLD_MISSING):
            raise FileNotFoundError(reason)  # an older sandbar (one release)
        resp.raise_for_status()

    # ── publish - Prismoid's door, composed from exec, identical to the Docker driver ────────────────

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        """**The only door**. Validate the extension, hash the bytes, copy into
        `/artifacts`, append a manifest entry - the exact same boundary the Docker driver enforces,
        because it is the product's contract, not the sandbox's. Injection-proof the same way
        (`ai/frames/publish.py`): static runner + JSON args over the file transport; the one exec
        is a fixed command line carrying no model-authored strings."""
        validate_publish(type, path)
        args = publish_args(path, artifact_target(path), MANIFEST, type, title, list(sources))
        await self.write_bytes(RUNNER_PATH, PUBLISH_RUNNER.encode(), mode=0o444, owner="system")
        await self.write_bytes(ARGS_PATH, json.dumps(args).encode(), mode=0o444, owner="system")
        result = await self.exec(f"python3 {RUNNER_PATH} {ARGS_PATH}")
        if result.exit_code != 0:
            raise PolicyDenied(f"publish failed: {result.stderr.strip()[:200]}")

    async def manifest(self) -> dict[str, Any]:
        return await read_manifest(self)

    # ── the browser sidecar ──────────────────────────────────────────────────────────────

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        """Every error is a result, the same contract `DockerFrame._browser_sync` gives -
        no exception ever escapes this method, and no result ever carries sandbar's own URL or a
        raw response body a model could read as instructions. `cd` into `/home/browser` is baked
        into the command itself because sandbar's exec has no per-call working directory (the
        container's own `WorkingDir` is `/work`, set once at create).

        The whole body is one `try/except Exception` (not just `httpx.HTTPError`): a non-JSON
        200, a non-dict JSON body, or a call after `destroy()` closed `self._client` (`RuntimeError`,
        not an `httpx.HTTPError`) must all become the same honest result, not an exception that
        escapes to the caller. A non-2xx status becomes `HTTP {status}` with no URL and no
        response body - `str(exc)` on an `httpx.HTTPStatusError` includes the full request URL,
        which is sandbar's internal address, not something a model-visible result should carry."""
        if self.profile != "browser":
            return {"ok": False, "error": "no browser in this frame"}
        timeout = config.current().computer_use.command_timeout_s if timeout is None else timeout
        try:
            r = await self._client.post(
                f"/v1/sandboxes/{self.sandbox_id}/exec",
                json={
                    "command": "cd /home/browser && python3 -m sidecar.ctl",
                    "timeout_s": timeout + START_S,  # a call may cold-start the sidecar first
                    "user": "browser",
                    "env": {
                        "SIDECAR_CMD": json.dumps(command),
                        "PYTHONPATH": "/opt/plane/browser",
                        "SIDECAR_RECORDING": self.recording_dir,
                        "SIDECAR_SNAPSHOT_CHARS": str(config.current().worker.browser_snapshot_chars),
                        "SIDECAR_HEADED_UA": "1" if config.current().worker.browser_headed_ua else "",
                        "HOME": "/home/browser",
                    },
                },
                timeout=httpx.Timeout(timeout + START_S + 15, connect=10.0),
            )
            if r.status_code >= 300:
                return {"ok": False, "error": f"sidecar exec failed: HTTP {r.status_code}"}
            body = r.json()
            if not isinstance(body, dict):
                return {"ok": False, "error": "sidecar exec failed: response was not an object"}
            stdout = str(cast("dict[str, Any]", body).get("stdout", ""))
            try:
                reply = json.loads(stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                tail = stdout[-400:]
                return {"ok": False, "error": f"sidecar exec failed: {tail}"}
            if not isinstance(reply, dict):
                return {"ok": False, "error": "sidecar reply was not an object"}
            return cast("dict[str, Any]", reply)
        except Exception as e:
            return {"ok": False, "error": f"sidecar exec failed: {type(e).__name__}"}

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        """REPLACE the running frame's allowlist, versioned, via the
        daemon's egress route - the Docker driver's semantics exactly.

        Unpinned, the push is one past the driver's synced version - or, when it has synced none
        (`egress_version == 0`, a driver found by `bind()`), an UNVERSIONED push (no `version`
        field, which sandbar applies while keeping its recorded version), so the row's version
        stays authoritative. Every versioned push that is not newer (locally, or a 409 from the
        daemon) returns False, logged, and is never retried here: a slow push must not overwrite
        a newer list. A 409 names the version sandbar holds (`error.version`; the message for an
        older sandbar) and the driver adopts it, so a caller that owns
        the list (the Machine's holder) can push once past it. A 400 means the sandbox was born without an egress principal (sandbar keeps
        a no-domain sandbox airtight) and raises `EgressUnavailable`. A 404 means the sandbox
        itself is gone and reports it exactly the way `bind()` does; any other non-2xx surfaces
        the daemon's own reason via `_err`."""
        if version is None and self.egress_version == 0:
            if await self._put_egress(domains, None) is not None:
                return False
            self.egress_hosts = sorted(domains)
            return True
        version = self.egress_version + 1 if version is None else version
        if version <= self.egress_version:
            logfire.warn(
                "stale egress push dropped for {id}: v{v} <= v{cur}",
                id=self.sandbox_id,
                v=version,
                cur=self.egress_version,
            )
            return False
        held = await self._put_egress(domains, version)
        if held is not None:
            self.egress_version = max(self.egress_version, held)
            return False
        self.egress_version, self.egress_hosts = version, sorted(domains)
        return True

    async def _put_egress(self, domains: list[str], version: int | None) -> int | None:
        """Push one list (`version=None`: unversioned). None when it applied; on a 409 (a stale
        version) the version sandbar holds (`error.version`; the message for an older sandbar; 0 when neither says)."""
        body: dict[str, Any] = {"allow_domains": list(domains)}
        if version is not None:
            body["version"] = version
        response = await self._request("PUT", f"/v1/sandboxes/{self.sandbox_id}/egress", json=body)
        if response.status_code == 404:
            raise RuntimeError(f"sandbar frame {self.sandbox_id} is gone")
        if response.status_code == 400:
            # The egress route's one refusal of a well-formed push: no principal to re-register
            # (born with no domains, or a backend with no egress at all). No push will ever land.
            raise EgressUnavailable(f"sandbar frame {self.sandbox_id} cannot be widened: {_err(response)}")
        if response.status_code == 409:
            reason = _err(response)
            logfire.warn(
                "sandbar refused a stale egress v{v} for {id}: {err}",
                v=version,
                id=self.sandbox_id,
                err=reason,
            )
            return _held_version(response, reason)
        if response.status_code >= 300:
            raise RuntimeError(f"sandbar egress {response.status_code}: {_err(response)}")
        return None

    # ── the Machine's verbs ───────────────────────────────────────────────────────────────────

    async def freeze(self) -> None:
        # `kill -1` as the agent reaches every process the agent may signal except the caller and
        # PID 1; the browser user's sidecar is untouched (the Docker driver's rule).
        await self.exec("kill -STOP -1 2>/dev/null; true", timeout=30)

    async def thaw(self) -> None:
        await self.exec("kill -CONT -1 2>/dev/null; true", timeout=30)

    async def archive(self, path: str, out: IO[bytes], *, limit: int | None = None) -> None:
        """sandbar's `GET .../archive?path=` tars the live filesystem's own uid/gid/mode (plain
        tar, rooted at the path's basename). An absent directory arrives as a complete empty tar,
        so that writes an empty archive; every other failure raises, as the Docker driver's does.

        The whole body is spooled BEFORE anything reaches `out`, then rewritten off-loop through
        `checkpoint_filter`. sandbar aborts the connection when the backend fails after the 200,
        and that broken body is the only signal: `tarfile` reads a tar cut at an entry boundary as
        a clean end, so a body taken as whole would be a cut-short checkpoint that loses the
        rest of `/work` once the frame is destroyed. `limit` is counted as the body arrives (a
        cap applied to `out` would only see the spool once it was whole): past it the stream is
        dropped and `ArchiveTooLarge` raised, one chunk past the limit at most."""
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as raw:
            seen = 0
            try:
                async with self._client.stream(
                    "GET", f"/v1/sandboxes/{self.sandbox_id}/archive", params=[("path", path)]
                ) as response:
                    if response.status_code >= 300:
                        await response.aread()
                        raise RuntimeError(f"sandbar archive {response.status_code}: {_err(response)}")
                    async for chunk in response.aiter_bytes():
                        seen += len(chunk)
                        if limit is not None and seen > limit:
                            raise ArchiveTooLarge(seen)
                        raw.write(chunk)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"sandbar archive of {path} failed: {type(exc).__name__}: {exc}") from exc
            raw.seek(0)
            await asyncio.to_thread(rewrite, raw, out, checkpoint_filter(path))

    async def unpack(self, path: str, src: IO[bytes]) -> None:
        """sandbar's `POST .../archive` extracts at `/` as root and keeps each header's
        uid/gid/mode exactly - so `restore_filter`'s stamps, applied here before the bytes leave,
        are what land a correctly-owned restore (never a follow-up chown)."""
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as spool:
            await asyncio.to_thread(rewrite, src, spool, restore_filter(path))
            spool.seek(0)

            async def body() -> AsyncIterator[bytes]:
                while chunk := await asyncio.to_thread(spool.read, 1 << 20):
                    yield chunk

            await self._extract(body())

    async def reset_mounts(self) -> None:
        """The boundary takes the last step's mounts away, as the Docker
        driver does: every entry of `/task/inputs` - files and directories - and the instruction
        files go through sandbar's remove route, which removes them as root, their owner, and takes
        nothing outside `/task`; `/task` and `/task/inputs` stay 0555 and root's, and the step
        marker stays (the boundary rewrites it). `/skills/<name>` is the agent's, so it is cleared
        as the agent."""
        task = set(await self.list_dir(TASK))
        stale = [f"{INPUTS}/{name.rstrip('/')}" for name in await self.list_dir(INPUTS)]
        stale += [path for path in INSTRUCTIONS if path.rsplit("/", 1)[1] in task]
        if stale:
            removed = await self._request(
                "POST", f"/v1/sandboxes/{self.sandbox_id}/remove", json={"paths": stale}
            )
            if removed.status_code >= 300:
                raise RuntimeError(f"sandbar frame {self.sandbox_id} kept its mounts: {_err(removed)}")
        cleared = await self.exec(SKILLS_RESET)
        if not cleared.ok:
            raise RuntimeError(
                f"sandbar frame {self.sandbox_id} kept its skills: {cleared.stderr.strip()[:200]}"
            )

    async def _extract(self, content: bytes | AsyncIterator[bytes]) -> None:
        """sandbar's `POST .../archive`: a tar extracted at `/` as root, each header's uid, gid and
        mode kept exactly."""
        response = await self._client.post(f"/v1/sandboxes/{self.sandbox_id}/archive", content=content)
        if response.status_code >= 300:
            raise RuntimeError(f"sandbar extract {response.status_code}: {_err(response)}")

    async def denials(self) -> list[dict[str, str]]:
        """What sandbar's egress allowlist refused for this frame - journalled as
        `tool.called ok=false`. Off-loop (the docker driver's reason). Empty when
        the frame has no egress (the daemon ran without a proxy, or the backend is a v1
        microVM with no NIC) - an honest nothing, never a guess."""

        def _get() -> httpx.Response:
            return httpx.get(
                f"{config.current().worker.sandbar_url}/v1/sandboxes/{self.sandbox_id}/denials",
                headers=_auth_headers(),
                timeout=5.0,
            )

        try:
            resp = await asyncio.to_thread(_get)
            resp.raise_for_status()
            return list(resp.json().get("denials", []))
        except httpx.HTTPError:
            return []

    # ── HTTP plumbing ─────────────────────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return await self._client.request(method, path, **kw)

    async def _post(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", path, json=json)
        if resp.status_code >= 300:
            raise PolicyDenied(f"sandbar {resp.status_code}: {_err(resp)}")
        return dict(resp.json())


def _auth_headers() -> dict[str, str]:
    token = config.current().worker.sandbar_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _err(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", resp.text))
    except Exception:
        return resp.text[:200]


def _err_code(resp: httpx.Response) -> str | None:
    """sandbar's `error.code` (`no_such_path`, `not_found`, `stale`), None when it names none."""
    try:
        code = resp.json().get("error", {}).get("code")
    except Exception:
        return None
    return code if isinstance(code, str) else None


def _held_version(resp: httpx.Response, reason: str) -> int:
    """The version sandbar holds, from its 409: `error.version` when it is an integer, else the old
    message pattern (one release of tolerance), else 0 (adopt nothing)."""
    try:
        held = resp.json().get("error", {}).get("version")
    except Exception:
        held = None
    if isinstance(held, int) and not isinstance(held, bool):
        return held
    match = _STALE.search(reason)
    return int(match.group(1)) if match else 0


def _control_client() -> httpx.AsyncClient:
    """The daemon, outside any one frame - a seam tests replace."""
    return httpx.AsyncClient(
        base_url=config.current().worker.sandbar_url, headers=_auth_headers(), timeout=30.0
    )


async def inventory() -> list[FrameInfo]:
    """Every sandbox the daemon runs that is THIS instance's: labelled with our instance,
    or - for the default instance only - wearing an older unlabelled name. The Docker driver's rule, so two
    dev instances' reconcilers never destroy each other's Machines."""
    async with _control_client() as client:
        response = await client.get("/v1/sandboxes")
        response.raise_for_status()
    mine = config.current().worker.instance
    out: list[FrameInfo] = []
    for item in response.json().get("items", []):
        labels = dict(cast("dict[str, str]", (item.get("spec") or {}).get("labels") or {}))
        ref = str(item["id"])
        if LABEL_KIND in labels:
            if labels.get(LABEL_INSTANCE, "") != mine:
                continue
        elif mine or not ref.startswith(LEGACY_PREFIXES):
            continue
        out.append(
            FrameInfo(
                ref=ref,
                labels=labels,
                created_at=parse_rfc3339(str(item["created_at"])),
                driver=SandbarFrame.driver,
            )
        )
    return out
