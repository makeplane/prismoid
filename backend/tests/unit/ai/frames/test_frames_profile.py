# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A frame carries its profile and attempt from construction, and `allow`
replaces the running frame's allowlist through the same control door `create` used."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import httpx
import pytest

from prismoid.ai import frames
from prismoid.ai.frames import docker_driver, egress, ensure_ready, new_frame, sandbar_driver
from prismoid.ai.frames.docker_driver import NETWORK, DockerFrame
from prismoid.ai.frames.driver import EgressUnavailable
from prismoid.ai.frames.image_browser.sidecar.ctl import START_S
from prismoid.ai.frames.sandbar_driver import SandbarFrame
from prismoid.conf import settings
from prismoid.schemas.machines import FrameInfo


def test_new_frame_threads_profile_and_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    frame = new_frame("t-s-3", profile="browser", attempt=3)
    assert isinstance(frame, DockerFrame)
    assert frame.profile == "browser" and frame.attempt == 3
    assert frame.recording_dir == "/artifacts/recording-3"
    monkeypatch.setattr(settings.worker, "frame_driver", "sandbar")
    sframe = new_frame("t-s-2", profile="browser", attempt=2)
    assert isinstance(sframe, SandbarFrame) and sframe.recording_dir == "/artifacts/recording-2"


async def test_docker_allow_reposts_the_frames_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        seen.append({"url": url, "json": json})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    frame = DockerFrame(task_id="t", profile="browser")
    frame.ip = "172.30.0.9"
    await frame.allow(["example.com", "accounts.google.com"])
    assert seen[0]["url"].endswith("/allow")
    assert seen[0]["json"] == {
        "ip": "172.30.0.9",
        "domains": ["example.com", "accounts.google.com"],
        "version": None,  # a driver that never synced a version (create or a pin) pushes unversioned
    }


async def test_docker_allow_raises_on_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    frame = DockerFrame(task_id="t", profile="browser")
    frame.ip = "172.30.0.9"
    with pytest.raises(RuntimeError, match="egress control refused"):
        await frame.allow(["example.com"])


_IP = "172.30.0.9"


def _proxy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Play the Docker-side egress proxy with its REAL control logic (`egress.apply_allow` /
    `egress.forget`), so each test asserts what the proxy ends up holding, not a script."""
    egress._ALLOW.clear()  # pyright: ignore[reportPrivateUsage]
    egress._VERSIONS.clear()  # pyright: ignore[reportPrivateUsage]
    calls: list[tuple[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        calls.append(("POST", json["version"]))
        status, body = egress.apply_allow(json["ip"], json["domains"], json["version"])
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    def fake_delete(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        ip = url.rsplit("/", 1)[-1]
        calls.append(("DELETE", ip))
        egress.forget(ip)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "delete", fake_delete)
    return calls


def _held() -> tuple[set[str], int | None]:
    return (
        egress._ALLOW.get(_IP, set()),  # pyright: ignore[reportPrivateUsage]
        egress._VERSIONS.get(_IP),  # pyright: ignore[reportPrivateUsage]
    )


async def test_an_unsynced_drivers_unpinned_push_applies_unversioned_and_leaves_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver found by `bind()` has not synced a version (0) while the proxy holds the row's.
    Its unpinned push (a resumed step's widen) goes unversioned: it applies, and the proxy keeps
    the row-authoritative version, so the next pinned boundary push at row+1 still lands."""
    calls = _proxy(monkeypatch)
    egress.apply_allow(_IP, ["old.example"], 4)
    frame = DockerFrame(task_id="t", profile="browser")
    frame.ip = _IP
    assert await frame.allow(["example.com"]) is True
    assert calls == [("POST", None)]
    assert _held() == ({"example.com"}, 4)
    assert frame.egress_version == 0 and frame.egress_hosts == ["example.com"]
    assert await frame.allow(["next.example"], 5) is True
    assert _held() == ({"next.example"}, 5) and frame.egress_version == 5


