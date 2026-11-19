# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`SandbarFrame` over a played daemon - no live sandbar in dev, so this plays it with
`httpx.MockTransport` the same way `test_fetch_tool.py` plays the web. What these pin: the
Machine's verbs - `create` sends labels, resources and the egress version; `allow` is
versioned and drops a stale push; `archive`/`unpack` move one directory through the `archives`
filters (whose own rules are tested in `test_frame_archives.py`); `inventory` shows only this
instance's frames - and `browser()` refuses honestly on a non-browser profile and never crashes
on a sidecar timeout/garbage reply.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from prismoid.ai.frames import FrameUnreachable, sandbar_driver
from prismoid.ai.frames.driver import EgressUnavailable
from prismoid.ai.frames.layout import ARTIFACTS, SKILLS_RESET
from prismoid.ai.frames.sandbar_driver import SandbarFrame
from prismoid.conf import settings
from prismoid.errors import PolicyDenied
from prismoid.schemas.machines import LABEL_INSTANCE, LABEL_KIND


@dataclass
class Recorder:
    """Plays sandbard: records every request it saw and returns a scripted response."""

    handler: Any
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _frame(profile: str, handler: Any) -> tuple[SandbarFrame, Recorder]:
    frame = SandbarFrame(task_id="t1", profile=profile)
    rec = Recorder(handler)
    frame._client = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage] - the one test seam
        transport=httpx.MockTransport(rec),
        base_url=frame._client.base_url,  # pyright: ignore[reportPrivateUsage]
    )
    return frame, rec


# ── create() picks the image by profile ──────────────────────────────────────────────────────


async def test_create_raises_a_clear_error_when_the_browser_image_setting_is_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.worker, "sandbar_browser_image", "")
    frame, rec = _frame("browser", lambda r: httpx.Response(500))
    with pytest.raises(RuntimeError, match="PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE"):
        await frame.create()
    assert rec.requests == []  # refused before any HTTP call


async def test_create_uses_the_configured_browser_image_tag_for_a_browser_profile(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.worker, "sandbar_browser_image", "prismoid-frame-browser:test")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "frame-t1"})

    frame, _ = _frame("browser", handler)
    await frame.create()
    assert seen["body"]["spec"]["image"] == "prismoid-frame-browser:test"


async def test_create_leaves_the_base_image_alone_for_the_base_profile(monkeypatch: pytest.MonkeyPatch):
    """A base-profile frame never even looks at the (possibly unset) browser image setting."""
    monkeypatch.setattr(settings.worker, "sandbar_browser_image", "")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "frame-t1"})

    frame, _ = _frame("base", handler)
    await frame.create()
    assert seen["body"]["spec"]["image"] == settings.worker.sandbar_image


# ── the Machine's verbs ──────────────────────────────────────────────────────────────────────


async def test_create_sends_labels_resources_and_the_egress_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "instance", "")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "machine-m-2"})

    frame, _ = _frame("base", handler)
    await frame.create(allow_domains=[], labels={"plane.kind": "machine"}, egress_version=4)
    assert seen["spec"]["labels"] == {"plane.kind": "machine", LABEL_INSTANCE: ""}
    assert seen["spec"]["cpus"] == 2 and seen["spec"]["mem_mb"] == 2048
    assert seen["egress_version"] == 4
    assert frame.egress_version == 4


async def test_create_stamps_the_dev_instance_like_the_docker_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instance rides on every labelled frame, and on every frame of a named instance, so
    `inventory()` shows each worker only its own. A default-instance throwaway stays unlabelled."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={})

    monkeypatch.setattr(settings.worker, "instance", "")
    frame, _ = _frame("base", handler)
    await frame.create()
    monkeypatch.setattr(settings.worker, "instance", "i7")
    await frame.create()
    assert [b["spec"]["labels"] for b in bodies] == [{}, {LABEL_INSTANCE: "i7"}]


