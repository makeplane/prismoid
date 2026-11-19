# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker filtering proxy against the real daemon.

Each test's commands run fenced (`exec.run`) with the Docker grant, so they reach Docker only
through the frame's own filtering socket. The tests touch only what they create: every object
wears this test's `prismoid.frame`, or the `prismoid.test` marker below, and teardown removes exactly
those. Skipped without a Docker daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from prismoid.device import docker_proxy
from prismoid.device import exec as device_exec
from prismoid.device.egress import DeviceEgress
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.schemas.device import ExecIn, ExecOut, FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only

pytestmark = darwin_only

USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
IMAGE = "alpine:3.20"
MARKER = "prismoid.test"  # what a test makes outside every frame wears this, and only that is removed


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """The user's own Docker CLI, unfenced: only for setup, checks and teardown of our objects."""
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONFIG")}
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=check, env=env
    )


@pytest.fixture(scope="module")
def daemon() -> str:
    upstream = docker_proxy.find_upstream()
    if upstream is None or _docker("info", check=False).returncode != 0:
        pytest.skip("no Docker daemon")
    return upstream


class Device:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.registry = FrameRegistry(home)
        self.egress = DeviceEgress(self.registry.frames)
        self.dockers = docker_proxy.DockerProxies(home)

    def frame(self, folder: Path) -> DeviceFrameState:
        spec = FrameSpecIn(
            task_id=f"e2-{uuid.uuid4().hex[:8]}",
            kind=FrameKind.MACHINE,
            grants=[FolderGrant(path=str(folder), mode="rw", name="/work")],
            docker=True,
        )
        return self.registry.create(spec)

    async def run(self, frame: DeviceFrameState, command: str, limit: float = 240) -> ExecOut:
        req = ExecIn(exec_id=uuid.uuid4().hex, command=command, timeout=limit)
        return await device_exec.run(frame, req, proxy=self.egress, docker=self.dockers)


def _build(tag: str, dockerfile: str) -> subprocess.CompletedProcess[str]:
    """An image of this test's own, built outside Prismoid (the owner's CLI)."""
    return subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _remove_labelled(selector: str) -> None:
    """Remove the containers, networks and volumes wearing `selector`, and nothing else."""
    for kind, listing in (
        ("container", ["ps", "-aq"]),
        ("network", ["network", "ls", "-q"]),
        ("volume", ["volume", "ls", "-q"]),
    ):
        ids = _docker(*listing, "--filter", f"label={selector}", check=False).stdout.split()
        if ids:
            _docker(*([kind, "rm", "-f"] if kind == "container" else [kind, "rm"]), *ids, check=False)


@pytest.fixture
async def device(daemon: str) -> AsyncIterator[Device]:
    # A short home: a frame's socket path must fit macOS's 104 bytes (pytest's tmp_path does not).
    home = Path(tempfile.mkdtemp(prefix="pdx-", dir="/tmp"))
    d = Device(home)
    await d.egress.serve()
    try:
        yield d
    finally:
        await d.dockers.close_all()
        for ref in list(d.registry.frames):
            _remove_labelled(f"{docker_proxy.FRAME_LABEL}={ref}")
            d.registry.destroy(ref)
        await d.egress.close()
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def folder() -> Iterator[Path]:
    """A folder in the user's real home: a grant must be in the home (or on an external disk)."""
    path = Path(tempfile.mkdtemp(prefix="prismoid-e2-", dir=USER_HOME))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _labelled(kind: str, ref: str) -> list[str]:
    listing = {"container": ["ps", "-q"], "network": ["network", "ls", "--format", "{{.Name}}"]}.get(
        kind, ["volume", "ls", "--format", "{{.Name}}"]
    )
    return _docker(*listing, "--filter", f"label={docker_proxy.FRAME_LABEL}={ref}").stdout.split()


COMPOSE = f"""\
services:
  web:
    image: {IMAGE}
    command: ["sh", "-c", "echo hello > /data/hello && sleep 300"]
    volumes:
      - ./data:/data
      - cache:/cache
  db:
    image: {IMAGE}
    command: ["sleep", "300"]
volumes:
  cache: {{}}
"""