async def test_a_pinned_stale_push_returns_false_and_leaves_the_proxys_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _proxy(monkeypatch)
    egress.apply_allow(_IP, ["newer.example"], 4)
    frame = DockerFrame(task_id="t", profile="browser")
    frame.ip = _IP
    assert await frame.allow(["example.com"], 3) is False
    assert calls == [("POST", 3)]
    assert _held() == ({"newer.example"}, 4)
    # The refusal named the proxy's version, and the driver adopted it: only the
    # Machine's holder pushes again past it. The list it did not land stays off the driver.
    assert frame.egress_version == 4 and frame.egress_hosts == []


async def test_a_slow_synced_unpinned_push_never_overwrites_a_newer_pinned_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zombie widen from a driver synced at v2 arrives after a boundary pushed v4: it is
    refused (409 -> False), never retried past the held version - retrying would overwrite the
    newer list and inflate the proxy past the row, so the next boundary push would be stale."""
    calls = _proxy(monkeypatch)
    egress.apply_allow(_IP, ["boundary.example"], 4)
    frame = DockerFrame(task_id="t", profile="browser")
    frame.ip, frame.egress_version = _IP, 2
    assert await frame.allow(["late.example"]) is False
    assert calls == [("POST", 3)]
    assert _held() == ({"boundary.example"}, 4)


async def test_a_new_frame_forgets_its_ips_old_record_before_registering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker reuses IPs, and a frame removed without `destroy()` (the boot reap, an operator's
    `docker rm`, a crashed worker) leaves its list and version on the proxy. A frame's birth is
    a new principal: its registration starts from nothing, never 409s into the old list."""
    calls = _proxy(monkeypatch)
    egress.apply_allow(_IP, ["dead-frames.example"], 7)

    class _Container:
        attrs: ClassVar[dict[str, Any]] = {"NetworkSettings": {"Networks": {NETWORK: {"IPAddress": _IP}}}}

        def reload(self) -> None:
            pass

    frame = DockerFrame(task_id="t")
    frame._container = _Container()  # pyright: ignore[reportPrivateUsage]
    frame._register_egress(["example.com"], 1)  # pyright: ignore[reportPrivateUsage]
    assert calls == [("DELETE", _IP), ("POST", 1)]
    assert _held() == ({"example.com"}, 1)
    assert frame.egress_version == 1 and frame.egress_hosts == ["example.com"]


async def test_a_destroyed_driver_cannot_push_into_whoever_holds_its_ip_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destroy forgets the IP: a late `allow()` on a destroyed driver raises "create() first"
    instead of writing into the next frame Docker hands that address."""
    calls = _proxy(monkeypatch)

    class _Container:
        def remove(self, force: bool = False) -> None:
            pass

    frame = DockerFrame(task_id="t")
    frame._container = _Container()  # pyright: ignore[reportPrivateUsage]
    frame.ip, frame.egress_version, frame.egress_hosts = _IP, 3, ["example.com"]
    await frame.destroy()
    assert (frame.ip, frame.egress_version, frame.egress_hosts) == ("", 0, [])
    with pytest.raises(RuntimeError, match=r"create\(\) first"):
        await frame.allow(["late.example"])
    assert calls == [("DELETE", _IP)]


def test_inventory_tolerates_frames_removed_while_it_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """`containers.list` inspects each container it found; one destroyed mid-listing must not
    raise out of the reconciler's listing - it is simply no longer there."""
    from prismoid.ai.frames import docker_driver
    from prismoid.schemas.machines import LABEL_INSTANCE, LABEL_KIND

    seen: dict[str, Any] = {}

    class _Frame:
        name = "machine-x-1"
        status = "running"
        labels: ClassVar[dict[str, str]] = {LABEL_KIND: "machine", LABEL_INSTANCE: "i9"}
        attrs: ClassVar[dict[str, Any]] = {"Created": "2026-09-30T12:00:00.123456789Z"}

    class _Stopped(_Frame):  # a restart stopped it: listed, its files intact
        name = "machine-z-1"
        status = "exited"

    class _Vanished:
        name = "machine-y-1"
        labels: ClassVar[dict[str, str]] = {LABEL_KIND: "machine", LABEL_INSTANCE: "i9"}
        attrs: ClassVar[dict[str, Any]] = {}  # its inspect data went with it

    class _Containers:
        def list(self, **kw: Any) -> list[Any]:
            seen.update(kw)
            return [_Frame(), _Vanished(), _Stopped()]

    class _Client:
        containers = _Containers()

    monkeypatch.setattr(docker_driver, "_client", lambda: _Client())
    monkeypatch.setattr(settings.worker, "instance", "i9")
    assert [f.ref for f in docker_driver.inventory()] == ["machine-x-1", "machine-z-1"]
    assert {f.driver for f in docker_driver.inventory()} == {"docker"}
    assert seen == {"all": True, "ignore_removed": True}