async def test_allow_sends_the_version_and_treats_409_as_a_dropped_push() -> None:
    rec_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rec_bodies.append(body)
        return httpx.Response(
            409 if body["version"] == 5 else 200, json={"ok": True, "version": body["version"]}
        )

    frame, rec = _frame("base", handler)
    frame.egress_version = 4
    assert await frame.allow(["a.example"], 5) is False
    assert frame.egress_version == 4 and frame.egress_hosts == []  # a refused push changes nothing
    assert await frame.allow(["a.example"], 6) is True and frame.egress_hosts == ["a.example"]
    assert await frame.allow(["b.example"], 6) is False  # not newer: dropped before any request
    assert len(rec.requests) == 2  # and the 409 was never retried
    assert await frame.allow(["c.example"]) is True  # unpinned and synced: one past v6
    assert rec_bodies[-1] == {"allow_domains": ["c.example"], "version": 7}
    assert frame.egress_version == 7


async def test_a_stale_refusal_adopts_the_version_sandbar_holds() -> None:
    """Sandbar's 409 names the version it holds only in its message
    (`core.ErrStale`'s text). The driver reads it and adopts it, so its owner's next unpinned
    push lands one past it; a message that does not say adopts nothing."""
    stale = {"error": {"message": "stale: egress version 3 is not newer than 7"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("version", 0) <= 7:
            return httpx.Response(409, json=stale)
        return httpx.Response(200, json={"ok": True, "version": body["version"]})

    frame, rec = _frame("base", handler)
    frame.egress_version = 2
    assert await frame.allow(["a.example"]) is False
    assert frame.egress_version == 7 and frame.egress_hosts == []
    assert await frame.allow(["a.example"]) is True and frame.egress_version == 8
    assert json.loads(rec.requests[-1].content) == {"allow_domains": ["a.example"], "version": 8}
    mute, _ = _frame("base", lambda request: httpx.Response(409, json={"error": {"message": "stale"}}))
    mute.egress_version = 2
    assert await mute.allow(["a.example"]) is False and mute.egress_version == 2


async def test_a_stale_refusal_reads_the_held_version_from_json() -> None:
    """Gate 3: sandbar's 409 carries the version it holds as JSON (`error.version`); the
    driver reads that, never the message - here the message says nothing at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("version", 0) <= 7:
            return httpx.Response(409, json={"error": {"message": "stale", "code": "stale", "version": 7}})
        return httpx.Response(200, json={"ok": True, "version": body["version"]})

    frame, _ = _frame("base", handler)
    frame.egress_version = 2
    assert await frame.allow(["a.example"]) is False and frame.egress_version == 7
    assert await frame.allow(["a.example"]) is True and frame.egress_version == 8


@pytest.mark.parametrize(
    ("error", "held"),
    [
        ({"message": "stale: egress version 3 is not newer than 9", "version": 7}, 7),  # JSON wins
        (
            {"message": "stale: egress version 3 is not newer than 9", "version": True},
            9,
        ),  # a bool is not a version
        ({"message": "stale: egress version 3 is not newer than 9"}, 9),  # an older sandbar: the text
        ({"message": "stale"}, 0),  # neither: adopt nothing
    ],
)
async def test_the_held_version_prefers_json_and_tolerates_the_old_text(
    error: dict[str, Any], held: int
) -> None:
    frame, _ = _frame("base", lambda request: httpx.Response(409, json={"error": error}))
    frame.egress_version = 1  # synced, so the push goes out versioned (v2) and meets the 409
    assert await frame.allow(["a.example"], version=2) is False
    assert frame.egress_version == max(1, held)  # adopted when sandbar names one, unchanged when not


async def test_an_unsynced_drivers_unpinned_push_goes_unversioned() -> None:
    """A driver found by `bind()` has synced no version (0) while the daemon holds the row's. Its
    unpinned push (a resumed step's widen) omits the version - sandbar's unversioned push, which
    applies and keeps the recorded version - so the row's version stays the truth."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "version": 4})

    frame, _ = _frame("browser", handler)
    assert await frame.allow(["example.com"]) is True
    assert bodies == [{"allow_domains": ["example.com"]}]
    assert frame.egress_version == 0 and frame.egress_hosts == ["example.com"]
    assert await frame.allow(["next.example"], 5) is True
    assert bodies[-1] == {"allow_domains": ["next.example"], "version": 5} and frame.egress_version == 5


async def test_allow_on_a_sandbox_born_without_egress_raises_egress_unavailable() -> None:
    """sandbar keeps a sandbox born with no domains airtight (network=none, no principal), so its
    egress route answers 400 and no push can ever widen it: the Machine must be re-created with
    the list. Both push shapes say so, and neither touches the driver's synced state."""
    no_egress = {
        "error": {
            "message": "invalid spec: this sandbox has no egress (it was created without allow_domains)"
        }
    }
    frame, rec = _frame("base", lambda request: httpx.Response(400, json=no_egress))
    with pytest.raises(EgressUnavailable, match="no egress"):
        await frame.allow(["a.example"])  # unsynced: the unversioned push
    frame.egress_version = 2
    with pytest.raises(EgressUnavailable, match="no egress"):
        await frame.allow(["a.example"])  # synced: v3
    assert (frame.egress_version, frame.egress_hosts) == (2, [])
    assert len(rec.requests) == 2


async def test_allow_keeps_a_gone_or_failing_daemon_a_plain_runtime_error() -> None:
    """Only the no-principal refusal is `EgressUnavailable`: a vanished sandbox and a daemon fault
    stay the plain `RuntimeError`s the resume ladder already handles."""
    for status, match in ((404, "is gone"), (502, "sandbar egress 502")):
        frame, _ = _frame("base", lambda request, status=status: httpx.Response(status, json={}))
        with pytest.raises(RuntimeError, match=match) as raised:
            await frame.allow(["a.example"])
        assert not isinstance(raised.value, EgressUnavailable)


async def test_a_destroyed_driver_forgets_its_egress_and_cannot_push_again() -> None:
    """Sandbar ids are deterministic (`machine-<id>-<epoch>`), so a late `allow()` on a destroyed
    driver must fail, never write into a sandbox recreated under the same id."""
    frame, rec = _frame("base", lambda request: httpx.Response(200, json={"destroyed": "x"}))
    frame.egress_version, frame.egress_hosts = 3, ["example.com"]
    await frame.destroy()
    assert (frame.egress_version, frame.egress_hosts) == (0, [])
    with pytest.raises(RuntimeError):
        await frame.allow(["late.example"])
    assert [r.method for r in rec.requests] == ["DELETE"]


async def test_freeze_and_thaw_signal_every_agent_process() -> None:
    commands: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        commands.append(json.loads(request.content)["command"])
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})

    frame, _ = _frame("base", handler)
    await frame.freeze()
    await frame.thaw()
    assert commands == ["kill -STOP -1 2>/dev/null; true", "kill -CONT -1 2>/dev/null; true"]


async def test_archive_streams_the_browser_through_the_checkpoint_filter() -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in ("browser/profile/Default/Cookies", "browser/profile/SingletonLock"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))

    frame, rec = _frame("browser", lambda request: httpx.Response(200, content=raw.getvalue()))
    out = io.BytesIO()
    await frame.archive("/browser", out)
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r") as tar:
        assert tar.getnames() == ["browser/profile/Default/Cookies"]
    assert rec.requests[0].method == "GET"
    assert rec.requests[0].url.path == f"/v1/sandboxes/{frame.sandbox_id}/archive"
    assert rec.requests[0].url.params.get_list("path") == ["/browser"]


