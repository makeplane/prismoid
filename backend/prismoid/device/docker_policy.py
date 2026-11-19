# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker proxy's policy: what a frame may ask its Docker for.

Pure: one request in, a `Verdict` out, no daemon (`docker_proxy.py` is the wire and the lookups).

- an allowlist of endpoints (`_ROUTES`); anything else is a 403;
- a container is created with no privileged mode, no host namespace, no device, no added
  capability, no `SecurityOpt` off `SECURITY_OPTS`, no Docker socket, no tmpfs over `TMPFS_MAX` and
  no bind outside the task's granted folders; a read-only grant binds read-only. A plain bind is the only
  way to a host folder: a volume backed by one (or by a device or a share) is refused, since the
  daemon resolves it only when a container starts, past every check;
- what the frame creates (containers, networks, volumes) wears `prismoid.task` and `prismoid.frame`; every
  other action names an object that must already wear this frame's label (`Verdict.owned`, looked
  up upstream), and lists and the event stream are filtered to them.

Docker reads a body's keys as Go's `encoding/json` does, case-insensitively (`hostconfig` is
`HostConfig`), so the policy never forwards the bytes it was sent: it parses them refusing any key
given twice (in any case, but in a map whose keys are data: Labels, DriverOpts, Options, Sysctls,
Annotations...), rewrites every key it vets to Docker's own spelling (`_CONTAINER`, `_VOLUME`...),
vets that, injects the labels last, and sends its own encoding of it. At a container's start (and
right after its create) the daemon's own view of it (`recheck`) is vetted again.

Every refusal names what was refused and why, never a real host path.
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from prismoid.device.fence_data import SECURITY_OPTS
from prismoid.schemas.device import folder_key

TASK_LABEL: Final = "prismoid.task"
FRAME_LABEL: Final = "prismoid.frame"
TMPFS_DEFAULT: Final = 512 * 1024 * 1024  # a tmpfs that names no size is held to this
TMPFS_MAX: Final = 2 * 1024 * 1024 * 1024  # it lives in the Docker VM's memory
_VERSION = re.compile(r"^/v\d+(?:\.\d+)?(?=/)")
_SIZE = re.compile(r"^([0-9]{1,15})([kmg]?)$", re.IGNORECASE)
_UNITS: Final = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_PREDEFINED_NETWORKS: Final = frozenset({"", "default", "bridge", "none"})
_NAMESPACES: Final = ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode")
_SHARED_NAMESPACES: Final = ("NetworkMode", "PidMode", "IpcMode")  # `container:<name>` joins one
_ESCALATIONS: Final = ("Devices", "DeviceCgroupRules", "DeviceRequests", "CapAdd", "Capabilities")
_LOCAL_DRIVERS: Final = (None, "", "local")
# The default runtime under its two names: another (`sysbox`, `kata`) would be another boundary.
_DEFAULT_RUNTIMES: Final = (None, "", "runc", "io.containerd.runc.v2")
_WILDCARDS: Final = ("", "0.0.0.0", "::")
# What the daemon masks and makes read-only in every container's /proc and /sys: a create that
# names its own lists (`--security-opt systempaths=unconfined` sends empty ones) is refused, and a
# container whose lists lack these is never started (a writable /proc/sys is the Docker VM's root).
_KEPT_MASKED: Final = ("/proc/kcore",)
_KEPT_READ_ONLY: Final = ("/proc/sys", "/proc/sysrq-trigger")


# ── the body, as Docker reads it ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Map:
    """A JSON object whose keys are data (Labels, DriverOpts): kept as spelled, two that differ only
    in case included; each value follows `values`."""

    values: object = None


@dataclass(frozen=True)
class _Each:
    """A JSON list whose items each follow `item`."""

    item: object = None