class _Container:
    """One container as the Docker SDK shows it: a status, and what `start`/`unpause` do to it."""

    def __init__(self, status: str, *, comes_up: str = "running", fails: Exception | None = None) -> None:
        self.status, self._comes_up, self._fails = status, comes_up, fails
        self.calls: list[str] = []
        self.attrs: dict[str, Any] = {"NetworkSettings": {"Networks": {NETWORK: {"IPAddress": "10.9.0.7"}}}}

    def start(self) -> None:
        self.calls.append("start")
        if self._fails is not None:
            raise self._fails

    def unpause(self) -> None:
        self.calls.append("unpause")

    def reload(self) -> None:
        self.status = self._comes_up


def _docker_with(monkeypatch: pytest.MonkeyPatch, found: _Container | Exception) -> list[str]:
    """The driver's Engine answers `get` with `found`; returns the addresses `bind` forgot."""
    from types import SimpleNamespace

    from prismoid.ai.frames import docker_driver

    def get(name: str) -> _Container:
        if isinstance(found, Exception):
            raise found
        return found

    forgot: list[str] = []
    monkeypatch.setattr(
        docker_driver, "_client", lambda: SimpleNamespace(containers=SimpleNamespace(get=get))
    )
    monkeypatch.setattr(DockerFrame, "_forget_egress", lambda self: forgot.append(self.ip))
    return forgot


async def test_bind_starts_a_stopped_container_and_forgets_its_addresss_old_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart stopped it, its writable layer intact: bind starts it (unpauses a paused one),
    says so, and forgets what the proxy holds for its address - it reaches nothing until pushed."""
    for status, verb in (("exited", "start"), ("created", "start"), ("paused", "unpause")):
        container = _Container(status)
        forgot = _docker_with(monkeypatch, container)
        frame = DockerFrame(task_id="t", name="machine-x-1")
        assert await frame.bind() is True
        assert container.calls == [verb] and frame.ip == "10.9.0.7" and forgot == ["10.9.0.7"]
    running = _Container("running")
    forgot = _docker_with(monkeypatch, running)
    assert await DockerFrame(task_id="t", name="machine-x-1").bind() is False
    assert running.calls == [] and forgot == []


async def test_bind_reads_gone_only_where_the_frame_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RuntimeError` is "gone": the caller rebuilds from the checkpoint and the reconciler destroys
    the container. So it is raised only for a frame that cannot run - missing, dead, exiting as it
    starts, or stopped where the caller asked for no start - never for an Engine hiccup."""
    from docker.errors import APIError, NotFound

    for found in (
        NotFound("no such container"),
        _Container("dead"),
        _Container("removing"),
        _Container("exited", fails=NotFound("removed as it started")),
        _Container("exited", comes_up="exited"),
    ):
        _docker_with(monkeypatch, found)
        with pytest.raises(RuntimeError):
            await DockerFrame(task_id="t", name="machine-x-1").bind()
    stopped = _Container("exited")
    _docker_with(monkeypatch, stopped)
    with pytest.raises(RuntimeError, match="not running"):
        await DockerFrame(task_id="t", name="machine-x-1").bind(start=False)
    assert stopped.calls == [], "a caller that only narrows or thaws never starts it"
    _docker_with(monkeypatch, _Container("exited", fails=APIError("500 Server Error")))
    with pytest.raises(APIError) as hiccup:
        await DockerFrame(task_id="t", name="machine-x-1").bind()
    assert not isinstance(hiccup.value, RuntimeError), "an Engine error is one infra attempt"


