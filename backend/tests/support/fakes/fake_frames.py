# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""FakeFrame - an in-memory FrameDriver for tests that exercise the Machine's policy without Docker
(ADR-063). Files live in a dict with a logical clock for mtimes; archives are real tars through
the real `archives` filters; `world` is the provider's inventory."""

from __future__ import annotations

import datetime as dt
import io
import itertools
import shlex
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from typing import IO, Any, ClassVar, Final, Literal

from prismoid.ai.frames.archives import checkpoint_filter, restore_filter, rewrite
from prismoid.ai.frames.driver import ArchiveTooLarge, ExecResult, FrameUnreachable
from prismoid.ai.frames.layout import INPUTS, INSTRUCTIONS, SKILLS, STANDARD_PATHS
from prismoid.conf import settings
from prismoid.schemas.device import FolderGrant, InterruptedOut
from prismoid.schemas.machines import LABEL_KIND, FrameDriverName, FrameInfo

_CLOCK = itertools.count(1)


class FrameStopped(Exception):
    """What a provider answers a frame that exists but is not running (Docker's 409 "container is
    not running" to an exec): not a `RuntimeError`, so a caller cannot read it as a vanished
    frame."""


class FakeFrame:
    paths = STANDARD_PATHS
    driver: ClassVar[str] = "docker"  # what a Docker Machine's row says - the policy tests' rows
    # Every `new_frame` the fake factory served: (the driver asked for, the frame's name). Tests
    # read it to see which driver an operation reached an existing Machine's frame through.
    made: ClassVar[list[tuple[str | None, str]]] = []
    # Every `destroy_ref` the fake served: (the ref, the driver it was asked through).
    destroyed: ClassVar[list[tuple[str, str | None]]] = []
    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "archive"
    world: ClassVar[dict[str, FakeFrame]] = {}
    created: ClassVar[list[str]] = []  # every ref ever created, in order - for assertions
    # The provider cannot say whether a frame runs (sandbar's `unknown`, an Engine that did not
    # answer): `bind` raises `FrameUnreachable`, never a RuntimeError. Reset by the fixture.
    unreachable: ClassVar[bool] = False

    def __init__(
        self,
        task_id: str,
        profile: str = "base",
        attempt: int = 1,
        *,
        name: str | None = None,
        recording: str | None = None,
    ) -> None:
        self.task_id, self.profile, self.attempt = task_id, profile, attempt
        self.name = name or f"frame-{task_id}"
        self.recording_dir = f"/artifacts/recording-{recording or attempt}"
        self.files: dict[str, tuple[bytes, int]] = {}
        self.labels: dict[str, str] = {}
        self.frozen = False
        self.calls: list[str] = []  # the lifecycle verbs that reached this frame, in order (on the live one)
        self.egress_version = 0
        self.egress_hosts: list[str] = []
        self.execs: list[str] = []
        self.created_at = dt.datetime.now(dt.UTC)
        self.running = True  # False: the provider still lists it, but a restart stopped it
        self.suspended = False  # a native frame `suspend` kept, files and memory (NativeFakeFrame)
        self.sidecar: str | None = None  # the running browser daemon's recording folder, if one runs
        self.closes = 0  # `close` calls that reached the live frame (a remote browser session ends there)

    def stop(self) -> None:
        """A Docker or host restart: the frame and its files are still there, its processes are not."""
        self.running, self.sidecar = False, None

    def _up(self) -> None:
        live = FakeFrame.world.get(self.name)
        if live is not None and not live.running:
            raise FrameStopped(f"frame {self.name} is not running")
        if live is not None and live.suspended:
            raise FrameStopped(f"frame {self.name} is suspended")

    def _log(self, verb: str) -> None:
        """Note a lifecycle verb on the LIVE frame: `bind` hands out copies, so a flag set on the
        caller's object is invisible to a test reading `world[ref]`; the order of verbs is not."""
        live = FakeFrame.world.get(self.name)
        if live is not None:
            live.calls.append(verb)

    @property
    def ref(self) -> str:
        return self.name

    # ── lifecycle ────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        self.labels = dict(labels or {})
        self.egress_version, self.egress_hosts = egress_version, sorted(allow_domains or [])
        FakeFrame.world[self.name] = self
        FakeFrame.created.append(self.name)

    async def bind(self, *, start: bool = True) -> bool:
        if FakeFrame.unreachable:
            raise FrameUnreachable(f"cannot say whether frame {self.name} runs")
        live = FakeFrame.world.get(self.name)
        if live is None:
            raise RuntimeError(f"frame {self.name} is gone")
        started = not live.running
        if started:  # the drivers' contract: a stopped frame is started, its address new
            if not start:
                raise RuntimeError(f"frame {self.name} is exited, not running")
            live.running, live.egress_version, live.egress_hosts = True, 0, []
        self.__dict__.update({k: v for k, v in live.__dict__.items() if k not in ("recording_dir",)})
        self.files = live.files  # share the live filesystem, as a real bind would
        return started

    async def destroy(self) -> None:
        FakeFrame.world.pop(self.name, None)

    async def close(self) -> None:
        live = FakeFrame.world.get(self.name)
        (live if live is not None else self).closes += 1

    async def reset_mounts(self) -> None:
        self._up()
        for path in [
            p for p in self.files if p.startswith((f"{INPUTS}/", f"{SKILLS}/")) or p in INSTRUCTIONS
        ]:
            del self.files[path]

    async def freeze(self) -> None:
        self._up()
        self.frozen = True
        self._log("freeze")

    async def thaw(self) -> None:
        self._up()
        self.frozen = False
        self._log("thaw")

    async def suspend(self) -> None:
        raise NotImplementedError("a FakeFrame sleeps by archive, never natively")

    async def resume(self) -> None:
        raise NotImplementedError("a FakeFrame sleeps by archive, never natively")

    # ── files ────────────────────────────────────────────────────────────────────────────

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: str = "agent") -> None:
        self._up()
        self.files[path] = (data, next(_CLOCK))

    async def write_file(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def read_bytes(self, path: str) -> bytes:
        return self.files[path][0]

    async def read_file(self, path: str) -> str:
        return self.files[path][0].decode()

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix) :].split("/")[0] for p in self.files if p.startswith(prefix)})

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        self._up()
        self.execs.append(command)
        if "find /work -newer " in command:
            marker = command.split("-newer ", 1)[1].split()[0]
            if marker not in self.files:  # `test -e <marker> && find …` fails without a marker
                return ExecResult(1, "", "")
            since = self.files[marker][1]
            hits = sorted(p for p, (_, t) in self.files.items() if p.startswith("/work/") and t > since)
            return ExecResult(0, "\n".join(hits), "")
        if command.startswith("realpath -m -- "):  # the write tools' resolve: no symlinks here
            return ExecResult(0, f"{shlex.split(command)[-1]}\n", "")
        if command.startswith("du -sb /work"):
            total = sum(len(d) for p, (d, _) in self.files.items() if p.startswith("/work/"))
            return ExecResult(0, f"{total}\t/work", "")
        if command.startswith("rm -f "):
            for path in command.split()[2:]:
                self.files.pop(path, None)
        if " && cp -r " in command:  # the recording's copy out of /artifacts (`_publish_recording`)
            src, dst = command.split(" && cp -r ", 1)[1].split()
            tree = {p: v for p, v in self.files.items() if p.startswith(f"{src}/")}
            if not tree:
                return ExecResult(1, "", f"cp: {src}: No such file or directory")
            self.files.update({f"{dst}{p[len(src) :]}": v for p, v in tree.items()})
        return ExecResult(0, "", "")

    async def manifest(self) -> dict[str, Any]:
        import orjson

        raw = self.files.get("/artifacts/manifest.json")
        return dict(orjson.loads(raw[0])) if raw else {"entries": []}

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        """The publish runner's entry (`ai/frames/publish.py`), so a harvest can read it."""
        import hashlib

        import orjson

        entries = (await self.manifest())["entries"]
        name = path.rsplit("/", 1)[-1]
        # A directory travels as a tar of its tree, as the runner's does.
        data = self.files[path][0] if path in self.files else self._tar(path)
        self.files[f"/artifacts/{name}"] = (data, next(_CLOCK))
        entries.append(
            {
                "path": f"/artifacts/{name}",
                "type": type,
                "title": title,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "sources": list(sources),
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
        await self.write_bytes("/artifacts/manifest.json", orjson.dumps({"entries": entries}))

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        """The sidecar as it behaves: ONE daemon per frame, spawned by the
        first command with the recording folder that exec names and recording there until a
        `record` moves it to the folder its own exec names (a `record` with no daemon spawns
        none); `shutdown` ends it. An act files a row; `finalize` indexes the folder."""
        live = FakeFrame.world.get(self.name, self)
        verb = command.get("verb")
        if verb == "shutdown":
            live.sidecar = None
            return {"ok": True}
        if verb == "record" and live.sidecar is None:
            return {"ok": True}
        if verb == "record" or live.sidecar is None:
            live.sidecar = self.recording_dir
            self.files.setdefault(f"{live.sidecar}/actions.json", (b"[]", next(_CLOCK)))
        folder = live.sidecar
        acts = [p for p in self.files if p.startswith(f"{folder}/") and p.endswith(".jpg")]
        if verb in ("open", "back", "click", "type", "fill", "select", "press", "scroll"):
            self.files[f"{folder}/{len(acts) + 1:04d}-{verb}.jpg"] = (b"\xff\xd8", next(_CLOCK))
        elif verb == "finalize":
            self.files[f"{folder}/index.html"] = (b"<!doctype html>", next(_CLOCK))
            return {"ok": True, "actions": len(acts), "acting": len(acts), "path": folder}
        return {"ok": True}

    def _tar(self, root: str) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for path, (data, _) in sorted(self.files.items()):
                if path.startswith(f"{root}/"):
                    info = tarfile.TarInfo(path[len(root.rsplit("/", 1)[0]) + 1 :])
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        return raw.getvalue()

    async def denials(self) -> list[dict[str, str]]:
        return []

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        self._up()
        # The provider keeps the allowlist and its version, not the driver object: the live frame
        # (`world`) stands for the egress proxy, and a driver that `bind()` attached pushes to it.
        live = FakeFrame.world.get(self.name)
        proxy = live if live is not None else self
        if version is None and self.egress_version == 0:  # unsynced: unversioned, version untouched
            self.egress_hosts = sorted(domains)
        else:
            version = self.egress_version + 1 if version is None else version
            if version <= self.egress_version:
                return False  # the driver's own stale drop: no request
            if proxy is not self and version <= proxy.egress_version:
                # The proxy refuses a stale version (409) and says which it holds; the driver
                # adopts it (the drivers' contract), so its owner can push once past it.
                self.egress_version = proxy.egress_version
                return False
            self.egress_version, self.egress_hosts = version, sorted(domains)
        if proxy is not self:
            proxy.egress_hosts = list(self.egress_hosts)
            proxy.egress_version = max(proxy.egress_version, self.egress_version)
        return True

    # ── archives ─────────────────────────────────────────────────────────────────────────

    async def archive(self, path: str, out: IO[bytes], *, limit: int | None = None) -> None:
        self._up()
        raw = io.BytesIO()
        root = path.strip("/")
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for name, (data, _) in sorted(self.files.items()):
                if name.startswith(f"/{root}/"):
                    info = tarfile.TarInfo(name.lstrip("/"))
                    info.size, info.uid = len(data), 1000
                    tar.addfile(info, io.BytesIO(data))
        if limit is not None and raw.tell() > limit:
            raise ArchiveTooLarge(raw.tell())
        raw.seek(0)
        rewrite(raw, out, checkpoint_filter(path))

    async def unpack(self, path: str, src: IO[bytes]) -> None:
        self._up()
        clean = io.BytesIO()
        rewrite(src, clean, restore_filter(path))
        clean.seek(0)
        with tarfile.open(fileobj=clean, mode="r") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    self.files[f"/{member.name}"] = (extracted.read(), next(_CLOCK))

    # ── the provider ─────────────────────────────────────────────────────────────────────

    @classmethod
    def inventory(cls) -> list[FrameInfo]:
        return [
            FrameInfo(ref=f.name, labels=dict(f.labels), created_at=f.created_at, driver=f.driver)
            for f in cls.world.values()
        ]


SANDBAR: Final = "sandbar"


class SandbarFakeFrame(FakeFrame):
    """A frame on the other production driver, sleeping by archive like Docker's: what a test lists
    under `sandbar`, so a row can name a frame its listing finds on another driver (a stale stamp
    the reconciler re-stamps, a driver that does not answer)."""

    driver: ClassVar[str] = SANDBAR


NATIVE: Final = "fake-native"


class NativeFakeFrame(FakeFrame):
    """A provider that sleeps natively, standing in for a remote driver:
    `suspend` keeps the frame and everything in it, `resume` brings it back, and
    while it sleeps nothing reaches it (`_up`). A worker whose `frame_driver` is `NATIVE` makes
    these."""

    driver: ClassVar[str] = NATIVE
    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "native"

    async def suspend(self) -> None:
        live = FakeFrame.world.get(self.name)
        if live is not None and live.suspended:
            return  # idempotent: DBOS replays a step
        self._up()
        FakeFrame.world[self.name].suspended = True
        self._log("suspend")

    async def resume(self) -> None:
        live = FakeFrame.world.get(self.name)
        if live is None:
            raise RuntimeError(f"frame {self.name} is gone")
        live.suspended = False  # idempotent: resuming a running frame changes nothing
        self._log("resume")


DEVICE: Final = FrameDriverName.DEVICE


class NoneSleepFakeFrame(FakeFrame):
    """A frame that is never archived, standing in for `DeviceFrame`:
    a sleep stops what it left running (`stop_services`) and keeps it, files and all, and a wake
    binds it. As the helper does, it names a Machine's frame `device-{task_id}` whatever it was
    asked, so every wake's create lands on the same frame, its files kept. A task whose
    `machine_driver` is `DEVICE` makes these, and so does a worker whose `frame_driver` is."""

    driver: ClassVar[str] = DEVICE
    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "none"
    device_name: str = "Test Mac"
    # The helper's exec journal: the command a crash cut off, set on the LIVE frame by a
    # test standing in for the helper, until the worker acknowledges it.
    cut: InterruptedOut | None = None
    # The grants the helper holds: what the frame was told (`grant`) at its last create
    # or bind. None: never told any.
    granted: tuple[list[FolderGrant], bool] | None = None

    def grant(self, folders: Sequence[FolderGrant], docker: bool) -> None:
        self.__dict__["told"] = (list(folders), docker)

    def _tell(self, told: tuple[list[FolderGrant], bool] | None) -> None:
        live = FakeFrame.world.get(self.name)
        if told is not None and live is not None:
            live.granted = self.granted = told  # pyright: ignore[reportAttributeAccessIssue]

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        if FakeFrame.unreachable:  # a helper that is down makes nothing
            raise FrameUnreachable("the device helper did not answer")
        if (labels or {}).get(LABEL_KIND) == "machine":
            self.name = f"device-{self.task_id}"
        kept = FakeFrame.world.get(self.name)
        await super().create(task_id, allow_domains, labels=labels, egress_version=egress_version)
        if kept is not None:
            self.files = kept.files  # the helper re-creates a frame over its own folders
        self._tell(self.__dict__.get("told"))

    async def bind(self, *, start: bool = True) -> bool:
        told = self.__dict__.get("told")
        started = await super().bind(start=start)
        self._tell(told)
        return started

    async def stop_services(self) -> None:
        self._log("stop_services")

    async def forget_quarantine(self) -> None:
        self._log("forget_quarantine")

    async def interrupted(self) -> InterruptedOut | None:
        if FakeFrame.unreachable:
            raise FrameUnreachable("the device helper did not answer")
        live = FakeFrame.world.get(self.name)
        if live is None:
            raise RuntimeError(f"no device frame {self.name!r}")
        return getattr(live, "cut", None)

    async def ack_interrupted(self, exec_id: str | None = None) -> None:
        live = FakeFrame.world.get(self.name)
        cut = getattr(live, "cut", None)
        if live is not None and cut is not None and exec_id in (None, cut.exec_id):
            live.cut = None  # pyright: ignore[reportAttributeAccessIssue]
            self._log("ack_interrupted")


def _serving() -> dict[str, type[FakeFrame]]:
    """Each driver a fake frame class serves, and that class."""
    return {cls.driver: cls for cls in (FakeFrame, SandbarFakeFrame, NativeFakeFrame, NoneSleepFakeFrame)}


def frame_class(driver: str | None) -> type[FakeFrame]:
    """The fake a `new_frame(driver=...)` makes: the one serving `driver` (named, or this worker's),
    else FakeFrame - the factory still serves any name, as it always did."""
    return _serving().get(driver or settings.worker.frame_driver, FakeFrame)


def install(monkeypatch: Any) -> None:
    """Route `prismoid.ai.frames.new_frame`/`inventory`/`destroy_ref`/`ensure_ready` to the fakes.
    Operations reach them as `frames.new_frame(...)` (module attribute), so the patch takes."""
    import prismoid.ai.frames as frames

    def _new_frame(
        task_id: str,
        *,
        driver: str | None = None,
        profile: str = "base",
        attempt: int = 1,
        name: str | None = None,
        recording: str | None = None,
    ) -> FakeFrame:
        FakeFrame.made.append((driver, name or f"frame-{task_id}"))
        return frame_class(driver)(task_id, profile, attempt, name=name, recording=recording)

    async def _inventory(drivers: Iterable[str] | None = None) -> list[FrameInfo]:
        # As `frames.inventory`: only the drivers asked (None: this worker's), and a driver no fake
        # frame class serves raises - so a fake-backed test sees a stale stamp and a silent driver.
        named = list(drivers) if drivers is not None else [""]
        asked = {d or settings.worker.frame_driver for d in named}
        for driver in sorted(asked):
            if driver not in _serving():
                raise ValueError(f"unknown frame_driver {driver!r}; no fake frame serves it")
        return [f for f in FakeFrame.inventory() if f.driver in asked]

    async def _destroy_ref(ref: str, *, driver: str | None = None) -> None:
        FakeFrame.destroyed.append((ref, driver))
        FakeFrame.world.pop(ref, None)

    monkeypatch.setattr(frames, "new_frame", _new_frame)
    monkeypatch.setattr(frames, "inventory", _inventory)
    monkeypatch.setattr(frames, "destroy_ref", _destroy_ref)
    monkeypatch.setattr(frames, "ensure_ready", lambda profile="base", driver=None: None)
    monkeypatch.setattr(frames, "sleep_kind", lambda driver=None: frame_class(driver).sleep_kind)