async def test_archive_raises_on_a_daemon_refusal_with_its_reason() -> None:
    frame, _ = _frame(
        "base", lambda request: httpx.Response(502, json={"error": {"message": "docker 500: boom"}})
    )
    out = io.BytesIO()
    with pytest.raises(RuntimeError, match="sandbar archive 502: docker 500: boom"):
        await frame.archive("/work", out)
    assert out.getvalue() == b""


class _BrokenBody(httpx.AsyncByteStream):
    """What sandbar sends when the backend fails after the 200: part of a tar, then an aborted
    connection."""

    def __init__(self, head: bytes) -> None:
        self.head = head

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.head
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


async def test_archive_raises_on_a_broken_stream_and_writes_nothing() -> None:
    """A body cut short must never become a checkpoint: `tarfile` reads a tar that stops at an
    entry boundary as a clean end, so the aborted connection is the only signal - it must raise,
    and `out` must stay empty (the rewrite runs only on a whole body)."""
    whole = io.BytesIO()
    with tarfile.open(fileobj=whole, mode="w") as tar:
        info = tarfile.TarInfo("work/notes.md")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    head = whole.getvalue()[:1024]  # the entry, without the trailer
    frame, _ = _frame("base", lambda request: httpx.Response(200, stream=_BrokenBody(head)))
    out = io.BytesIO()
    with pytest.raises(RuntimeError, match="sandbar archive of /work failed: RemoteProtocolError"):
        await frame.archive("/work", out)
    assert out.getvalue() == b""