async def test_sandbar_allow_puts_the_egress_route(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_request(self: SandbarFrame, method: str, path: str, **kw: Any) -> httpx.Response:
        calls.append((method, path, kw.get("json") or {}))
        return httpx.Response(200, request=httpx.Request(method, "http://sandbar" + path))

    monkeypatch.setattr(SandbarFrame, "_request", fake_request)
    frame = SandbarFrame(task_id="t", profile="browser")
    await frame.allow(["example.com"], 1)
    assert calls == [
        ("PUT", f"/v1/sandboxes/{frame.sandbox_id}/egress", {"allow_domains": ["example.com"], "version": 1})
    ]


async def test_sandbar_allow_reports_a_gone_frame_and_the_daemons_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_404(self: SandbarFrame, method: str, path: str, **kw: Any) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request(method, "http://sandbar" + path))

    monkeypatch.setattr(SandbarFrame, "_request", fake_request_404)
    frame = SandbarFrame(task_id="t", profile="browser")
    with pytest.raises(RuntimeError, match=f"sandbar frame {frame.sandbox_id} is gone"):
        await frame.allow(["example.com"])

    async def fake_request_400(self: SandbarFrame, method: str, path: str, **kw: Any) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "this sandbox has no egress"},
            request=httpx.Request(method, "http://sandbar" + path),
        )

    monkeypatch.setattr(SandbarFrame, "_request", fake_request_400)
    frame = SandbarFrame(task_id="t", profile="browser")
    with pytest.raises(EgressUnavailable, match="this sandbox has no egress"):
        await frame.allow(["example.com"])