async def test_compose_up_through_proxy(device: Device, folder: Path) -> None:
    (folder / "compose.yaml").write_text(COMPOSE)
    (folder / "data").mkdir()
    frame = device.frame(folder)
    project = f"prismoid-e2-{uuid.uuid4().hex[:6]}"
    up = await device.run(frame, f"docker compose -p {project} up -d --quiet-pull")
    assert up.exit_code == 0, up.stderr
    running = _labelled("container", frame.ref)
    assert len(running) == 2, "both services run, each wearing the frame's label"
    labels = json.loads(_docker("inspect", "--format", "{{json .Config.Labels}}", running[0]).stdout)
    assert labels[docker_proxy.TASK_LABEL] == frame.spec.task_id
    assert f"{project}_default" in _labelled("network", frame.ref)
    assert f"{project}_cache" in _labelled("volume", frame.ref)

    ps = await device.run(frame, f"docker compose -p {project} ps -q && docker ps -q")
    assert ps.exit_code == 0, ps.stderr
    assert {c[:12] for c in ps.stdout.split()} == {c[:12] for c in running}, "a list shows only this frame's"
    logs = await device.run(frame, f"sleep 1; docker compose -p {project} exec -T web cat /data/hello")
    assert logs.exit_code == 0 and logs.stdout.strip() == "hello", logs.stderr
    assert (folder / "data" / "hello").read_text().strip() == "hello", "the bind is the granted folder"

    down = await device.run(frame, f"docker compose -p {project} down -v -t 1")
    assert down.exit_code == 0, down.stderr
    assert _labelled("container", frame.ref) == [] and _labelled("volume", frame.ref) == []
    assert _labelled("network", frame.ref) == []


async def test_stop_labelled_stops_task_containers(device: Device, folder: Path) -> None:
    other_folder = Path(tempfile.mkdtemp(prefix="prismoid-e2-", dir=USER_HOME))
    try:
        mine, other = device.frame(folder), device.frame(other_folder)
        volume = f"prismoid-e2-{uuid.uuid4().hex[:6]}"
        for frame in (mine, other):
            named = f"-v {volume}:/v" if frame is mine else ""  # a named volume Docker would make
            started = await device.run(frame, f"docker run -d {named} {IMAGE} sleep 300")
            assert started.exit_code == 0, started.stderr
        assert _labelled("volume", mine.ref) == [volume], "the proxy made it, labelled"
        [ours], [theirs] = _labelled("container", mine.ref), _labelled("container", other.ref)
        await device.dockers.stop(mine.ref)
        state = "{{.State.Running}}"
        assert _docker("inspect", "--format", state, ours).stdout.strip() == "false"
        assert _docker("inspect", "--format", state, theirs).stdout.strip() == "true", (
            "another frame's runs on"
        )
    finally:
        shutil.rmtree(other_folder, ignore_errors=True)


async def test_container_mounting_home_refused(device: Device, folder: Path) -> None:
    frame = device.frame(folder)
    refused = await device.run(frame, f'docker run --rm -v "$HOME:/h" {IMAGE} true')
    assert refused.exit_code != 0
    assert "the bind mount at /h is outside your granted folders" in refused.stderr, refused.stderr
    assert str(USER_HOME) not in refused.stderr, "a refusal names no real path"
    for flag in (
        "--privileged",
        "--network host",
        "-v /var/run/docker.sock:/var/run/docker.sock",
        "--security-opt systempaths=unconfined",  # a writable /proc/sys
    ):
        out = await device.run(frame, f"docker run --rm {flag} {IMAGE} true")
        assert out.exit_code != 0 and "not allowed" in out.stderr, (flag, out.stderr)
    assert _docker("ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={frame.ref}").stdout == ""


async def test_another_containers_are_out_of_reach(device: Device, folder: Path, daemon: str) -> None:
    marker = f"{MARKER}={uuid.uuid4().hex[:8]}"
    try:
        theirs = _docker("create", "--label", marker, IMAGE, "sleep", "300").stdout.strip()
        frame = device.frame(folder)
        for command in (f"docker start {theirs}", f"docker rm -f {theirs}", f"docker logs {theirs}"):
            out = await device.run(frame, command)
            assert out.exit_code != 0 and "is not this task's" in out.stderr, (command, out.stderr)
        listed = await device.run(frame, "docker ps -aq")
        assert listed.exit_code == 0 and theirs[:12] not in listed.stdout
        assert _docker("inspect", "--format", "{{.State.Status}}", theirs).stdout.strip() == "created"
        direct = await device.run(frame, f"DOCKER_HOST=unix://{daemon} docker ps")
        assert direct.exit_code != 0, "the fence admits no Docker socket but the frame's own"
    finally:
        _remove_labelled(marker)