class _CountedBody(httpx.AsyncByteStream):
    """A body served in fixed chunks, counting what the client pulled."""

    def __init__(self, body: bytes, chunk: int) -> None:
        self.body, self.chunk, self.pulled = body, chunk, 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), self.chunk):
            piece = self.body[start : start + self.chunk]
            self.pulled += len(piece)
            yield piece


async def test_an_archive_over_the_cap_stops_reading_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive spools the whole body before it is rewritten, so the cap must
    count while it spools - a 6 MiB `/work` under a 1 MiB cap is abandoned within one chunk of the
    cap, never read whole to a worker temp file first. The store reads it as `Oversize`."""
    from prismoid import storage
    from prismoid.operations.machines import store

    monkeypatch.setattr(storage, "put_file", lambda *a, **k: pytest.fail("nothing over the cap is uploaded"))
    big = io.BytesIO()
    with tarfile.open(fileobj=big, mode="w") as tar:
        info = tarfile.TarInfo("work/big.bin")
        info.size = 6 * 1024 * 1024
        tar.addfile(info, io.BytesIO(bytes(info.size)))
    chunk, cap = 64 * 1024, 1024 * 1024
    served = _CountedBody(big.getvalue(), chunk)
    frame, _ = _frame("base", lambda request: httpx.Response(200, stream=served))
    with pytest.raises(store.Oversize) as over:
        await store.save_work(frame, "machines/m/1.tar.gz", cap)
    assert served.pulled <= cap + chunk, f"read {served.pulled} bytes past a {cap}-byte cap"
    assert over.value.seen > cap


async def test_unpack_posts_the_restore_filtered_tar() -> None:
    """`unpack` hands the daemon only what `restore_filter` keeps, stamped as the agent: the
    escape through an earlier symlink never reaches the POST."""
    src = io.BytesIO()
    with tarfile.open(fileobj=src, mode="w") as tar:
        link = tarfile.TarInfo("work/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc"
        tar.addfile(link)
        for name in ("work/notes.md", "work/evil/cron"):
            info = tarfile.TarInfo(name)
            info.size, info.uid = 2, 0
            tar.addfile(info, io.BytesIO(b"ok"))
    src.seek(0)
    posted: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.content)
        return httpx.Response(200, json={"extracted": "x"})

    frame, rec = _frame("base", handler)
    await frame.unpack("/work", src)
    assert rec.requests[0].method == "POST"
    assert rec.requests[0].url.path == f"/v1/sandboxes/{frame.sandbox_id}/archive"
    with tarfile.open(fileobj=io.BytesIO(posted[0]), mode="r") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert sorted(members) == ["work/evil", "work/notes.md"]
    assert members["work/notes.md"].uid == 1000


async def test_unpack_raises_with_the_daemons_reason() -> None:
    frame, _ = _frame("base", lambda request: httpx.Response(502, json={"error": {"message": "disk full"}}))
    with pytest.raises(RuntimeError, match="sandbar extract 502: disk full"):
        await frame.unpack("/work", io.BytesIO(_empty_tar()))


def _empty_tar() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w"):
        pass
    return out.getvalue()


async def test_reset_mounts_takes_roots_mounts_away_and_clears_the_skills() -> None:
    """The Docker driver's semantics on sandbar: what an earlier step left
    under /task - every entry of /task/inputs, files and directories, and the instruction files - is
    removed by sandbar's remove route, as root, so no name stays in the agent's view; the step
    marker stays (the boundary rewrites it), and /skills, the agent's, is cleared by an exec."""
    listings = {"/task": ["inputs/", "context.md", ".step-start"], "/task/inputs": ["01-a-final.md", "sub/"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dir"):
            return httpx.Response(200, json={"entries": listings[request.url.params["path"]]})
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
        return httpx.Response(200, json={"removed": 3})

    frame, rec = _frame("base", handler)
    await frame.reset_mounts()
    removed = next(r for r in rec.requests if r.url.path.endswith("/remove"))
    assert removed.url.path == f"/v1/sandboxes/{frame.sandbox_id}/remove"
    assert json.loads(removed.content) == {
        "paths": ["/task/inputs/01-a-final.md", "/task/inputs/sub", "/task/context.md"]
    }
    assert not any(r.url.path.endswith("/archive") for r in rec.requests), "nothing is emptied in place"
    run = next(r for r in rec.requests if r.url.path.endswith("/exec"))
    assert json.loads(run.content)["command"] == SKILLS_RESET


async def test_reset_mounts_says_why_sandbar_kept_a_mount() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dir"):
            return httpx.Response(
                200, json={"entries": ["context.md"] if "inputs" not in request.url.params["path"] else []}
            )
        return httpx.Response(503, json={"error": {"message": "remove: Permission denied"}})

    frame, _ = _frame("base", handler)
    with pytest.raises(RuntimeError, match="Permission denied"):
        await frame.reset_mounts()


async def test_inventory_lists_labelled_and_legacy_sandboxes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "instance", "")
    items = [
        {
            "id": "machine-a-1",
            "spec": {"labels": {"plane.kind": "machine"}},
            "created_at": "2026-09-30T12:00:00.123456789Z",
        },
        {"id": "frame-t-s-1", "spec": {}, "created_at": "2026-09-30T12:00:00Z"},
        {"id": "someone-else", "spec": {}, "created_at": "2026-09-30T12:00:00Z"},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": items}))
    monkeypatch.setattr(
        sandbar_driver,
        "_control_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://sandbar"),
    )
    found = await sandbar_driver.inventory()
    assert [f.ref for f in found] == ["machine-a-1", "frame-t-s-1"]
    assert {f.driver for f in found} == {"sandbar"}
    assert found[0].created_at.tzinfo is not None


async def test_inventory_on_a_named_instance_sees_only_its_own_labelled_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another dev instance's Machines, the default instance's, and every unlabelled legacy frame
    are invisible to a named instance - its reconciler never destroys what it does not own."""
    monkeypatch.setattr(settings.worker, "instance", "i1")
    stamp = "2026-09-30T12:00:00Z"
    items = [
        {
            "id": "machine-a-1",
            "spec": {"labels": {LABEL_KIND: "machine", LABEL_INSTANCE: "i1"}},
            "created_at": stamp,
        },
        {
            "id": "machine-b-1",
            "spec": {"labels": {LABEL_KIND: "machine", LABEL_INSTANCE: "i2"}},
            "created_at": stamp,
        },
        {"id": "machine-c-1", "spec": {"labels": {LABEL_KIND: "machine"}}, "created_at": stamp},
        {"id": "frame-t-s-1", "spec": {}, "created_at": stamp},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": items}))
    monkeypatch.setattr(
        sandbar_driver,
        "_control_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://sandbar"),
    )
    assert [f.ref for f in await sandbar_driver.inventory()] == ["machine-a-1"]


# ── browser() ─────────────────────────────────────────────────────────────────────────────────


async def test_browser_refuses_honestly_off_the_browser_profile_with_no_network_call():
    frame, rec = _frame("base", lambda r: httpx.Response(500))
    result = await frame.browser({"verb": "ping"})
    assert result == {"ok": False, "error": "no browser in this frame"}
    assert rec.requests == []


async def test_browser_posts_as_the_browser_user_with_the_command_in_its_own_env():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"exit_code": 0, "stdout": '{"ok": true}\n', "stderr": ""})

    frame, rec = _frame("browser", handler)
    result = await frame.browser({"verb": "screenshot"})
    assert result == {"ok": True}
    assert len(rec.requests) == 1
    assert seen["body"]["user"] == "browser"
    assert json.loads(seen["body"]["env"]["SIDECAR_CMD"]) == {"verb": "screenshot"}
    assert seen["body"]["env"]["SIDECAR_RECORDING"] == f"{ARTIFACTS}/recording-1"
    assert seen["body"]["env"]["SIDECAR_SNAPSHOT_CHARS"] == str(settings.worker.browser_snapshot_chars)
    assert "cd /home/browser" in seen["body"]["command"]


async def test_browser_reports_an_http_failure_as_a_result_with_no_url_or_body_leaked():
    base_url = str(SandbarFrame(task_id="t1")._client.base_url)  # pyright: ignore[reportPrivateUsage]
    frame, _ = _frame(
        "browser", lambda r: httpx.Response(400, json={"error": {"message": "some internal detail"}})
    )
    result = await frame.browser({"verb": "ping"})
    assert result == {"ok": False, "error": "sidecar exec failed: HTTP 400"}
    assert base_url not in result["error"]
    assert "some internal detail" not in result["error"]


async def test_browser_reports_an_unparseable_reply_as_a_result_not_an_exception():
    frame, _ = _frame(
        "browser", lambda r: httpx.Response(200, json={"exit_code": 1, "stdout": "not json", "stderr": ""})
    )
    result = await frame.browser({"verb": "ping"})
    assert result["ok"] is False
    assert "sidecar exec failed" in result["error"]


async def test_browser_reports_a_non_json_200_body_as_a_result_not_an_exception():
    frame, _ = _frame("browser", lambda r: httpx.Response(200, content=b"not json at all"))
    result = await frame.browser({"verb": "ping"})
    assert result["ok"] is False
    assert "sidecar exec failed" in result["error"]


async def test_browser_reports_a_non_dict_200_body_as_a_result_not_an_exception():
    frame, _ = _frame("browser", lambda r: httpx.Response(200, json=["not", "a", "dict"]))
    result = await frame.browser({"verb": "ping"})
    assert result == {"ok": False, "error": "sidecar exec failed: response was not an object"}


async def test_browser_after_destroy_is_a_result_not_a_runtime_error():
    """`destroy()` closes `self._client`; a call after that raises `RuntimeError` from httpx, not
    an `httpx.HTTPError` - the broad `except Exception` is what catches this, not the narrower
    exception type the original code checked."""
    frame, rec = _frame("browser", lambda r: httpx.Response(200, json={"exit_code": 0, "stdout": "{}"}))
    await frame._client.aclose()  # pyright: ignore[reportPrivateUsage] - simulate the closed-client state destroy() leaves
    result = await frame.browser({"verb": "ping"})
    assert result["ok"] is False
    assert "sidecar exec failed" in result["error"]
    assert rec.requests == []


async def test_bind_on_a_missing_sandbox_is_a_gone_frame() -> None:
    """Gate 2: the record outlived its container - the lifecycle wakes the Machine from
    its checkpoint instead of failing every step as infra."""
    frame, _ = _frame("base", lambda request: httpx.Response(200, json={"id": "x", "state": "missing"}))
    with pytest.raises(RuntimeError, match="missing"):
        await frame.bind()


def _stopped(start: httpx.Response) -> Any:
    """sandbard holding a stopped sandbox: its record says so, and its start answers `start`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/start"):
            return start
        return httpx.Response(200, json={"id": "frame-t1", "state": "stopped"})

    return handler


async def test_bind_starts_a_stopped_sandbox_and_binds_to_it() -> None:
    """A stopped sandbox still holds /work (a container's writable layer survives `docker stop`):
    bind starts it and binds, so a Machine never loses the work since its last checkpoint to a
    stop - only its processes are new."""
    frame, rec = _frame("base", _stopped(httpx.Response(200, json={"id": "frame-t1", "state": "running"})))
    assert await frame.bind() is True  # the caller hears its processes, a page among them, went
    assert [(r.method, r.url.path) for r in rec.requests] == [
        ("GET", "/v1/sandboxes/frame-t1"),
        ("POST", "/v1/sandboxes/frame-t1/start"),
    ]


async def test_bind_without_start_never_starts_a_stopped_sandbox() -> None:
    """`start=False` (a cancelled park's narrowing, the reconciler's narrow-and-thaw) binds only a
    running frame: a stopped one reaches nothing and has nothing frozen, and starting it there would
    race the next step's own start."""
    frame, rec = _frame("base", _stopped(httpx.Response(200, json={"id": "frame-t1", "state": "running"})))
    with pytest.raises(RuntimeError, match="stopped"):
        await frame.bind(start=False)
    assert [r.method for r in rec.requests] == ["GET"]


async def test_bind_on_a_stopped_sandbox_whose_container_went_is_a_gone_frame() -> None:
    frame, _ = _frame("base", _stopped(httpx.Response(404, json={"error": {"code": "not_found"}})))
    with pytest.raises(RuntimeError, match="gone"):
        await frame.bind()


async def test_bind_on_a_stopped_sandbox_that_will_not_start_is_an_infra_error() -> None:
    """A start the Engine refused is not a gone frame: one infra attempt, the files kept."""
    frame, _ = _frame("base", _stopped(httpx.Response(503, json={"error": {"message": "start: boom"}})))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await frame.bind()
    assert not isinstance(raised.value, RuntimeError)


async def test_bind_on_an_unreadable_state_is_an_infra_error_never_a_gone_frame() -> None:
    """`unknown` must not read as gone: the lifecycle rebuilds on RuntimeError, and a transient
    Engine error must never cost a Machine its unsaved work."""
    frame, _ = _frame("base", lambda request: httpx.Response(200, json={"id": "x", "state": "unknown"}))
    with pytest.raises(FrameUnreachable) as raised:
        await frame.bind()
    assert not isinstance(raised.value, RuntimeError)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>a proxy's error page</html>"),
        httpx.Response(200, json=["not", "a", "record"]),
        httpx.Response(200, json="running"),
    ],
)
async def test_bind_on_a_200_that_is_not_a_record_is_an_infra_error(response: httpx.Response) -> None:
    """Fix wave B4: a body sandbar did not write cannot say the frame is gone - one infra attempt,
    never a rebuild, and never a JSONDecodeError the seed's retry rule does not know."""
    frame, _ = _frame("base", lambda request: response)
    with pytest.raises(FrameUnreachable) as raised:
        await frame.bind()
    assert not isinstance(raised.value, RuntimeError)