# The fields the policy reads or writes, each object's by Docker's exact spelling (a `dict`: field
# to its own schema; None is a value the policy does not look inside). Go matches a key to a field
# case-insensitively, so a key is rewritten to its field's spelling before anything is vetted.
_FREE: Final = _Map()
_DRIVER: Final = {"Name": None, "Options": _FREE}
_MOUNT: Final = {
    "Type": None,
    "Source": None,
    "Target": None,
    "ReadOnly": None,
    "VolumeOptions": {"Labels": _FREE, "DriverConfig": _DRIVER},
    "TmpfsOptions": {"SizeBytes": None, "Options": None},
}
_HOST: Final = {
    **dict.fromkeys(
        (
            "Privileged",
            *_NAMESPACES,
            *_ESCALATIONS,
            "SecurityOpt",
            "VolumeDriver",
            "Runtime",
            "VolumesFrom",
            "Links",
            "Binds",
            "ShmSize",
            "PublishAllPorts",
            "MaskedPaths",
            "ReadonlyPaths",
        )
    ),
    "Mounts": _Each(_MOUNT),
    "Tmpfs": _FREE,
    "Sysctls": _FREE,
    "StorageOpt": _FREE,
    "PortBindings": _Map(_Each({"HostIp": None, "HostPort": None})),
    "Annotations": _FREE,
    "LogConfig": {"Config": _FREE},
}
_ENDPOINT: Final = {"DriverOpts": _FREE}
_CONTAINER: Final = {
    "Labels": _FREE,
    "HostConfig": _HOST,
    "NetworkingConfig": {"EndpointsConfig": _Map(_ENDPOINT)},
    "Volumes": _FREE,
    "ExposedPorts": _FREE,
}
_EXEC: Final = {"Privileged": None, "Detach": None}
_VOLUME: Final = {"Name": None, "Driver": None, "DriverOpts": _FREE, "Labels": _FREE}
_NETWORK: Final = {
    "Name": None,
    "Labels": _FREE,
    "Options": _FREE,
    "IPAM": {"Options": _FREE, "Config": _Each({"AuxiliaryAddresses": _FREE})},
}
_CONNECT: Final = {"Container": None, "EndpointConfig": _ENDPOINT}
_DISCONNECT: Final = {"Container": None, "Force": None}


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str
    body: bytes | None = None  # a rewritten body, with labels injected
    target: str | None = None  # a rewritten request target (a list filtered to this frame)
    # The objects the request names, each `(kind, name)`, that must be this frame's - made through
    # its proxy and still wearing its label (`docker_proxy._Proxy._check`): kind is container,
    # container-start (vetted again as the daemon holds it, `recheck`), exec, network, volume or
    # volume-new (or absent: the proxy makes it).
    owned: tuple[tuple[str, str], ...] = ()
    raw: bool = False  # a hijacked stream: relayed raw once the daemon switches to it
    # What the request makes (container, network or volume): the proxy journals its id before the
    # client hears of it, and the helper acts at the frame's end on journaled ids only.
    creates: str | None = None


class _Refused(Exception):
    pass


class _Pairs(list[tuple[str, Any]]):
    """A JSON object as its keys and values in order, until `_canonical` knows what it is."""


def _pairs(pairs: list[tuple[str, Any]]) -> _Pairs:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Refused("the request body gives a key twice")
    return _Pairs(pairs)