async def test_a_browser_command_waits_the_configured_timeout_on_both_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PRISMOID_COMPUTER_USE_COMMAND_TIMEOUT_S` is the one knob for how long a browser command
    may take - on sandbar it rides the exec request, on Docker it bounds the sidecar call."""
    from prismoid.ai.frames import docker_driver
    from prismoid.conf import settings

    monkeypatch.setattr(settings.computer_use, "command_timeout_s", 7.5)
    sent: list[dict[str, Any]] = []

    async def fake_post(path: str, **kw: Any) -> httpx.Response:
        sent.append(kw["json"])
        return httpx.Response(
            200, json={"exit_code": 0, "stdout": '{"ok": true}\n'}, request=httpx.Request("POST", path)
        )

    sandbar = SandbarFrame(task_id="t", profile="browser")
    monkeypatch.setattr(sandbar._client, "post", fake_post)  # pyright: ignore[reportPrivateUsage]
    assert await sandbar.browser({"verb": "snapshot"}) == {"ok": True}
    assert sent[0]["timeout_s"] == 7.5 + START_S  # a call may cold-start the sidecar first

    waited: list[float] = []

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:  # noqa: ASYNC109
        waited.append(timeout)
        return await awaitable

    monkeypatch.setattr(docker_driver.asyncio, "wait_for", fake_wait_for)
    docker = DockerFrame(task_id="t", profile="browser")
    monkeypatch.setattr(docker, "_browser_sync", lambda command: {"ok": True})
    assert await docker.browser({"verb": "snapshot"}) == {"ok": True}
    assert waited == [7.5 + START_S + 5]


def test_new_frame_takes_the_machines_driver_over_the_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing Machine's frame is its row's driver's; None is this worker's."""
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    assert isinstance(new_frame("t", driver="sandbar", name="machine-m-1"), SandbarFrame)
    assert isinstance(new_frame("t", driver=None), DockerFrame)
    monkeypatch.setattr(settings.worker, "frame_driver", "sandbar")
    assert isinstance(new_frame("t", driver="docker"), DockerFrame)
    with pytest.raises(ValueError, match="unknown frame_driver 'e2b'"):
        new_frame("t", driver="e2b")


def test_each_driver_names_itself() -> None:
    assert (DockerFrame.driver, SandbarFrame.driver) == ("docker", "sandbar")


def test_ensure_ready_builds_an_image_only_for_a_docker_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    def ensure_image(profile: str = "base") -> str:
        built.append(profile)
        return "prismoid-frame:test"

    monkeypatch.setattr(docker_driver, "ensure_image", ensure_image)
    monkeypatch.setattr(settings.worker, "frame_driver", "sandbar")
    ensure_ready("browser")  # this worker's driver is sandbar: its rootfs is built out of band
    ensure_ready("browser", "docker")  # a Docker frame's
    assert built == ["browser"]


async def test_inventory_asks_every_named_driver_and_stamps_each_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciler lists every driver a row names a frame on, not only this worker's,
    and the old fallback to Docker for any other name is gone."""
    at = dt.datetime(2026, 10, 13, tzinfo=dt.UTC)

    async def sandbar_listing() -> list[FrameInfo]:
        return [FrameInfo("machine-b-1", {}, at, driver="sandbar")]

    monkeypatch.setattr(
        docker_driver, "inventory", lambda: [FrameInfo("machine-a-1", {}, at, driver="docker")]
    )
    monkeypatch.setattr(sandbar_driver, "inventory", sandbar_listing)
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    assert [f.ref for f in await frames.inventory()] == ["machine-a-1"]
    both = await frames.inventory(["sandbar", "docker"])
    assert [(f.ref, f.driver) for f in both] == [("machine-a-1", "docker"), ("machine-b-1", "sandbar")]
    with pytest.raises(ValueError, match="unknown frame_driver 'e2b'"):
        await frames.inventory(["e2b"])
    monkeypatch.setattr(settings.worker, "frame_driver", "e2b")
    with pytest.raises(ValueError, match="unknown frame_driver 'e2b'"):
        await frames.inventory()  # an unknown configured driver no longer silently lists Docker


async def test_a_listing_is_per_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing is per driver. When one driver failing failed the reconciler's whole listing, a
    single row on a driver that cannot answer stalled every healthy driver's hygiene. Asked
    alone, Docker answers while sandbar is down, and only sandbar's own listing fails. One call
    still never hands back a partial list - which is why the reconciler asks each driver alone."""
    at = dt.datetime(2026, 10, 13, tzinfo=dt.UTC)

    async def broken() -> list[FrameInfo]:
        raise ConnectionError("sandbar is down")

    monkeypatch.setattr(
        docker_driver, "inventory", lambda: [FrameInfo("machine-a-1", {}, at, driver="docker")]
    )
    monkeypatch.setattr(sandbar_driver, "inventory", broken)
    assert [f.ref for f in await frames.inventory(["docker"])] == ["machine-a-1"]
    with pytest.raises(ConnectionError):
        await frames.inventory(["sandbar"])
    with pytest.raises(ConnectionError):
        await frames.inventory(["docker", "sandbar"])


async def test_destroy_ref_goes_to_the_driver_that_holds_the_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    gone: list[tuple[str, str]] = []

    async def destroy(self: Any) -> None:
        gone.append((type(self).driver, self.ref))

    monkeypatch.setattr(DockerFrame, "destroy", destroy)
    monkeypatch.setattr(SandbarFrame, "destroy", destroy)
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    await frames.destroy_ref("machine-m-2", driver="sandbar")
    await frames.destroy_ref("machine-m-1")
    assert gone == [("sandbar", "machine-m-2"), ("docker", "machine-m-1")]