@pytest.mark.parametrize("record", [{"id": "x", "state": "running"}, {"id": "x"}])
async def test_bind_attaches_to_a_running_sandbox_or_one_from_an_older_daemon(record: dict[str, Any]) -> None:
    frame, _ = _frame("base", lambda request: httpx.Response(200, json=record))
    assert await frame.bind() is False


async def test_inventory_lists_a_sandbox_whatever_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped sandbox keeps its files and `bind` starts it, so the reconciler judges it as a
    running one; a record that outlived its container is the next bind's or sleep's to find gone,
    and destroyed as disowned once its row moves on. Neither may vanish from the listing."""
    monkeypatch.setattr(settings.worker, "instance", "")
    states = ("running", "stopped", "missing", "unknown", None)
    items = [
        {
            "id": f"machine-{state or 'none'}-1",
            "spec": {"labels": {LABEL_KIND: "machine"}},
            "created_at": "2026-10-02T12:00:00Z",
            **({"state": state} if state else {}),
        }
        for state in states
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": items}))
    monkeypatch.setattr(
        sandbar_driver,
        "_control_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://sandbar"),
    )
    assert [f.ref for f in await sandbar_driver.inventory()] == [f"machine-{s or 'none'}-1" for s in states]


# ── a missing path is a tool result, never a dead step (RR-missing-path) ────────────────────────


def _no_such_path(message: str = "no such path: /task/context.md") -> httpx.Response:
    return httpx.Response(404, json={"error": {"message": message, "code": "no_such_path"}})


@pytest.mark.parametrize("read", ["read_bytes", "read_file"])
async def test_a_path_sandbar_says_is_not_there_is_file_not_found(read: str) -> None:
    """The found-live defect: an agent's read of an absent `/task/context.md` came back an
    `HTTPStatusError` that killed the step. sandbar's 404 `no_such_path` is the Docker driver's
    `FileNotFoundError`, which `read_file` the tool turns into an answer."""
    frame, rec = _frame("base", lambda request: _no_such_path())
    with pytest.raises(FileNotFoundError) as raised:
        await getattr(frame, read)("/task/context.md")
    assert str(raised.value) == "no such path: /task/context.md"  # names the path once
    assert rec.requests[0].url.params["path"] == "/task/context.md"


async def test_a_missing_directory_lists_empty_like_the_docker_driver() -> None:
    """The Docker driver's `list_dir` is `ls … 2>/dev/null || true` (and the fake frame's a prefix
    filter): a directory that is not there lists as nothing. Parity, so no caller sees the swap."""
    frame, _ = _frame(
        "base", lambda request: _no_such_path("no such path: list /nope: ls: /nope: No such file")
    )
    assert await frame.list_dir("/nope") == []


@pytest.mark.parametrize("entries", [None, []])
async def test_an_empty_directory_lists_as_nothing(entries: list[str] | None) -> None:
    """A fresh Machine's `/task/inputs` is empty, and a sandbar whose Go nil slice marshals as
    `"entries": null` must read as the empty list - the step boundary lists it on every step."""
    frame, _ = _frame("base", lambda request: httpx.Response(200, json={"entries": entries}))
    assert await frame.list_dir("/task/inputs") == []


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "no such sandbox", "code": "not_found"}},
        {"error": {"message": "no such sandbox"}},  # a sandbar from before the code
        {},
    ],
)
@pytest.mark.parametrize("read", ["read_bytes", "read_file", "list_dir"])
async def test_any_other_404_on_a_read_is_the_sandbox_gone(read: str, body: dict[str, Any]) -> None:
    """Only `no_such_path` is a missing path: any other 404 is the sandbox gone, never a file the
    model should be told is absent. It surfaces as `exec`'s 404 does (`_post`: `PolicyDenied`, a
    failed attempt whose retry re-binds or wakes the Machine) - never a raw `RuntimeError`, which
    nothing in an agent run catches and which fails the task (fix round 1)."""
    frame, _ = _frame("base", lambda request: httpx.Response(404, json=body))
    with pytest.raises(PolicyDenied, match="sandbar 404") as raised:
        await getattr(frame, read)("/task/context.md")
    assert not isinstance(raised.value, RuntimeError)


@pytest.mark.parametrize(
    "message",
    [
        'docker 404: {"message":"Could not find the file /task/context.md in container 4319b0ba"}',
        "list /task/context.md: ls: /task/context.md: No such file or directory",
    ],
)
async def test_an_older_sandbars_502_for_a_missing_path_is_still_a_missing_path(message: str) -> None:
    """One release of tolerance: a sandbar from before `no_such_path` answered a missing path 502
    with the Engine's (or ls's) words."""
    frame, _ = _frame("base", lambda request: httpx.Response(502, json={"error": {"message": message}}))
    if message.startswith("docker"):
        with pytest.raises(FileNotFoundError):
            await frame.read_bytes("/task/context.md")
    else:
        assert await frame.list_dir("/task/context.md") == []


@pytest.mark.parametrize("read", ["read_bytes", "list_dir"])
async def test_every_other_read_failure_still_raises(read: str) -> None:
    """A backend failure (a 502 that names no missing path) is not a missing path: it raises as
    before, so a broken frame is never reported to the model as an empty one."""
    frame, _ = _frame(
        "base", lambda request: httpx.Response(502, json={"error": {"message": "docker 500: boom"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await getattr(frame, read)("/work")


async def test_the_read_file_tool_answers_a_missing_path_on_sandbar() -> None:
    """End to end through the tool the model holds: the missing read is an ANSWER, the
    step lives."""
    from prismoid.ai.tools.frame_tools import frame_toolset

    class _Ctx:
        tool_call_id = "toolu_read_01"

    frame, _ = _frame("base", lambda request: _no_such_path())
    read_file: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function
    answer = await read_file(_Ctx(), "/task/context.md")
    assert isinstance(answer, str) and "/task/context.md does not exist" in answer


async def test_the_read_file_tool_on_a_gone_sandbox_costs_an_attempt_not_the_task() -> None:
    """Fix round 1: every sandbard restart forgets its sandboxes, so a read can meet a 404
    `not_found` mid-run. Out of the tool it must come as `PolicyDenied` - what `execution` counts
    as one failed attempt - never a raw `RuntimeError` that escapes the run and fails the task."""
    from prismoid.ai.tools.frame_tools import frame_toolset

    class _Ctx:
        tool_call_id = "toolu_read_02"

    gone = {"error": {"message": "no such sandbox", "code": "not_found"}}
    frame, _ = _frame("base", lambda request: httpx.Response(404, json=gone))
    read_file: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function
    with pytest.raises(PolicyDenied) as raised:
        await read_file(_Ctx(), "/task/context.md")
    assert not isinstance(raised.value, RuntimeError)


def test_frame_unreachable_is_the_protocols_error_not_sandbars() -> None:
    """Spec A3: every driver raises the one "the provider did not answer" error, so the lifecycle
    reads it the same whatever the provider - one infra attempt, never a RuntimeError (a gone
    frame, a rebuild)."""
    from prismoid.ai.frames import driver

    assert FrameUnreachable is driver.FrameUnreachable is sandbar_driver.FrameUnreachable
    assert FrameUnreachable.__module__ == "prismoid.ai.frames.driver"
    assert issubclass(FrameUnreachable, ConnectionError)
    assert not issubclass(FrameUnreachable, RuntimeError)