def _canonical(value: object, schema: object, where: str) -> object:
    """`value` (parsed into `_Pairs`) as Docker reads it under `schema`: each object's keys in
    Docker's spelling. An object holding two keys that Docker would read as one is refused, but in a
    map whose keys are data; so is a key that is not ASCII where Docker's fields are."""
    if isinstance(value, list) and not isinstance(value, _Pairs):
        item = schema.item if isinstance(schema, _Each) else None
        return [_canonical(v, item, where) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(value, _Pairs):
        return value
    if isinstance(schema, _Map):
        return {k: _canonical(v, schema.values, k) for k, v in value}
    fields: dict[str, object] = schema if isinstance(schema, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    spelling = {name.casefold(): name for name in fields}
    out: dict[str, object] = {}
    seen: dict[str, str] = {}
    for key, item in value:
        if fields and not key.isascii():
            raise _Refused(f"{where} has a key that is not one of Docker's: {key!r}")
        if (fold := key.casefold()) in seen:
            raise _Refused(f"{where} gives {seen[fold]!r} and {key!r}, which Docker reads as one key")
        seen[fold] = key
        name = spelling.get(fold, key)
        out[name] = _canonical(item, fields.get(name), name)
    return out


@dataclass
class _Ask:
    method: str
    version: str  # the `/v1.NN` prefix as sent, or ""
    route: str
    query: str
    body: bytes
    ref: str
    task_id: str
    roots: tuple[str, ...]
    read_only: tuple[str, ...]
    schema: object = None  # the body's, for a route that reads one (`_Route.body`)
    protected: tuple[str, ...] = ()  # the git folders, pointer files, `.envrc`s a container never writes
    owned: list[tuple[str, str]] = field(default_factory=list)
    doc: dict[str, Any] | None = None  # the body as Docker reads it, once parsed

    @property
    def labels(self) -> dict[str, str]:
        return {TASK_LABEL: self.task_id, FRAME_LABEL: self.ref}

    def json(self) -> dict[str, Any]:
        """The body in Docker's spelling (`_canonical`): what the policy vets and edits, and what
        `ok` sends instead of the bytes the client sent."""
        if self.doc is not None:
            return self.doc
        try:
            value = json.loads(self.body or b"{}", object_pairs_hook=_pairs)
        except ValueError:
            raise _Refused(f"{self.route}: the request body is not JSON") from None
        if not isinstance(value, _Pairs):
            raise _Refused(f"{self.route}: the request body is not a JSON object")
        self.doc = _canonical(value, self.schema, "the request body")  # pyright: ignore[reportAttributeAccessIssue]
        return self.doc  # pyright: ignore[reportReturnType]

    def ok(self, **kw: Any) -> Verdict:
        data = None
        if self.schema is not None:  # a body read whole goes as the policy read it, never as sent
            try:
                data = json.dumps(self.json(), ensure_ascii=False).encode()
            except UnicodeEncodeError:  # a lone surrogate: Go would read another string
                raise _Refused(f"{self.route}: the request body holds text that is not Unicode") from None
        return Verdict(True, "", data, owned=tuple(dict.fromkeys(self.owned)), **kw)


def _dict(obj: dict[str, Any], key: str, *, put: bool = False) -> dict[str, Any]:
    """`obj[key]` as an object; absent or null is {}, which `put` sets so an edit lands in the body."""
    value = obj.get(key)
    if value is None:
        value = {}
        if put:
            obj[key] = value
    if not isinstance(value, dict):
        raise _Refused(f"{key} is not a JSON object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _list(obj: dict[str, Any], key: str) -> list[Any]:
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Refused(f"{key} is not a JSON list")
    return value  # pyright: ignore[reportUnknownVariableType]


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise _Refused(f"{what} is not a string")
    return value


def _within(path: str, folder: str) -> bool:
    """`path` is `folder` or inside it, both compared by their folder key (NFC, case-folded): the
    identity the grants and the Seatbelt fence use, so a folder the fence lets a command write is
    one a container may mount."""
    key, root = folder_key(path), folder_key(folder)
    return key == root or key.startswith(root.rstrip("/") + "/")


def _host_path(ask: _Ask, source: str, *, read_only: bool, what: str) -> None:
    """A host folder a container mounts: inside a granted folder after every link is resolved, never
    a socket, and read-only when it reaches into a read-only grant. Refusals name `what` (the
    container side), never `source`."""
    if not source.startswith("/"):
        raise _Refused(f"{what} needs an absolute host path")
    real = os.path.realpath(source)
    try:
        is_socket = stat.S_ISSOCK(os.stat(real).st_mode)
    except OSError:
        is_socket = False
    if is_socket:
        raise _Refused(f"{what} mounts a socket: mounting a Docker socket (or any socket) is not allowed")
    if any(
        unicodedata.normalize("NFC", os.path.basename(p)).casefold() == "docker.sock" for p in (source, real)
    ):
        raise _Refused(f"{what} mounts a Docker socket: that is not allowed")
    if not any(_within(real, root) for root in ask.roots):
        raise _Refused(f"{what} is outside your granted folders: only they can be mounted")
    if not read_only and any(_within(real, r) or _within(r, real) for r in ask.read_only):
        raise _Refused(f"{what} reaches a read-only grant: mount it read-only (`:ro`, `readonly`)")


def _overlays(ask: _Ask, rw: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """What the rw binds `rw` (host source, container target) need laid over them, each (a protected
    path, where it lands in the container). A container has no fence, so its rw bind of a repository
    reaches `.git/hooks`, which run later on the person's own git, and of a folder its `.envrc` or
    `.vscode`. A rw bind into a protected path has nothing to be laid over it: refused."""
    needed: list[tuple[str, str]] = []
    for source, target in rw:
        real = os.path.realpath(source)
        for git in ask.protected:
            if _within(real, git):
                raise _Refused(
                    f"the bind mount at {target} reaches what runs outside a container: mount it read-only"
                )
            if _within(git, real) and os.path.lexists(git):
                below = git.split("/")[len(real.split("/")) :]
                needed.append((git, posixpath.join(target.rstrip("/") or "/", *below)))
    return list(dict.fromkeys(needed))


def _read_only_overlays(ask: _Ask, host: dict[str, Any]) -> None:
    """Each overlay a create's rw binds need, as a read-only bind in its `Mounts`: a bind by `Mounts`
    fails if its source has gone, where one by `Binds` would have the daemon make it."""
    rw: list[tuple[str, str]] = []
    ro: set[tuple[str, str]] = set()
    for spec in _list(host, "Binds"):
        parts = _str(spec, "a bind").split(":")
        if len(parts) > 1 and parts[0].startswith("/"):
            read_only = len(parts) > 2 and "ro" in parts[2].split(",")
            (ro.add if read_only else rw.append)((parts[0], parts[1]))  # pyright: ignore[reportUnknownMemberType]
    mounts = _list(host, "Mounts")
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Type") == "bind":  # pyright: ignore[reportUnknownMemberType]
            entry = (str(mount.get("Source")), str(mount.get("Target")))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            (ro.add if mount.get("ReadOnly") else rw.append)(entry)  # pyright: ignore[reportUnknownMemberType]
    sent = {(os.path.realpath(source), target) for source, target in ro}
    added = [
        {"Type": "bind", "Source": git, "Target": target, "ReadOnly": True}
        for git, target in _overlays(ask, rw)
        if (os.path.realpath(git), target) not in sent
    ]
    if added:
        host["Mounts"] = [*mounts, *added]


def _plain_volume(driver: object, opts: dict[str, Any], what: str) -> None:
    """A volume is Docker's own local storage, nothing more: a `local` volume bound to a host folder
    (`type=none`, `o=bind`, `device=...`) is mounted by the daemon only when a container starts,
    after every check here, so a folder swapped for a link meanwhile would carry it out of the
    grants. A block device or a share is refused the same way."""
    if driver not in _LOCAL_DRIVERS:
        raise _Refused(f"{what} names a volume driver: only Docker's local volumes are allowed")
    flags = str(opts.get("o", "")).split(",")
    if "device" in opts or "bind" in flags or opts.get("type") == "none":
        raise _Refused(
            f"{what} is backed by a host folder, a device or a share, which is not allowed: "
            "use a bind mount instead (`-v /granted/folder:/path`)"
        )


def _tmpfs_size(text: str, what: str) -> int:
    m = _SIZE.match(text.strip())
    if m is None:
        raise _Refused(f"{what}'s size must be bytes or a k, m or g amount")
    return int(m[1]) * _UNITS[m[2].lower()]


def _tmpfs_cap(size: int, what: str, *, zero_ok: bool = False) -> None:
    """To Linux tmpfs a size of 0 is no limit at all, so 0 is refused where it would reach it."""
    if size < 0 or (size == 0 and not zero_ok):
        raise _Refused(f"{what} needs a size above 0: a tmpfs of size 0 has no limit")
    if size > TMPFS_MAX:
        raise _Refused(f"{what} is larger than {TMPFS_MAX // 1024**3} GiB, which is not allowed")


def _tmpfs_options(target: str, options: object) -> str:
    """A `HostConfig.Tmpfs` entry's options (`--tmpfs /run:size=64m`): sized above 0 and within the
    cap. Its size is given only as `size=`: `nr_blocks` (another spelling of it, 0 = no limit) and
    `nr_inodes=0` are refused."""
    what = f"the tmpfs at {target}"
    parts = [p for p in _str(options or "", what).split(",") if p]
    sizes: list[str] = []
    for part in parts:
        key, _, value = part.partition("=")
        if (key := key.strip().lower()) == "size":
            sizes.append(value)
        elif key == "nr_blocks":
            raise _Refused(f"{what}'s nr_blocks is not allowed: give its size with size=")
        elif key == "nr_inodes" and _tmpfs_size(value, f"{what}'s nr_inodes") == 0:
            raise _Refused(f"{what}'s nr_inodes is 0, which is no limit at all: give a number above 0")
    if not sizes:
        return ",".join([*parts, f"size={TMPFS_DEFAULT}"])
    for size in sizes:
        _tmpfs_cap(_tmpfs_size(size, what), what)
    return ",".join(parts)


def _bind(ask: _Ask, spec: object) -> None:
    parts = _str(spec, "a bind").split(":")
    if len(parts) == 1:
        return  # an anonymous volume
    source, target = parts[0], parts[1]
    if source.startswith("/"):
        modes = parts[2].split(",") if len(parts) > 2 else []
        _host_path(ask, source, read_only="ro" in modes, what=f"the bind mount at {target}")
    else:
        ask.owned.append(("volume-new", source))


def _mount(ask: _Ask, mount: object) -> None:
    if not isinstance(mount, dict):
        raise _Refused("a mount is not a JSON object")
    target = str(mount.get("Target", ""))  # pyright: ignore[reportUnknownArgumentType]
    kind = mount.get("Type")  # pyright: ignore[reportUnknownVariableType]
    if kind == "bind":
        _host_path(
            ask,
            _str(mount.get("Source"), f"the bind mount at {target}'s source"),  # pyright: ignore[reportUnknownArgumentType]
            read_only=bool(mount.get("ReadOnly")),  # pyright: ignore[reportUnknownArgumentType]
            what=f"the bind mount at {target}",
        )
    elif kind == "volume":
        opts = _dict(mount, "VolumeOptions", put=True)  # pyright: ignore[reportUnknownArgumentType]
        driver = _dict(opts, "DriverConfig")
        _plain_volume(driver.get("Name"), _dict(driver, "Options"), f"the volume at {target}")
        if source := mount.get("Source"):  # pyright: ignore[reportUnknownVariableType]
            ask.owned.append(("volume-new", _str(source, "a volume's name")))
        opts["Labels"] = {**_dict(opts, "Labels"), **ask.labels}  # what Docker makes for it is labelled
    elif kind == "tmpfs":
        what = f"the tmpfs at {target}"
        tmpfs = _dict(mount, "TmpfsOptions", put=True)  # pyright: ignore[reportUnknownArgumentType]
        if "SizeBytes" not in tmpfs or tmpfs["SizeBytes"] is None:
            tmpfs["SizeBytes"] = TMPFS_DEFAULT  # unset is no limit to Docker: held to the default
        if not isinstance(size := tmpfs["SizeBytes"], int):
            raise _Refused(f"{what}'s SizeBytes is not a number")
        _tmpfs_cap(size, what)
        for option in _list(tmpfs, "Options"):  # `[[key, value]]`, the API's own mount options
            key = str(option[0] if isinstance(option, list) and option else option).strip().lower()  # pyright: ignore[reportUnknownArgumentType]
            if key in ("size", "nr_blocks", "nr_inodes"):
                raise _Refused(f"{what}'s {key} option is not allowed: give its size with SizeBytes")
    elif kind != "image":
        raise _Refused(f"a mount of type {kind!r} is not allowed")


def _system_paths(host: dict[str, Any], *, inspected: bool) -> None:
    """A create names no masked or read-only paths of its own; the daemon's view keeps them."""
    if not inspected:
        for key in ("MaskedPaths", "ReadonlyPaths"):
            if host.get(key) is not None:
                raise _Refused(f"{key} is not allowed: a container keeps Docker's masked /proc and /sys")
        return
    for key, kept in (("MaskedPaths", _KEPT_MASKED), ("ReadonlyPaths", _KEPT_READ_ONLY)):
        if not set(kept) <= set(_list(host, key)):
            raise _Refused(f"{key} lacks Docker's own: a container keeps Docker's masked /proc and /sys")


def _namespaces(ask: _Ask, host: dict[str, Any]) -> None:
    """No namespace shared with the host; one shared with a container or a network is an object the
    frame must own."""
    for key in _NAMESPACES:
        mode = _str(host.get(key) or "", key)
        if mode.lower() == "host" or mode.lower().startswith("host:"):
            raise _Refused(f"{key} host is not allowed: a container shares no namespace with the host")
        if mode.startswith("container:") and key in _SHARED_NAMESPACES:
            ask.owned.append(("container", mode.removeprefix("container:")))
        elif key == "NetworkMode" and mode not in _PREDEFINED_NETWORKS:
            ask.owned.append(("network", mode))


def _confinement(host: dict[str, Any]) -> None:
    """No device, no added capability, and the confinement, volumes and runtime Docker gives."""
    for key in _ESCALATIONS:
        if host.get(key):
            raise _Refused(f"{key} is not allowed: a container gets no device and no added capability")
    for opt in _list(host, "SecurityOpt"):
        if _str(opt, "a SecurityOpt") not in SECURITY_OPTS:
            raise _Refused(f"SecurityOpt {opt!r} is not allowed: a container keeps its confinement")
    if host.get("VolumeDriver") not in _LOCAL_DRIVERS:
        raise _Refused("VolumeDriver is not allowed: volumes are Docker's local ones")
    if host.get("Runtime") not in _DEFAULT_RUNTIMES:
        raise _Refused("Runtime is not allowed: a container runs on Docker's default runtime")


def _host(ask: _Ask, host: dict[str, Any], *, inspected: bool = False) -> None:
    """A container's `HostConfig`: as asked for at its create, or (`inspected`) as the daemon holds
    it. Edits land in `host` (a tmpfs is sized), and the objects it names go in `ask.owned`."""
    if host.get("Privileged"):
        raise _Refused("a privileged container is not allowed")
    _namespaces(ask, host)
    _confinement(host)
    for entry in (*_list(host, "VolumesFrom"), *_list(host, "Links")):
        ask.owned.append(("container", _str(entry, "a container's name").split(":")[0].lstrip("/")))
    for bind in _list(host, "Binds"):
        _bind(ask, bind)
    for mount in _list(host, "Mounts"):
        _mount(ask, mount)
    if tmpfs := _dict(host, "Tmpfs"):
        host["Tmpfs"] = {target: _tmpfs_options(target, opts) for target, opts in tmpfs.items()}
    _published(host, inspected=inspected)
    if not inspected:  # the daemon's view is held to the same at its start (`recheck`)
        _read_only_overlays(ask, host)
    if not inspected:  # the daemon's view holds its own default size (OrbStack's is ~4 GiB)
        if not isinstance(shm := host.get("ShmSize") or 0, int):
            raise _Refused("ShmSize is not a number")
        _tmpfs_cap(shm, "ShmSize", zero_ok=True)  # 0 is the daemon's default
    _system_paths(host, inspected=inspected)


def _published(host: dict[str, Any], *, inspected: bool) -> None:
    """A published port stays on loopback, where the frame's window reaches it and the LAN does not:
    at a create the wildcard (or nothing) is rewritten to `127.0.0.1`; any other address outside
    loopback is refused, as is everything outside loopback in the daemon's view."""
    if host.get("PublishAllPorts"):
        raise _Refused("PublishAllPorts is not allowed: publish a port with -p 127.0.0.1:<port>:<port>")
    for port, bindings in _dict(host, "PortBindings").items():
        for binding in bindings or []:
            if not isinstance(binding, dict):
                raise _Refused(f"the binding of {port} is not a JSON object")
            address = _str(binding.get("HostIp") or "", "a HostIp")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            if address in _WILDCARDS and not inspected:
                binding["HostIp"] = "127.0.0.1"
            elif not _on_loopback(address):
                raise _Refused(f"{port} is published on {address or 'every address'}: it stays on loopback")


def _on_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _container_create(ask: _Ask, _m: re.Match[str]) -> Verdict:
    config = ask.json()
    _host(ask, _dict(config, "HostConfig"))
    for name in _dict(_dict(config, "NetworkingConfig"), "EndpointsConfig"):
        if name not in _PREDEFINED_NETWORKS:
            ask.owned.append(("network", name))
    config["Labels"] = {**_dict(config, "Labels"), **ask.labels}  # what the agent set never forges them
    return ask.ok(creates="container")


def _exec_create(ask: _Ask, m: re.Match[str]) -> Verdict:
    if ask.json().get("Privileged"):
        raise _Refused("a privileged exec is not allowed")
    ask.owned.append(("container", m["id"]))
    return ask.ok()


def _exec_start(ask: _Ask, m: re.Match[str]) -> Verdict:
    ask.owned.append(("exec", m["id"]))
    return ask.ok(raw=not ask.json().get("Detach"))


def _labelled_create(ask: _Ask, _m: re.Match[str]) -> Verdict:
    """A network or volume create: labelled. A volume is a plain local one (`_plain_volume`)."""
    body = ask.json()
    if ask.route == "/volumes/create":
        what = f"the volume {body.get('Name') or ''}".rstrip()
        _plain_volume(body.get("Driver"), _dict(body, "DriverOpts"), what)
    body["Labels"] = {**_dict(body, "Labels"), **ask.labels}
    return ask.ok(creates="volume" if ask.route == "/volumes/create" else "network")


def _network_connect(ask: _Ask, m: re.Match[str]) -> Verdict:
    ask.owned += [("network", m["id"]), ("container", _str(ask.json().get("Container"), "Container"))]
    return ask.ok()


def _labelled_list(ask: _Ask, _m: re.Match[str]) -> Verdict:
    """A list of containers, networks or volumes, or the event stream: only this frame's (Docker
    ANDs label filters)."""
    pairs = parse_qsl(ask.query, keep_blank_values=True)
    given = [v for k, v in pairs if k == "filters"]
    if len(given) > 1:
        raise _Refused("filters is given more than once")
    try:
        filters = json.loads(given[0]) if given else {}
    except ValueError:
        raise _Refused("filters is not JSON") from None
    if not isinstance(filters, dict):
        raise _Refused("filters is not a JSON object")
    mine = f"{FRAME_LABEL}={ask.ref}"
    label = filters.get("label")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if label is None:
        filters["label"] = [mine]
    elif isinstance(label, list):
        label.append(mine)  # pyright: ignore[reportUnknownMemberType]
    elif isinstance(label, dict):
        label[mine] = True
    else:
        raise _Refused("the label filter is neither a list nor an object")
    query = urlencode([*((k, v) for k, v in pairs if k != "filters"), ("filters", json.dumps(filters))])
    return ask.ok(target=f"{ask.version}{ask.route}?{query}")


def _pull(ask: _Ask, _m: re.Match[str]) -> Verdict:
    query = dict(parse_qsl(ask.query, keep_blank_values=True))
    if "fromSrc" in query or not query.get("fromImage"):
        raise _Refused("an image import is not allowed: images/create passes only a pull (fromImage)")
    return ask.ok()


def _plain(ask: _Ask, _m: re.Match[str]) -> Verdict:
    return ask.ok()


def _stream(ask: _Ask, _m: re.Match[str]) -> Verdict:
    return ask.ok(raw=True)


def _object(kind: str, *, raw: bool = False) -> Callable[[_Ask, re.Match[str]], Verdict]:
    def vet(ask: _Ask, m: re.Match[str]) -> Verdict:
        ask.owned.append((kind, m["id"]))
        return ask.ok(raw=raw)

    return vet


@dataclass(frozen=True)
class _Route:
    methods: frozenset[str]
    pattern: re.Pattern[str]
    vet: Callable[[_Ask, re.Match[str]], Verdict]
    body: object = None  # the schema of a body read whole and vetted; otherwise it streams


def _r(
    methods: str, pattern: str, vet: Callable[[_Ask, re.Match[str]], Verdict], *, body: object = None
) -> _Route:
    return _Route(frozenset(methods.split()), re.compile(f"^{pattern}$"), vet, body)


_ID = r"(?P<id>[^/]+)"
_ROUTES: Final = (
    _r("GET HEAD", r"/_ping", _plain),
    _r("GET", r"/version", _plain),
    _r("GET", r"/info", _plain),
    _r("GET", r"/events", _labelled_list),
    _r("GET", r"/images/json", _plain),
    _r("POST", r"/images/create", _pull),
    _r("GET", r"/images/.+/json", _plain),
    _r("POST", r"/build", _plain),
    _r("POST", r"/(?:session|grpc)", _stream),  # BuildKit, for `docker build` and `compose build`
    _r("POST", r"/containers/create", _container_create, body=_CONTAINER),
    _r("GET", r"/containers/json", _labelled_list),
    _r("POST", rf"/containers/{_ID}/start", _object("container-start")),
    _r("POST", rf"/containers/{_ID}/(?:stop|kill|wait|resize|rename)", _object("container")),
    _r("POST", rf"/containers/{_ID}/attach", _object("container", raw=True)),
    _r("GET", rf"/containers/{_ID}/(?:logs|json)", _object("container")),
    _r("DELETE", rf"/containers/{_ID}", _object("container")),
    _r("POST", rf"/containers/{_ID}/exec", _exec_create, body=_EXEC),
    _r("POST", rf"/exec/{_ID}/start", _exec_start, body=_EXEC),
    _r("POST", rf"/exec/{_ID}/resize", _object("exec")),
    _r("GET", rf"/exec/{_ID}/json", _object("exec")),
    _r("POST", r"/networks/create", _labelled_create, body=_NETWORK),
    _r("GET", r"/networks", _labelled_list),
    _r("GET DELETE", rf"/networks/{_ID}", _object("network")),
    _r("POST", rf"/networks/{_ID}/connect", _network_connect, body=_CONNECT),
    _r("POST", rf"/networks/{_ID}/disconnect", _network_connect, body=_DISCONNECT),
    _r("POST", r"/volumes/create", _labelled_create, body=_VOLUME),
    _r("GET", r"/volumes", _labelled_list),
    _r("GET DELETE", rf"/volumes/{_ID}", _object("volume")),
)


def _split(path: str) -> tuple[str, str, str]:
    """A request target as (version prefix, route, query); `_Refused` for one the proxy will not
    read: not origin-form, or a path with an escape or a dot segment."""
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        raise _Refused("the request target is not a path")
    if "%" in parts.path or any(seg in (".", "..") for seg in parts.path.split("/")):
        raise _Refused("a request path with an escape or a dot segment is not allowed")
    version = m.group(0) if (m := _VERSION.match(parts.path)) else ""
    return version, parts.path[len(version) :], parts.query


def _route_of(method: str, route: str) -> tuple[_Route, re.Match[str]] | None:
    for entry in _ROUTES:
        if method in entry.methods and (m := entry.pattern.match(route)):
            return entry, m
    return None


def reads_body(method: str, target: str) -> bool:
    """Whether the proxy reads this request's body whole to vet it (a create); otherwise it streams."""
    try:
        found = _route_of(method, _split(target)[1])
    except _Refused:
        return False
    return found is not None and found[0].body is not None


def vet_request(
    method: str,
    path: str,
    body: bytes,
    *,
    ref: str,
    task_id: str,
    roots: Sequence[str],
    read_only: Sequence[str] = (),
    protected: Sequence[str] = (),
) -> Verdict:
    """The proxy's policy for one request: allowed (with the body or target to send instead, and
    the objects to prove this frame's) or refused, with why. `roots` are the real paths a container
    may mount; `read_only` those of them it mounts only read-only; `protected` the paths a rw bind
    holding one gets laid over it, read-only: git folders and pointer files, `Plants.later`'s."""
    try:
        version, route, query = _split(path)
        found = _route_of(method.upper(), route)
        if found is None:
            raise _Refused(
                f"{method.upper()} {route} is not allowed through Prismoid's Docker proxy: it passes images, "
                "builds, containers, exec, networks and volumes, and only this task's own"
            )
        entry, m = found
        ask = _Ask(
            method.upper(),
            version,
            route,
            query,
            body,
            ref,
            task_id,
            tuple(roots),
            tuple(read_only),
            entry.body,
            tuple(protected),
        )
        return entry.vet(ask, m)
    except _Refused as e:
        return Verdict(False, str(e))


def _resolved_mounts(ask: _Ask, container: dict[str, Any], name: str) -> None:
    """Every mount the daemon resolved, as `recheck` says."""
    rw: list[tuple[str, str]] = []
    overlaid: set[tuple[str, str]] = set()
    for mount in _list(container, "Mounts"):
        if not isinstance(mount, dict):
            raise _Refused(f"container {name} has a mount that is not a JSON object")
        kind, where = mount.get("Type"), mount.get("Destination", "")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        what = f"container {name}'s {kind} mount at {where}"
        if kind == "bind":
            source = _str(mount.get("Source"), what)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            _host_path(ask, source, read_only=not mount.get("RW", True), what=what)  # pyright: ignore[reportUnknownMemberType]
            if mount.get("RW", True):  # pyright: ignore[reportUnknownMemberType]
                rw.append((source, _str(where, what)))
            else:
                overlaid.add((os.path.realpath(source), _str(where, what)))
        elif kind == "volume":
            ask.owned.append(("volume", _str(mount.get("Name"), what)))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        elif kind not in ("tmpfs", "image"):
            raise _Refused(f"{what} is not allowed")
    for git, target in _overlays(ask, rw):
        if (os.path.realpath(git), target) not in overlaid:
            raise _Refused(f"container {name}'s {target} runs outside a container: it must be read-only")


def recheck(
    container: dict[str, Any],
    name: str,
    *,
    roots: Sequence[str],
    read_only: Sequence[str] = (),
    protected: Sequence[str] = (),
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """The daemon's own view of a container (its inspect document), vetted again at its start: its
    `HostConfig` as a create's, and every mount it resolved - a bind by its real path now (a folder
    vetted at create may have become a link out), a volume as one this frame must own - and each rw
    bind's read-only overlays there (`_overlays`). Returns why it is refused (or None), and the
    objects (`(kind, name)`) it reaches that must be the frame's."""
    ask = _Ask(
        "POST",
        "",
        "/containers/start",
        "",
        b"",
        "",
        "",
        tuple(roots),
        tuple(read_only),
        None,
        tuple(protected),
    )
    try:
        if not isinstance(host := container.get("HostConfig") or {}, dict):
            raise _Refused(f"container {name}'s HostConfig is not a JSON object")
        _host(ask, host, inspected=True)  # pyright: ignore[reportUnknownArgumentType]
        _resolved_mounts(ask, container, name)
    except _Refused as e:
        return str(e), ()
    owned = (("volume", n) if k == "volume-new" else (k, n) for k, n in ask.owned)  # nothing is made now
    return None, tuple(dict.fromkeys(owned))
