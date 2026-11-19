# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A device frame's Docker objects at a sleep and at its end, against a
fake daemon.

The proxy journals every object it makes (`docker_journal`), and the helper acts only on journaled
ids that still wear the frame's label: an image's `LABEL prismoid.frame=<ref>` forges the label, never
the journal. The reconciler destroys a frame Postgres disowns with
`frames.destroy_ref(ref, driver="device")` (operations/machines/reconcile.py): the worker's
`DeviceFrame.destroy`, the helper's `DELETE /frames/{ref}`, and there `DockerProxies.destroy`. With
Docker away the cleanup waits in the journal and is tried again at the helper's next start.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette

from prismoid.ai import frames
from prismoid.ai.frames import device_driver
from prismoid.conf import settings
from prismoid.device import docker_proxy
from prismoid.device.docker_journal import CAP, Journal
from prismoid.device.docker_policy import FRAME_LABEL, TASK_LABEL
from prismoid.device.helper import KEY_HEADER
from prismoid.runner.device_door import create_app

REF = "device-t1"
MASKED = {"MaskedPaths": ["/proc/kcore"], "ReadonlyPaths": ["/proc/sys", "/proc/sysrq-trigger"]}


def _go(obj: dict[str, Any], name: str, default: Any = None) -> Any:
    """`obj[name]` as Go's `encoding/json` reads a struct field: in any case, the last key wins."""
    found = default
    for key, value in obj.items():
        if key.casefold() == name.casefold():
            found = value
    return found


def _mounts(host: dict[str, Any]) -> list[dict[str, Any]]:
    """The mounts a container's inspect lists for its `HostConfig`, as the daemon resolves them."""
    out: list[dict[str, Any]] = []
    for bind in _go(host, "Binds") or []:
        source, target = bind.split(":")[:2]
        kind = (
            {"Type": "bind", "Source": source}
            if source.startswith("/")
            else {"Type": "volume", "Name": source}
        )
        out.append({**kind, "Destination": target, "RW": True})
    for mount in _go(host, "Mounts") or []:
        if _go(mount, "Type") == "volume":
            out.append({"Type": "volume", "Name": _go(mount, "Source"), "Destination": _go(mount, "Target")})
    return out