async def test_no_grant_no_socket(device: Device, folder: Path) -> None:
    frame = device.frame(folder)
    started = await device.run(frame, "docker version --format '{{.Server.APIVersion}}'")
    assert started.exit_code == 0, started.stderr
    attached = await device.run(frame, f"echo piped | docker run --rm -i {IMAGE} sh -c 'cat; echo hi'")
    assert attached.exit_code == 0 and attached.stdout.split() == ["piped", "hi"], attached.stderr
    socket = docker_proxy.socket_path(device.home, frame.ref)
    assert socket.exists() and oct(socket.stat().st_mode & 0o777) == "0o600"
    device.registry.regrant(frame, GrantsIn(grants=frame.spec.grants, docker=False))
    await device.dockers.ensure(frame)
    assert not socket.exists(), "a revoked grant's socket goes"


async def test_build_through_proxy(device: Device, folder: Path) -> None:
    """BuildKit: the build's `/session` and `/grpc` streams pass; the result lands in the folder."""
    (folder / "Dockerfile").write_text(f"FROM {IMAGE}\nRUN echo built > /built\n")
    frame = device.frame(folder)
    built = await device.run(frame, "docker build --output type=local,dest=out .")
    assert built.exit_code == 0, built.stderr
    assert (folder / "out" / "built").read_text().strip() == "built"


async def test_a_bind_turned_link_is_refused_at_start(device: Device, folder: Path) -> None:
    """A folder vetted at create that becomes a link out before the start is vetted again."""
    (folder / "data").mkdir()
    frame = device.frame(folder)
    made = await device.run(frame, f"docker create -v $PRISMOID_WORK_DIR/data:/data {IMAGE} true")
    assert made.exit_code == 0, made.stderr
    swapped = await device.run(
        frame, f"rmdir data && ln -s {USER_HOME} data && docker start {made.stdout.strip()}"
    )
    assert swapped.exit_code != 0
    assert "bind mount at /data is outside your granted folders" in swapped.stderr, swapped.stderr


async def test_a_volume_bound_to_a_folder_refused_at_create(device: Device, folder: Path) -> None:
    """Review E2 #1: a local volume bound to a folder inside a grant, the folder then swapped for a
    link to `~`, would be bound by the daemon at start, past every check. It never gets made."""
    frame = device.frame(folder)
    opts = "type=none --opt o=bind --opt device=$PRISMOID_WORK_DIR/data"
    made = await device.run(frame, f"mkdir data && docker volume create --opt {opts} bound")
    assert made.exit_code != 0 and "use a bind mount instead" in made.stderr, made.stderr
    mount = "type=volume,src=bound2,dst=/d,volume-opt=type=none,volume-opt=o=bind,volume-opt=device=$PRISMOID_WORK_DIR/data"
    ran = await device.run(frame, f'docker run --rm --mount "{mount}" {IMAGE} true')
    assert ran.exit_code != 0 and "use a bind mount instead" in ran.stderr, ran.stderr
    assert str(folder) not in made.stderr + ran.stderr, "a refusal names no real path"
    assert _labelled("volume", frame.ref) == []
    events = await device.run(frame, "docker events --since 5m --until 1s")
    assert events.exit_code == 0, events.stderr


async def test_destroy_removes_task_objects(device: Device, folder: Path) -> None:
    """At the frame's end its containers, networks and anonymous volumes go; a named volume stays."""
    frame = device.frame(folder)
    tag = uuid.uuid4().hex[:6]
    made = await device.run(
        frame,
        f"docker network create prismoid-e2-{tag} >/dev/null && docker volume create prismoid-e2-{tag} >/dev/null && "
        f"docker run -d --network prismoid-e2-{tag} -v /anon -v prismoid-e2-{tag}:/named {IMAGE} sleep 300",
    )
    assert made.exit_code == 0, made.stderr
    [container] = _labelled("container", frame.ref)
    mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
    [anonymous] = [m["Name"] for m in mounts if m["Destination"] == "/anon"]
    await device.dockers.destroy(frame.ref)
    assert _docker("ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={frame.ref}").stdout == ""
    assert _labelled("network", frame.ref) == []
    assert _docker("volume", "inspect", anonymous, check=False).returncode != 0, "the anonymous volume went"
    assert _labelled("volume", frame.ref) == [f"prismoid-e2-{tag}"], (
        "a named volume (a database's data) stays"
    )