class FakeDocker:
    """A Docker daemon on a Unix socket that keeps its objects and lists more than any filter asks
    for: another frame's (`c2`), an unlabelled one (`c3`) and `forged`, which wears this frame's
    label from its image but was never made through the proxy. It reads a body's keys in any case,
    as Docker does, and records every request. `relabel` makes it put other labels on what it makes,
    and `host_extra` read host fields the proxy did not send (a daemon the policy misread)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: list[tuple[str, str]] = []
        self._server: asyncio.Server | None = None
        mine, theirs = {FRAME_LABEL: REF}, {FRAME_LABEL: "device-other"}
        self.containers: dict[str, dict[str, Any]] = {
            "forged": {"Labels": mine, "State": "running"},
            "c2": {"Labels": theirs, "State": "running"},
            "c3": {"Labels": {}, "State": "running"},
        }
        self.networks: dict[str, dict[str, Any]] = {"forged-net": {"Labels": mine}}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.relabel: dict[str, str] | None = None
        self.host_extra: dict[str, Any] = {}
        self.slow_lookups = 0.0
        self._made = 0

    def _labels(self, body: dict[str, Any]) -> dict[str, str]:
        return dict(self.relabel if self.relabel is not None else _go(body, "Labels") or {})

    def answer(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        route = re.sub(r"^/v[0-9.]+/", "/", urlsplit(path).path)
        parts = route.strip("/").split("/")
        if (method, route) == ("POST", "/containers/create"):
            self._made += 1
            ident = f"c{self._made}"
            host = {**MASKED, **(_go(body, "HostConfig") or {}), **self.host_extra}
            self.containers[ident] = {
                "Labels": self._labels(body),
                "State": "running",  # and started
                "HostConfig": host,
                "Mounts": _mounts(host),
            }
            return 201, {"Id": ident, "Warnings": []}
        if (method, route) == ("POST", "/networks/create"):
            self.networks["n1"] = {"Labels": self._labels(body)}
            return 201, {"Id": "n1", "Warning": ""}
        if (method, route) == ("POST", "/volumes/create"):
            name = _go(body, "Name") or f"anon{len(self.volumes)}"
            self.volumes.setdefault(name, {"Labels": self._labels(body)})  # an existing one: as it is
            return 201, {"Name": name, **self.volumes[name]}
        if (method, route) == ("GET", "/containers/json"):
            return 200, [{"Id": i, **c} for i, c in self.containers.items()]
        if (method, route) == ("GET", "/networks"):
            return 200, [{"Id": i, **n} for i, n in self.networks.items()]
        if (method, route) == ("GET", "/volumes"):
            return 200, {"Volumes": [{"Name": n, **v} for n, v in self.volumes.items()]}
        if method == "GET" and parts[0] == "containers" and len(parts) == 3 and parts[2] == "json":
            c = self.containers.get(parts[1])
            if c is None:
                return 404, None
            host = c.get("HostConfig", MASKED)
            return 200, {
                "Id": parts[1],
                "Config": {"Labels": c["Labels"]},
                "HostConfig": host,
                "Mounts": c.get("Mounts", []),
            }
        if method == "GET" and parts[0] in ("networks", "volumes") and len(parts) == 2:
            table = self.networks if parts[0] == "networks" else self.volumes
            found = table.get(parts[1])
            return (200, {"Id": parts[1], "Name": parts[1], **found}) if found else (404, None)
        if method == "DELETE" and parts[0] in ("containers", "networks", "volumes"):
            table = {"containers": self.containers, "networks": self.networks, "volumes": self.volumes}[
                parts[0]
            ]
            return (204, None) if table.pop(parts[1], None) is not None else (404, None)
        return 204, None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        method, path, _ = head.split("\r\n")[0].split(" ")
        length = re.search(r"(?i)content-length: *([0-9]+)", head)
        raw = await reader.readexactly(int(length[1])) if length else b""
        self.seen.append((method, path))
        if method == "GET" and self.slow_lookups:
            await asyncio.sleep(self.slow_lookups)
        status, doc = self.answer(method, path, json.loads(raw) if raw else {})
        data = b"" if doc is None else json.dumps(doc).encode()
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
        )
        await writer.drain()
        writer.close()

    async def up(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def down(self) -> None:
        assert self._server is not None
        self._server.close()
        self.path.unlink(missing_ok=True)  # Docker away: its socket is gone

    def acted(self) -> list[tuple[str, str]]:
        return [(m, urlsplit(p).path) for m, p in self.seen if m != "GET"]


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="pdc-", dir="/tmp"))  # a Unix socket path must be short
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def docker(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeDocker]:
    fake = FakeDocker(short_dir / "docker.sock")
    await fake.up()
    monkeypatch.setattr(docker_proxy, "find_upstream", lambda: str(fake.path))
    yield fake
    if fake.path.exists():
        await fake.down()


class Helpers:
    """Helper lives on one home: each `life()` is a fresh process's state (a restart)."""

    def __init__(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home, self.monkeypatch = home, monkeypatch
        self.apps: list[Starlette] = []
        monkeypatch.setattr(settings.local, "home", home)

    def life(self) -> httpx.AsyncClient:
        app = create_app(self.home, device_name=lambda: "Mac")
        self.apps.append(app)
        # The worker's driver dials the helper's socket; here it reaches this life's app in process.
        self.monkeypatch.setattr(
            device_driver.httpx, "AsyncHTTPTransport", lambda **_: httpx.ASGITransport(app=app)
        )
        key = (self.home / "helper.key").read_text().strip()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
        )

    @property
    def dockers(self) -> docker_proxy.DockerProxies:
        return self.apps[-1].state.dockers


@pytest.fixture
async def helpers(
    short_dir: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Helpers]:
    h = Helpers(short_dir / "home", monkeypatch)
    yield h
    for app in h.apps:
        await app.state.dockers.close_all()


async def _frame(helper: httpx.AsyncClient, *, docker: bool, task: str = "t1") -> None:
    made = await helper.post("/frames", json={"task_id": task, "kind": "machine", "docker": docker})
    assert made.status_code == 200, made.text


async def _through_proxy(
    home: Path,
    route: str,
    body: dict[str, Any] | bytes | None = None,
    method: str = "POST",
    ref: str = REF,
) -> int:
    """What the frame's own Docker CLI does, through its filtering socket: the answer's status."""
    reader, writer = await asyncio.open_unix_connection(str(docker_proxy.socket_path(home, ref)))
    data = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    writer.write(
        f"{method} {route} HTTP/1.1\r\nHost: docker\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
    )
    await writer.drain()
    answer = await reader.read()
    writer.close()
    return int(answer.split(b" ")[1])


async def _made(helpers: Helpers, helper: httpx.AsyncClient) -> None:
    """A frame with Docker that made a container and a network through its proxy."""
    await _frame(helper, docker=True)
    assert await _through_proxy(helpers.home, "/v1.47/containers/create", {"Image": "alpine"}) == 201
    assert await _through_proxy(helpers.home, "/networks/create", {"Name": "demo_default"}) == 201
    assert helpers.dockers.journal.entry(REF) == {"containers": ["c1"], "networks": ["n1"], "volumes": []}


async def test_the_reconcilers_destroy_removes_only_what_the_frame_made(
    helpers: Helpers, docker: FakeDocker
) -> None:
    helper = helpers.life()
    await _made(helpers, helper)
    docker.seen.clear()
    await frames.destroy_ref(REF, driver="device")  # what the reconciler calls for a disowned frame
    # Stopped first: a container writes the grants until it stops, and the last walk comes after.
    assert docker.acted() == [
        ("POST", "/containers/c1/stop"),
        *sorted([("DELETE", "/containers/c1"), ("DELETE", "/networks/n1")]),
    ], "never `forged` (its image's label), another frame's or an unlabelled one"
    assert any(p.endswith("?force=1&v=1") for _, p in docker.seen if p.startswith("/containers/c1"))
    lists = [p for m, p in docker.seen if m == "GET" and "filters" in p]
    assert lists and all(
        json.loads(parse_qs(urlsplit(p).query)["filters"][0]) == {"label": [f"{FRAME_LABEL}={REF}"]}
        for p in lists
    ), "listed by the frame's label alone, never by journaled ids"
    assert helpers.dockers.journal.pending() == [] and helpers.dockers.journal.entry(REF) == {}
    assert (await helper.get(f"/frames/{REF}")).status_code == 404


async def test_a_sleep_stops_only_journaled_containers(helpers: Helpers, docker: FakeDocker) -> None:
    helper = helpers.life()
    await _made(helpers, helper)
    docker.seen.clear()
    assert (await helper.post(f"/frames/{REF}/stop-services")).status_code == 204
    assert docker.acted() == [("POST", "/containers/c1/stop")], "a sleep stops; it removes nothing"
    await helper.delete(f"/frames/{REF}")
    docker.seen.clear()
    await _frame(helper, docker=False)
    await helper.post(f"/frames/{REF}/stop-services")
    await helper.delete(f"/frames/{REF}")
    assert docker.seen == [], "a frame that made nothing through Docker never wakes it"


async def test_a_restart_then_a_sleep_stops_the_journaled_containers(
    helpers: Helpers, docker: FakeDocker
) -> None:
    await _made(helpers, helpers.life())
    revoked = await helpers.life().put(f"/frames/{REF}/grants", json={"grants": [], "docker": False})
    assert revoked.status_code == 200, "a fresh helper life, the grant revoked since"
    docker.seen.clear()
    assert (await helpers.life().post(f"/frames/{REF}/stop-services")).status_code == 204
    assert docker.acted() == [("POST", "/containers/c1/stop")]


async def test_a_cleanup_with_docker_away_is_tried_again_at_the_next_start(
    helpers: Helpers, docker: FakeDocker
) -> None:
    await _made(helpers, helpers.life())
    await docker.down()
    assert (await helpers.life().delete(f"/frames/{REF}")).status_code == 204, "a frame's end never waits"
    if (tried := helpers.dockers.retrying) is not None:
        await tried  # this life's own retry, with Docker still away
    [(ref, pending)] = helpers.dockers.journal.pending()
    assert ref == REF and pending.stat().st_mode & 0o777 == 0o600, "the cleanup waits in the journal"
    await docker.up()
    docker.seen.clear()
    helpers.life()
    async with helpers.apps[-1].router.lifespan_context(helpers.apps[-1]):  # the helper starts
        assert helpers.dockers.retrying is not None
        await helpers.dockers.retrying
    assert sorted(docker.acted()) == [("DELETE", "/containers/c1"), ("DELETE", "/networks/n1")]
    assert helpers.dockers.journal.pending() == [], "done: the journal goes"