async def test_a_label_from_an_image_is_not_this_frames(device: Device, folder: Path) -> None:
    """Review N3: an image whose Dockerfile sets `LABEL prismoid.frame=<ref>` puts the label on every
    container run from it, by anyone. One the owner runs outside Prismoid is not the frame's: its sleep
    and its end leave it alone, and its proxy will not act on it."""
    frame = device.frame(folder)
    image, marker = f"prismoid-e2-label-{uuid.uuid4().hex[:6]}", f"{MARKER}={uuid.uuid4().hex[:8]}"
    dockerfile = f"FROM {IMAGE}\nLABEL {docker_proxy.FRAME_LABEL}={frame.ref}\n"
    built = _build(image, dockerfile)
    assert built.returncode == 0, built.stderr
    try:
        owners = _docker("run", "-d", "--label", marker, image, "sleep", "300").stdout.strip()
        mine = await device.run(frame, f"docker run -d {IMAGE} sleep 300")
        assert mine.exit_code == 0, mine.stderr
        assert (
            owners[:12]
            in _docker("ps", "-q", "--filter", f"label={docker_proxy.FRAME_LABEL}={frame.ref}").stdout
        )
        refused = await device.run(frame, f"docker stop {owners}")
        assert refused.exit_code != 0 and "is not this task's" in refused.stderr, refused.stderr
        await device.dockers.stop(frame.ref)
        state = "{{.State.Running}}"
        assert _docker("inspect", "--format", state, owners).stdout.strip() == "true", "a sleep leaves it"
        await device.dockers.destroy(frame.ref)
        assert _docker("inspect", "--format", state, owners).stdout.strip() == "true", "the end leaves it"
        assert _docker("inspect", mine.stdout.strip(), check=False).returncode != 0, "the frame's own went"
    finally:
        _remove_labelled(marker)
        _docker("rmi", image, check=False)  # the image this test built, by its own tag


async def test_compose_recreate_through_proxy(device: Device, folder: Path) -> None:
    """A service whose config changed is recreated (compose stops, renames and replaces it), its
    named volume carried over; and a Machine rebuilt under its ref reaches that volume again."""
    (folder / "compose.yaml").write_text(COMPOSE)
    (folder / "data").mkdir()
    frame = device.frame(folder)
    project = f"prismoid-e2-{uuid.uuid4().hex[:6]}"
    up = await device.run(frame, f"docker compose -p {project} up -d --quiet-pull")
    assert up.exit_code == 0, up.stderr
    (folder / "compose.yaml").write_text(COMPOSE.replace('["sleep", "300"]', '["sleep", "301"]'))
    again = await device.run(frame, f"docker compose -p {project} up -d")
    assert again.exit_code == 0, again.stderr
    assert "Recreate" in again.stderr or "Recreated" in again.stderr, again.stderr
    assert len(_labelled("container", frame.ref)) == 2
    down = await device.run(frame, f"docker compose -p {project} down -t 1")  # the named volume stays
    assert down.exit_code == 0, down.stderr
    await device.dockers.destroy(frame.ref)
    device.registry.destroy(frame.ref)
    rebuilt = device.registry.create(frame.spec)  # the same task's Machine, made anew under its ref
    device.dockers.revive(rebuilt.ref)
    back = await device.run(
        rebuilt, f"docker compose -p {project} up -d && docker compose -p {project} down -v -t 1"
    )
    assert back.exit_code == 0, back.stderr
    assert _labelled("volume", frame.ref) == []


async def _raw(device: Device, frame: DeviceFrameState, route: str, body: bytes) -> tuple[int, str]:
    """A request a client crafts by hand, straight to the frame's filtering socket: (status, body)."""
    await device.dockers.ensure(frame)
    path = docker_proxy.socket_path(device.home, frame.ref)
    reader, writer = await asyncio.open_unix_connection(str(path))
    head = f"POST {route} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"
    writer.write(f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await writer.drain()
    answer = await reader.read()
    writer.close()
    status, _, rest = answer.partition(b" ")[2].partition(b" ")
    return int(status), rest.partition(b"\r\n\r\n")[2].decode(errors="replace")


async def test_lowercase_keys_are_vetted_as_docker_reads_them(device: Device, folder: Path) -> None:
    """Review C1: Docker reads `hostconfig` as `HostConfig`. A privileged, host-PID create spelled so,
    a mount of an owner's volume spelled so, and a second spelling of `Labels` are refused, and
    nothing is made; a lowercase `labels` alone never displaces the frame's."""
    frame = device.frame(folder)
    marker = f"{MARKER}={uuid.uuid4().hex[:8]}"
    standin = f"prismoid-e2-owner-{uuid.uuid4().hex[:6]}"  # our own stand-in for an owner's volume
    _docker("volume", "create", "--label", marker, standin)
    try:
        create = "/v1.47/containers/create"
        privileged = {
            "Image": IMAGE,
            "hostconfig": {"Privileged": True, "PidMode": "host", "CapAdd": ["ALL"]},
        }
        status, why = await _raw(device, frame, create, json.dumps(privileged).encode())
        assert status == 403 and "privileged" in why, why
        mounts = {
            "Image": IMAGE,
            "hostconfig": {"mounts": [{"type": "volume", "source": standin, "target": "/v"}]},
        }
        status, why = await _raw(device, frame, create, json.dumps(mounts).encode())
        assert status == 403 and "is not this task's" in why, why
        forged = (
            b'{"Name": "prismoid-e2-seeded", "Labels": {}, "labels": {"prismoid.frame": "device-sibling"}}'
        )
        status, why = await _raw(device, frame, "/v1.47/volumes/create", forged)
        assert status == 403 and "one key" in why, why
        status, why = await _raw(
            device, frame, "/v1.47/volumes/create", json.dumps({"name": standin}).encode()
        )
        assert status == 403 and "already exists" in why, why
        assert _docker("ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={frame.ref}").stdout == ""
        assert _labelled("volume", frame.ref) == [], "nothing was made"
        assert _docker("volume", "inspect", standin, check=False).returncode == 0, "the stand-in is untouched"
        lowercase = {"Image": IMAGE, "labels": {docker_proxy.FRAME_LABEL: "device-sibling"}}
        status, made = await _raw(device, frame, create, json.dumps(lowercase).encode())
        assert status == 201, made
        ident = json.loads(made)["Id"]
        labels = json.loads(_docker("inspect", "--format", "{{json .Config.Labels}}", ident).stdout)
        assert (
            labels[docker_proxy.FRAME_LABEL] == frame.ref
            and labels[docker_proxy.TASK_LABEL] == frame.spec.task_id
        )
    finally:
        _remove_labelled(marker)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


TOUCH = (
    "touch /w/.git/hooks/pre-commit; echo hook=$?; "
    "echo x >> /w/.git/config; echo config=$?; "
    "touch /w/normal.txt; echo normal=$?"
)


async def test_a_container_never_writes_the_granted_repositorys_git_folder(
    device: Device, folder: Path
) -> None:
    """Fix round 2, on the real daemon: a stand-in repository, granted, bound rw. The container has
    no fence: the read-only overlay is what stops it; the rest of the bind stays writable."""
    _git("init", "-q", cwd=folder)
    config = (folder / ".git" / "config").read_text()
    frame = device.frame(folder)
    assert str(folder / ".git") in frame.protected_git
    out = await device.run(frame, f"docker run --rm -v \"$PWD:/w\" {IMAGE} sh -c '{TOUCH}'")
    assert out.exit_code == 0, out
    assert "hook=1" in out.stdout and "config=1" in out.stdout and "normal=0" in out.stdout, out
    assert (folder / "normal.txt").exists()
    assert not (folder / ".git" / "hooks" / "pre-commit").exists()
    assert (folder / ".git" / "config").read_text() == config


async def test_a_container_never_writes_a_worktrees_pointer_file(device: Device, folder: Path) -> None:
    """A worktree grant's `.git` is a file: it gets the same overlay, and the container cannot
    point the worktree at a repository of its own."""
    main = folder / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x", cwd=main)
    work = folder / "wt"
    _git("worktree", "add", "-q", str(work), cwd=main)
    pointer = (work / ".git").read_text()
    frame = device.frame(work)
    assert str(work / ".git") in frame.protected_git
    script = "echo evil > /w/.git; echo pointer=$?; touch /w/normal.txt; echo normal=$?"
    out = await device.run(frame, f"docker run --rm -v \"$PWD:/w\" {IMAGE} sh -c '{script}'")
    assert "pointer=1" in out.stdout and "normal=0" in out.stdout, out
    assert (work / ".git").read_text() == pointer


async def test_a_container_without_its_overlay_is_refused_at_start(device: Device, folder: Path) -> None:
    """A container created before the repository was protected passes no start: the daemon's own
    view is what is vetted."""
    frame = device.frame(folder)
    out = await device.run(frame, f'docker create -v "$PWD:/w" {IMAGE} sleep 5')
    assert out.exit_code == 0, out
    ident = out.stdout.strip().splitlines()[-1]
    _git("init", "-q", cwd=folder)
    frame.protected_git = (str(folder / ".git"),)
    out = await device.run(frame, f"docker start {ident}")
    assert out.exit_code != 0 and "read-only" in out.stderr, out
    assert _docker("inspect", "-f", "{{.State.Running}}", ident).stdout.strip() == "false"