async def test_a_frame_gone_while_the_helper_was_down_is_cleaned_at_start(
    helpers: Helpers, docker: FakeDocker
) -> None:
    await _made(helpers, helpers.life())
    shutil.rmtree(helpers.home / "tasks" / "t1")  # the frame's folder went with the helper down
    docker.seen.clear()
    helpers.life()
    async with helpers.apps[-1].router.lifespan_context(helpers.apps[-1]):
        assert helpers.dockers.retrying is not None
        await helpers.dockers.retrying
    assert ("DELETE", "/containers/c1") in docker.acted()


async def test_destroy_without_a_daemon_is_quiet(
    helpers: Helpers, docker: FakeDocker, caplog: pytest.LogCaptureFixture
) -> None:
    helper = helpers.life()
    await _made(helpers, helper)
    await docker.down()
    with caplog.at_level("WARNING"):
        assert (await helper.delete(f"/frames/{REF}")).status_code == 204
    assert not [r for r in caplog.records if r.name == docker_proxy.__name__], "no daemon: nothing to say"


def _failing_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Journal, "record", full)


async def test_an_existing_volume_is_never_the_creates(
    helpers: Helpers, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I1: Docker answers 201 for a volume name that exists, so a create that could not be
    journaled must never remove it - it may be the owner's database."""
    await _frame(helpers.life(), docker=True)
    docker.volumes["owners_pgdata"] = {"Labels": {}}
    for spelling in ("Name", "name", "NAME"):  # Docker reads `name` as `Name`
        assert await _through_proxy(helpers.home, "/volumes/create", {spelling: "owners_pgdata"}) == 403
    docker.volumes["mine_pgdata"] = {"Labels": {FRAME_LABEL: REF, TASK_LABEL: "t1"}}  # an earlier life's
    _failing_journal(monkeypatch)  # the agent filled the disk
    for spelling in ("Name", "name"):
        assert await _through_proxy(helpers.home, "/volumes/create", {spelling: "mine_pgdata"}) == 500
        assert await _through_proxy(helpers.home, "/volumes/create", {spelling: "owners_pgdata"}) == 403
    assert {"owners_pgdata", "mine_pgdata"} <= set(docker.volumes), "neither existing volume is removed"
    assert ("DELETE", "/volumes/owners_pgdata") not in docker.acted()
    assert ("DELETE", "/volumes/mine_pgdata") not in docker.acted()
    assert await _through_proxy(helpers.home, "/volumes/create", {"Name": "fresh"}) == 500
    assert "fresh" not in docker.volumes, "what this very call made is removed again"


async def test_a_volume_wearing_the_frames_label_is_adopted(helpers: Helpers, docker: FakeDocker) -> None:
    """Review M2: a Machine rebuilt under its ref reaches the named volumes its earlier life made."""
    await _frame(helpers.life(), docker=True)
    docker.volumes["demo_pgdata"] = {"Labels": {FRAME_LABEL: REF, TASK_LABEL: "t1"}}
    assert await _through_proxy(helpers.home, "/volumes/demo_pgdata", method="GET") == 200
    assert helpers.dockers.journal.holds(REF, "volumes", "demo_pgdata")
    docker.containers["forged"]["Labels"] = {FRAME_LABEL: REF}
    assert await _through_proxy(helpers.home, "/containers/forged/json", method="GET") == 403, (
        "a container is never adopted: it takes its image's labels"
    )


async def test_a_removal_through_the_proxy_leaves_the_journal(helpers: Helpers, docker: FakeDocker) -> None:
    helper = helpers.life()
    await _made(helpers, helper)
    assert await _through_proxy(helpers.home, "/v1.47/containers/c1?force=1", method="DELETE") == 204
    assert helpers.dockers.journal.entry(REF)["containers"] == []


async def test_a_large_journal_lists_by_label_and_prunes(helpers: Helpers, docker: FakeDocker) -> None:
    """Review I2: journaled ids never go in a query (20k of them pass the daemon's 1 MiB request
    line), and a sleep prunes the ids of what is gone."""
    helper = helpers.life()
    await _made(helpers, helper)
    path = helpers.home / "run" / "docker" / f"{REF}.json"
    gone = [f"{i:064x}" for i in range(20_000)]
    path.write_text(json.dumps({"containers": ["c1", *gone], "networks": ["n1"], "volumes": []}))
    helpers.life()  # a fresh life reads it from disk
    docker.seen.clear()
    assert (await helpers.life().post(f"/frames/{REF}/stop-services")).status_code == 204
    assert ("POST", "/containers/c1/stop") in docker.acted()
    assert max(len(p) for _, p in docker.seen) < 1000, "no request carries the journal"
    assert helpers.dockers.journal.entry(REF) == {"containers": ["c1"], "networks": ["n1"], "volumes": []}


async def test_a_create_past_the_cap_is_refused(helpers: Helpers, docker: FakeDocker) -> None:
    helper = helpers.life()
    await _frame(helper, docker=True)
    ids = [f"{i:064x}" for i in range(CAP)]
    for i in ids:  # the daemon has them all, each this frame's
        docker.containers[i] = {"Labels": {FRAME_LABEL: REF}, "State": "exited"}
    path = helpers.home / "run" / "docker" / f"{REF}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"containers": ids, "networks": [], "volumes": []}))
    helpers.life()
    await helpers.life().put(f"/frames/{REF}/grants", json={"grants": [], "docker": True})  # serves it
    assert await _through_proxy(helpers.home, "/containers/create", {"Image": "alpine"}) == 403
    for i in ids[:10]:
        del docker.containers[i]  # removed outside the proxy (`docker run --rm`)
    assert await _through_proxy(helpers.home, "/containers/create", {"Image": "alpine"}) == 201, (
        "at the cap, what is gone is pruned first"
    )
    assert helpers.dockers.journal.count(REF) == CAP - 10 + 1


async def test_a_pending_cleanup_is_retried_on_a_timer(
    helpers: Helpers, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: Docker coming back is noticed with no other event."""
    monkeypatch.setattr(docker_proxy, "_RETRY_EVERY_S", 0.05)
    await _made(helpers, helpers.life())
    await docker.down()
    helper = helpers.life()
    app = helpers.apps[-1]
    async with app.router.lifespan_context(app):
        assert (await helper.delete(f"/frames/{REF}")).status_code == 204
        assert helpers.dockers.journal.pending()
        await docker.up()
        for _ in range(100):
            if not helpers.dockers.journal.pending():
                break
            await asyncio.sleep(0.05)
    assert helpers.dockers.journal.pending() == [] and ("DELETE", "/containers/c1") in docker.acted()


async def test_a_frame_made_while_its_destroy_runs_waits_for_it(helpers: Helpers, docker: FakeDocker) -> None:
    """Review M5: one lock spans a frame's destroy; a POST for its ref lands after, never between."""
    helper = helpers.life()
    await _frame(helper, docker=True)
    lock = helpers.dockers.lock(REF)
    await lock.acquire()  # a destroy in flight
    made = asyncio.create_task(
        helper.post("/frames", json={"task_id": "t1", "kind": "machine", "docker": True})
    )
    await asyncio.sleep(0.1)
    assert not made.done(), "the POST waits for the destroy"
    lock.release()
    assert (await made).status_code == 200


async def test_lowercase_keys_never_pass_the_proxy(helpers: Helpers, docker: FakeDocker) -> None:
    """Review C1, N1: the daemon reads `hostconfig` as `HostConfig`, so the proxy does too, and what
    it forwards is its own encoding of what it vetted."""
    await _frame(helpers.life(), docker=True)
    docker.volumes["owners_pgdata"] = {"Labels": {}}  # a stand-in for the owner's database
    privileged = {"Image": "alpine", "hostconfig": {"Privileged": True, "PidMode": "host"}}
    assert await _through_proxy(helpers.home, "/containers/create", privileged) == 403
    for host in (
        {"mounts": [{"type": "volume", "source": "owners_pgdata", "target": "/v"}]},
        {"binds": ["owners_pgdata:/v"]},
    ):
        mounting = {"Image": "alpine", "hostconfig": host}
        assert await _through_proxy(helpers.home, "/containers/create", mounting) == 403
    forged = b'{"Name": "seeded", "Labels": {}, "labels": {"prismoid.frame": "device-t2"}}'
    assert await _through_proxy(helpers.home, "/volumes/create", forged) == 403
    assert not [p for m, p in docker.acted() if m == "POST"], "nothing was made"
    assert set(docker.volumes) == {"owners_pgdata"} and docker.volumes["owners_pgdata"] == {"Labels": {}}
    lowercase = {"Image": "alpine", "labels": {FRAME_LABEL: "device-t2", "app": "web"}}
    assert await _through_proxy(helpers.home, "/containers/create", lowercase) == 201
    assert docker.containers["c1"]["Labels"] == {"app": "web", FRAME_LABEL: REF, TASK_LABEL: "t1"}


async def test_what_a_create_made_is_looked_at(helpers: Helpers, docker: FakeDocker) -> None:
    """After every create the object is inspected: one that does not wear this frame's and this
    task's labels, or a container the daemon holds otherwise than the policy sent, is removed again
    and refused - and a volume without them was never this call's, so it is left."""
    await _frame(helpers.life(), docker=True)
    docker.relabel = {FRAME_LABEL: "device-other", TASK_LABEL: "other"}
    assert await _through_proxy(helpers.home, "/containers/create", {"Image": "alpine"}) == 403
    assert await _through_proxy(helpers.home, "/networks/create", {"Name": "demo_default"}) == 403
    assert await _through_proxy(helpers.home, "/volumes/create", {"Name": "fresh"}) == 403
    assert sorted(docker.acted()) == [
        ("DELETE", "/containers/c1"),
        ("DELETE", "/networks/n1"),
        ("POST", "/containers/create"),
        ("POST", "/networks/create"),
        ("POST", "/volumes/create"),
    ], "never DELETE /volumes/fresh"
    docker.relabel, docker.host_extra = None, {"Privileged": True}  # a field the policy misread
    assert await _through_proxy(helpers.home, "/containers/create", {"Image": "alpine"}) == 403
    assert "c2" not in docker.containers
    assert helpers.dockers.journal.count(REF) == 0, "nothing refused is journaled"


async def test_a_start_revets_the_daemons_view(helpers: Helpers, docker: FakeDocker) -> None:
    """Review C1, in depth: at its start a container is vetted again as the daemon holds it."""
    helper = helpers.life()
    await _made(helpers, helper)
    start = "/v1.47/containers/c1/start"
    host = docker.containers["c1"]["HostConfig"]
    for key, value in (("Privileged", True), ("PidMode", "host"), ("CapAdd", ["ALL"]), ("MaskedPaths", [])):
        before, host[key] = host.get(key), value
        assert await _through_proxy(helpers.home, start) == 403, key
        host[key] = before
    docker.volumes["owners_pgdata"] = {"Labels": {}}
    docker.containers["c1"]["Mounts"] = [{"Type": "volume", "Name": "owners_pgdata", "Destination": "/v"}]
    assert await _through_proxy(helpers.home, start) == 403, "a volume the frame does not own"
    docker.containers["c1"]["Mounts"] = [{"Type": "bind", "Source": "/etc", "Destination": "/e", "RW": True}]
    assert await _through_proxy(helpers.home, start) == 403, "a bind outside the grants"
    assert not [p for _, p in docker.acted() if p.endswith("/start")]
    docker.containers["c1"]["Mounts"] = []
    assert await _through_proxy(helpers.home, start) == 204


async def test_volume_names_are_one_lock_across_frames(helpers: Helpers, docker: FakeDocker) -> None:
    """Review N2: no other frame's proxy makes a name between a lookup that found it absent and the
    create; and whichever frame loses a race for a name never removes the winner's."""
    helper = helpers.life()
    await _frame(helper, docker=True)
    await _frame(helper, docker=True, task="t2")
    names = helpers.dockers.volume_names
    await names.acquire()
    waiting = asyncio.create_task(_through_proxy(helpers.home, "/volumes/create", {"Name": "shared"}))
    await asyncio.sleep(0.1)
    assert not waiting.done() and "shared" not in docker.volumes
    names.release()
    assert await waiting == 201
    docker.slow_lookups = 0.05
    racing = [
        _through_proxy(helpers.home, "/volumes/create", {"Name": "raced"}, ref=ref)
        for ref in (REF, "device-t2")
    ]
    assert sorted(await asyncio.gather(*racing)) == [201, 403]
    assert ("DELETE", "/volumes/raced") not in docker.acted()


async def test_a_sleeps_pruning_waits_for_a_create(helpers: Helpers, docker: FakeDocker) -> None:
    """Review N3: a sleep lists and prunes under the frame's lock, so a create in flight is kept."""
    helper = helpers.life()
    await _made(helpers, helper)
    lock = helpers.dockers.lock(REF)
    await lock.acquire()  # a create in flight
    sleeping = asyncio.create_task(helper.post(f"/frames/{REF}/stop-services"))
    await asyncio.sleep(0.1)
    assert not sleeping.done(), "the sleep waits to list"
    docker.containers["c9"] = {"Labels": {FRAME_LABEL: REF}, "State": "running"}  # the create lands
    helpers.dockers.journal.record(REF, "containers", "c9")
    lock.release()
    assert (await sleeping).status_code == 204
    assert "c9" in helpers.dockers.journal.entry(REF)["containers"]


def test_a_prune_keeps_what_was_journaled_after_its_listing(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.record(REF, "containers", "gone")
    journal.record(REF, "containers", "kept")
    before = journal.entry(REF)
    journal.record(REF, "containers", "late")  # made while the listing was out
    journal.keep(REF, {"containers": {"kept"}}, before)
    assert journal.entry(REF)["containers"] == ["kept", "late"]


async def test_the_cap_counts_every_journaled_id(helpers: Helpers, docker: FakeDocker) -> None:
    """Review N4: the volumes a create makes for its mounts, and volumes adopted, count too."""
    helper = helpers.life()
    await _frame(helper, docker=True)
    ids = [f"{i:064x}" for i in range(CAP - 1)]
    for i in ids:
        docker.containers[i] = {"Labels": {FRAME_LABEL: REF}, "State": "exited"}
    path = helpers.home / "run" / "docker" / f"{REF}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"containers": ids, "networks": [], "volumes": []}))
    helpers.life()
    await helpers.life().put(f"/frames/{REF}/grants", json={"grants": [], "docker": True})
    two = {"Image": "alpine", "HostConfig": {"Binds": ["v1:/a", "v2:/b"]}}
    assert await _through_proxy(helpers.home, "/containers/create", two) == 403
    assert "v1" in docker.volumes and "v2" not in docker.volumes, "the one past the cap is removed again"
    assert helpers.dockers.journal.count(REF) == CAP
    docker.volumes["adoptable"] = {"Labels": {FRAME_LABEL: REF, TASK_LABEL: "t1"}}
    assert await _through_proxy(helpers.home, "/volumes/adoptable", method="GET") == 403
    assert helpers.dockers.journal.count(REF) == CAP


async def test_the_retry_timer_survives_and_asks_once_per_try(
    helpers: Helpers,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review N5: a failure to look for pending cleanups is logged and the timer goes on; with Docker
    away, it is looked for and asked once per try, not once per pending cleanup."""
    helpers.life()
    dockers = helpers.dockers
    for n in range(3):
        dockers.journal.record(f"device-gone{n}", "containers", "c1")
        dockers.journal.retire(f"device-gone{n}")
    looked: list[int] = []

    def nothing() -> None:
        looked.append(1)

    away = docker_proxy.DockerProxies(helpers.home, upstream=nothing)
    await away.retry_pending()
    assert looked == [1], "no Docker here: looked for once, not per pending cleanup"
    await docker.down()
    calls: list[str] = []
    real_call = docker_proxy.call

    async def counted(upstream: str, method: str, path: str, *a: Any, **k: Any) -> Any:
        calls.append(path)
        return await real_call(upstream, method, path, *a, **k)

    monkeypatch.setattr(docker_proxy, "call", counted)
    await dockers.retry_pending()
    assert calls == ["/_ping"], "Docker away: asked once"

    def broken(_: Journal) -> list[tuple[str, Path]]:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(docker_proxy, "_RETRY_EVERY_S", 0.01)
    monkeypatch.setattr(Journal, "pending", broken)
    dockers.start([])
    with caplog.at_level("ERROR"):
        dockers.kick()  # never raises into the command that kicked
        await asyncio.sleep(0.05)
    assert dockers._timer is not None and not dockers._timer.done(), "the timer lives on"  # pyright: ignore[reportPrivateUsage]
    assert any("pending Docker cleanups" in r.getMessage() for r in caplog.records)
