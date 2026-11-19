# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker proxy's policy: `docker_policy` on the bodies Docker clients send.

Pure: no daemon. What needs the daemon (an object's labels, the relay itself) is in
tests/integration/device/test_docker_proxy.py. The rig is `tests/support/rigs/docker_policy_rig.py`; the overlay a
repository's `.git` gets is `tests/unit/device/test_docker_policy_overlay.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.docker_policy import (
    FRAME_LABEL,
    TASK_LABEL,
    TMPFS_DEFAULT,
    Verdict,
    recheck,
    vet_request,
)
from tests.support.rigs.docker_policy_rig import REF, TASK, create, inspected, refused, vet
from tests.support.rigs.docker_policy_rig import root as root


def test_bind_inside_root_allowed_and_labelled(root: Path) -> None:
    verdict = create({"Binds": [f"{root}/data:/data", "cache:/cache"]}, root, Labels={"app": "web"})
    assert verdict.allow, verdict.reason
    assert verdict.body is not None
    sent = json.loads(verdict.body)
    assert sent["Labels"] == {"app": "web", TASK_LABEL: TASK, FRAME_LABEL: REF}
    assert sent["HostConfig"]["Binds"] == [f"{root}/data:/data", "cache:/cache"], (
        "the binds are forwarded as given"
    )
    assert ("volume-new", "cache") in verdict.owned, "a named volume is this frame's, or made labelled"


def test_labels_cannot_be_forged(root: Path) -> None:
    verdict = create({}, root, Labels={FRAME_LABEL: "device-other", TASK_LABEL: "other"})
    assert verdict.body is not None
    assert json.loads(verdict.body)["Labels"] == {TASK_LABEL: TASK, FRAME_LABEL: REF}


def test_bind_home_refused(root: Path, tmp_path: Path) -> None:
    home = os.path.expanduser("~")
    for host in (
        {"Binds": [f"{home}:/h"]},
        {"Binds": [f"{root}/../..:/up"]},
        {"Mounts": [{"Type": "bind", "Source": home, "Target": "/h"}]},
    ):
        verdict = create(host, root)
        refused(verdict, "outside your granted folders")
        assert home not in verdict.reason and str(tmp_path) not in verdict.reason, "no real path in a refusal"


def test_bind_through_a_link_out_of_the_root_refused(root: Path, tmp_path: Path) -> None:
    (root / "out").symlink_to(tmp_path)
    refused(create({"Binds": [f"{root}/out:/o"]}, root), "outside your granted folders")


def test_read_only_grant_binds_read_only(root: Path, tmp_path: Path) -> None:
    specs = Path(os.path.realpath(tmp_path)) / "specs"
    specs.mkdir()
    ro = (specs,)
    refused(
        vet("POST", "/containers/create", {"HostConfig": {"Binds": [f"{specs}:/s"]}}, root, read_only=ro),
        "read-only",
    )
    mount = {"Type": "bind", "Source": str(specs), "Target": "/s"}
    refused(
        vet("POST", "/containers/create", {"HostConfig": {"Mounts": [mount]}}, root, read_only=ro),
        "read-only",
    )
    assert vet(
        "POST", "/containers/create", {"HostConfig": {"Binds": [f"{specs}:/s:ro"]}}, root, read_only=ro
    ).allow
    ok = {**mount, "ReadOnly": True}
    assert vet("POST", "/containers/create", {"HostConfig": {"Mounts": [ok]}}, root, read_only=ro).allow


def test_privileged_refused(root: Path) -> None:
    refused(create({"Privileged": True}, root), "privileged")


@pytest.mark.parametrize(
    "key", ["NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"]
)
def test_host_network_refused(root: Path, key: str) -> None:
    refused(create({key: "host"}, root), key, "host")


@pytest.mark.parametrize(
    ("host", "word"),
    [
        ({"Devices": [{"PathOnHost": "/dev/kvm", "PathInContainer": "/dev/kvm"}]}, "Devices"),
        ({"CapAdd": ["SYS_ADMIN"]}, "CapAdd"),
        ({"SecurityOpt": ["seccomp=unconfined"]}, "unconfined"),
        ({"SecurityOpt": ["apparmor:unconfined"]}, "unconfined"),
    ],
)
def test_escalations_refused(root: Path, host: dict[str, Any], word: str) -> None:
    refused(create(host, root), word)


@pytest.mark.parametrize(
    "opt",
    [
        'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}',  # an allow-all profile, by value
        "seccomp=/tmp/allow-all.json",
        "apparmor=my-profile",
        "label=disable",
        "no-new-privileges=false",
        "writable-cgroups=true",
    ],
)
def test_a_security_opt_not_on_the_list_is_refused(root: Path, opt: str) -> None:
    refused(create({"SecurityOpt": [opt]}, root), "SecurityOpt")
    why, _ = recheck(inspected({"SecurityOpt": [opt]}), "web", roots=[str(root)])
    assert why is not None and "SecurityOpt" in why, why


@pytest.mark.parametrize(
    "opt", ["no-new-privileges", "no-new-privileges=true", "no-new-privileges:true", "seccomp=builtin"]
)
def test_a_security_opt_that_keeps_the_confinement_passes(root: Path, opt: str) -> None:
    assert create({"SecurityOpt": [opt]}, root).allow
    assert recheck(inspected({"SecurityOpt": [opt]}), "web", roots=[str(root)])[0] is None


def test_docker_sock_mount_refused(root: Path) -> None:
    (root / "docker.sock").touch()
    for host in (
        {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
        {"Binds": [f"{root}/docker.sock:/var/run/docker.sock"]},
        {"Mounts": [{"Type": "bind", "Source": f"{root}/docker.sock", "Target": "/d.sock"}]},
    ):
        refused(create(host, root), "Docker socket")


def test_other_containers_and_networks_must_be_this_frames(root: Path) -> None:
    verdict = create(
        {
            "VolumesFrom": ["db:ro"],
            "NetworkMode": "demo_default",
            "PidMode": "container:db",
            "Links": ["cache:c"],
        },
        root,
        NetworkingConfig={"EndpointsConfig": {"demo_default": {}, "bridge": {}}},
    )
    assert verdict.allow, verdict.reason
    assert set(verdict.owned) == {("container", "db"), ("container", "cache"), ("network", "demo_default")}


def test_volume_mounts(root: Path) -> None:
    mount: dict[str, Any] = {"Type": "volume", "Source": "pg", "Target": "/v"}
    verdict = create({"Mounts": [mount]}, root)
    assert verdict.allow, verdict.reason
    assert ("volume-new", "pg") in verdict.owned
    assert verdict.body is not None
    labels = json.loads(verdict.body)["HostConfig"]["Mounts"][0]["VolumeOptions"]["Labels"]
    assert labels == {TASK_LABEL: TASK, FRAME_LABEL: REF}, "a volume Docker makes for the mount is labelled"


BOUND = (
    {"type": "none", "o": "bind", "device": "<root>/data"},  # bound inside a grant: still refused
    {"o": "bind", "device": "/etc"},
    {"type": "none", "device": "<root>"},
    {"type": "nfs", "o": "addr=10.0.0.1", "device": ":/export"},
    {"type": "ext4", "device": "/dev/sda1"},
)


@pytest.mark.parametrize("opts", BOUND)
def test_volume_backed_by_a_folder_refused(root: Path, opts: dict[str, str]) -> None:
    """The daemon binds such a volume only when a container starts, past every check:
    a folder inside a grant swapped for a link to `~` after the create would be mounted. A bind
    mount, vetted again at start, is the only way to a host folder."""
    opts = {k: v.replace("<root>", str(root)) for k, v in opts.items()}
    created = vet("POST", "/volumes/create", {"Name": "x", "DriverOpts": opts}, root)
    refused(created, "use", "bind mount instead")
    assert str(root) not in created.reason and "/etc" not in created.reason, "no real path in a refusal"
    mount = {
        "Type": "volume",
        "Source": "x",
        "Target": "/v",
        "VolumeOptions": {"DriverConfig": {"Options": opts}},
    }
    refused(create({"Mounts": [mount]}, root), "the volume at /v", "bind mount instead")


def test_volumes_are_local(root: Path) -> None:
    refused(vet("POST", "/volumes/create", {"Name": "x", "Driver": "rexray"}, root), "volume driver")
    mount = {
        "Type": "volume",
        "Source": "x",
        "Target": "/v",
        "VolumeOptions": {"DriverConfig": {"Name": "rexray"}},
    }
    refused(create({"Mounts": [mount]}, root), "volume driver")
    verdict = vet(
        "POST", "/volumes/create", {"Name": "x", "Driver": "local", "DriverOpts": {"size": "1g"}}, root
    )
    assert verdict.allow and verdict.body is not None
    assert json.loads(verdict.body)["Labels"] == {TASK_LABEL: TASK, FRAME_LABEL: REF}


def test_tmpfs_is_capped(root: Path) -> None:
    verdict = create({"Tmpfs": {"/run": "", "/cache": "rw,size=64m"}}, root)
    assert verdict.allow and verdict.body is not None
    assert json.loads(verdict.body)["HostConfig"]["Tmpfs"] == {
        "/run": f"size={TMPFS_DEFAULT}",
        "/cache": "rw,size=64m",
    }
    refused(create({"Tmpfs": {"/run": "size=3g"}}, root), "the tmpfs at /run", "larger than 2 GiB")
    refused(create({"Tmpfs": {"/run": "size=50%"}}, root), "size must be")
    mount = {"Type": "tmpfs", "Target": "/t"}
    sized = create({"Mounts": [mount]}, root)
    assert sized.body is not None
    assert json.loads(sized.body)["HostConfig"]["Mounts"][0]["TmpfsOptions"]["SizeBytes"] == TMPFS_DEFAULT
    refused(
        create({"Mounts": [{**mount, "TmpfsOptions": {"SizeBytes": 3 * 1024**3}}]}, root), "larger than 2 GiB"
    )
    refused(create({"ShmSize": 4 * 1024**3}, root), "ShmSize", "larger than 2 GiB")
    assert create({"ShmSize": 0}, root).allow, "ShmSize 0 is Docker's 64 MiB default, not unlimited"


@pytest.mark.parametrize(
    "options",
    [
        "size=0",
        "size=0m",
        "rw,size=1m,size=0",
        "size=1m,nr_blocks=0",
        "nr_blocks=1000",
        "size=1m,nr_inodes=0",
        "size=1m,nr_inodes=0k",
        "size=1m,nr_inodes=00",
        "size=1m,nr_inodes=0x0",
        "size=0x0",
    ],
)
def test_tmpfs_with_no_limit_refused(root: Path, options: str) -> None:
    """Review N1: to Linux tmpfs a size (or nr_blocks) of 0 is no limit at all."""
    refused(create({"Tmpfs": {"/run": options}}, root), "the tmpfs at /run")


def test_tmpfs_mount_with_no_limit_refused(root: Path) -> None:
    mount: dict[str, Any] = {"Type": "tmpfs", "Target": "/t"}
    for options in (
        {"SizeBytes": 0},
        {"SizeBytes": -1},
        {"SizeBytes": 1024, "Options": [["nr_blocks", "0"]]},
    ):
        refused(create({"Mounts": [{**mount, "TmpfsOptions": options}]}, root), "the tmpfs at /t")
    sized = create(
        {"Mounts": [{**mount, "TmpfsOptions": {"SizeBytes": 1024, "Options": [["mode", "1777"]]}}]}, root
    )
    assert sized.allow, sized.reason


def test_folders_compare_as_the_fence_does(root: Path) -> None:
    """By the grants' folder key (NFC, case-folded): a spelling APFS takes as the same folder binds."""
    upper = str(root).upper()
    assert create({"Binds": [f"{upper}/data:/data"]}, root).allow
    accented = root / "caf\u00e9"
    accented.mkdir()
    decomposed = str(root) + "/cafe\u0301"
    assert create({"Binds": [f"{decomposed}:/c"]}, accented).allow


def test_start_rechecks_binds(root: Path, tmp_path: Path) -> None:
    data = {"Type": "bind", "Source": str(root / "data"), "Destination": "/data", "RW": True}
    assert recheck(inspected(None, data), "web", roots=[str(root)]) == (None, ())
    (root / "out").symlink_to(tmp_path)
    out = {"Type": "bind", "Source": str(root / "out"), "Destination": "/o", "RW": True}
    why, _ = recheck(inspected(None, out), "web", roots=[str(root)])
    assert why is not None and "outside your granted folders" in why and str(tmp_path) not in why


@pytest.mark.parametrize(
    ("host", "word"),
    [
        ({"Privileged": True}, "privileged"),
        ({"PidMode": "host"}, "PidMode"),
        ({"NetworkMode": "host"}, "NetworkMode"),
        ({"IpcMode": "host"}, "IpcMode"),
        ({"UTSMode": "host"}, "UTSMode"),
        ({"UsernsMode": "host"}, "UsernsMode"),
        ({"CgroupnsMode": "host"}, "CgroupnsMode"),
        ({"Devices": [{"PathOnHost": "/dev/kmsg"}]}, "Devices"),
        ({"CapAdd": ["SYS_ADMIN"]}, "CapAdd"),
        ({"SecurityOpt": ["seccomp=unconfined"]}, "unconfined"),
        ({"Tmpfs": {"/t": "size=0"}}, "the tmpfs at /t"),
        ({"MaskedPaths": []}, "MaskedPaths"),
        ({"ReadonlyPaths": ["/proc/bus"]}, "ReadonlyPaths"),
    ],
)
def test_start_revets_the_daemons_view(root: Path, host: dict[str, Any], word: str) -> None:
    """Review C1, in depth: whatever a create got past the policy, the container the daemon holds is
    vetted again before it starts."""
    why, _ = recheck(inspected(host), "web", roots=[str(root)])
    assert why is not None and word in why, why


def test_start_names_every_volume_it_mounts(root: Path) -> None:
    pg = {"Type": "volume", "Name": "owners_pgdata", "Destination": "/v", "RW": True}
    why, owned = recheck(inspected({"Binds": ["cache:/c"]}, pg), "web", roots=[str(root)])
    assert why is None and set(owned) == {("volume", "owners_pgdata"), ("volume", "cache")}, (
        "each must be this frame's: none is made at a start"
    )
    why, _ = recheck(inspected(None, {"Type": "npipe", "Destination": "/p"}), "web", roots=[str(root)])
    assert why is not None


RAW_CREATE = "/v1.47/containers/create"


def raw(path: str, body: bytes, root: Path) -> Verdict:
    return vet_request("POST", path, body, ref=REF, task_id=TASK, roots=[str(root)])


@pytest.mark.parametrize(
    "body",
    [
        {"Image": "alpine", "hostconfig": {"Privileged": True}},
        {"Image": "alpine", "HostConfig": {"privileged": True}},
        {"Image": "alpine", "HOSTCONFIG": {"PIDMODE": "host"}},
        {"Image": "alpine", "hostconfig": {"capadd": ["ALL"]}},
        {"Image": "alpine", "hostconfig": {"devices": [{"PathOnHost": "/dev/kmsg"}]}},
        {"Image": "alpine", "hostconfig": {"securityopt": ["seccomp=unconfined"]}},
        {"Image": "alpine", "hostconfig": {"binds": ["/etc:/h"]}},
        {"Image": "alpine", "hostconfig": {"mounts": [{"type": "bind", "source": "/etc", "target": "/h"}]}},
        {"Image": "alpine", "hostconfig": {"tmpfs": {"/t": "size=0"}}},
        {"Image": "alpine", "hostconfig": {"maskedpaths": []}},
        {"Image": "alpine", "Ho\u017ftConfig": {"Privileged": True}},  # Go folds the long s to s
    ],
)
def test_keys_are_read_as_docker_reads_them(root: Path, body: dict[str, Any]) -> None:
    """Review C1: Go's `encoding/json` matches a key to a field in any case, so `hostconfig` is
    `HostConfig` to the daemon: the policy reads it so too."""
    refused(raw(RAW_CREATE, json.dumps(body).encode(), root))


def test_a_lowercase_mount_of_an_owners_volume_is_vetted(root: Path) -> None:
    """Review C1 / hunt: `hostconfig.mounts` naming the owner's volume must reach the ownership check."""
    for host in (
        {"mounts": [{"type": "volume", "source": "owners_pgdata", "target": "/v"}]},
        {"binds": ["owners_pgdata:/v"]},
    ):
        verdict = raw(RAW_CREATE, json.dumps({"Image": "alpine", "hostconfig": host}).encode(), root)
        assert verdict.allow and ("volume-new", "owners_pgdata") in verdict.owned, verdict
        assert verdict.body is not None
        sent = json.loads(verdict.body)
        assert "hostconfig" not in sent and sent["HostConfig"], "sent in Docker's own spelling"
    mount = json.loads(verdict.body)["HostConfig"]  # pyright: ignore[reportPossiblyUnbound]
    assert mount == {"Binds": ["owners_pgdata:/v"]}


def test_a_lowercase_labels_never_forges_them(root: Path) -> None:
    """Review N1: a second spelling of `Labels` after the injected one would win in Go."""
    forged = {"prismoid.frame": "device-sibling", "prismoid.task": "sibling"}
    for path, rest in (
        (RAW_CREATE, '"Image": "alpine"'),
        ("/volumes/create", '"Name": "pgdata"'),
        ("/networks/create", '"Name": "demo_default"'),
    ):
        both = f'{{{rest}, "Labels": {{}}, "labels": {json.dumps(forged)}}}'.encode()
        refused(raw(path, both, root), "'Labels' and 'labels'", "one key")
        alone = raw(path, f'{{{rest}, "labels": {json.dumps(forged)}}}'.encode(), root)
        assert alone.allow and alone.body is not None, alone
        sent = json.loads(alone.body)
        assert "labels" not in sent and sent["Labels"] == {TASK_LABEL: TASK, FRAME_LABEL: REF}, path


def test_a_lowercase_volume_name_is_the_name(root: Path) -> None:
    """Review I1: `{"name": ...}` is Docker's `Name`; the proxy's existing-volume check reads the
    policy's encoding, so it sees it."""
    verdict = raw("/volumes/create", b'{"name": "owners_pgdata", "driveropts": {"size": "1g"}}', root)
    assert verdict.allow and verdict.body is not None
    assert json.loads(verdict.body) == {
        "Name": "owners_pgdata",
        "DriverOpts": {"size": "1g"},
        "Labels": {TASK_LABEL: TASK, FRAME_LABEL: REF},
    }
    refused(raw("/volumes/create", b'{"name": "x", "driveropts": {"type": "none", "device": "/"}}', root))
    refused(raw("/volumes/create", b'{"name": "x", "driver": "rexray"}', root), "volume driver")


@pytest.mark.parametrize(
    "body",
    [
        b'{"Image": "alpine", "Image": "busybox"}',
        b'{"Image": "alpine", "Labels": {"a": "1", "a": "2"}}',
        b'{"Image": "alpine", "HostConfig": {"Privileged": false, "privileged": true}}',
        b'{"Image": "alpine", "HostConfig": {"Mounts": [{"Type": "tmpfs", "type": "volume", "Target": "/t"}]}}',
        b'{"Image": "alpine", "Healthcheck": {"Test": ["NONE"], "test": ["CMD", "x"]}}',
        b'{"Image": "alpine", "NetworkingConfig": {"EndpointsConfig": {"n": {"DriverOpts": {}, "driveropts": {}}}}}',
    ],
)
def test_duplicate_keys_refused(root: Path, body: bytes) -> None:
    refused(raw(RAW_CREATE, body, root))


def test_keys_that_are_data_are_kept_as_spelled(root: Path) -> None:
    """In a map whose keys are data (Labels, Tmpfs, Sysctls, EndpointsConfig...), two keys differing
    only in case are two entries, kept as they are."""
    sysctls = {"net.ipv4.ip_forward": "1", "NET.IPV4.IP_FORWARD": "1"}
    body: dict[str, Any] = {
        "Image": "alpine",
        "Labels": {"App": "1", "app": "2"},
        "HostConfig": {
            "Tmpfs": {"/Run": "size=1m", "/run": "size=2m"},
            "Sysctls": sysctls,
        },
        "NetworkingConfig": {"EndpointsConfig": {"Net": {}, "net": {}}},
    }
    verdict = vet("POST", RAW_CREATE, body, root)
    assert verdict.allow and verdict.body is not None, verdict
    sent = json.loads(verdict.body)
    assert sent["Labels"] == {"App": "1", "app": "2", TASK_LABEL: TASK, FRAME_LABEL: REF}
    assert sent["HostConfig"]["Tmpfs"] == {"/Run": "size=1m", "/run": "size=2m"}
    assert sent["HostConfig"]["Sysctls"] == sysctls
    assert {("network", "Net"), ("network", "net")} <= set(verdict.owned)
    opts = vet("POST", "/volumes/create", {"Name": "v", "DriverOpts": {"Size": "1", "size": "2"}}, root)
    assert opts.allow and opts.body is not None
    assert json.loads(opts.body)["DriverOpts"] == {"Size": "1", "size": "2"}


def test_the_body_sent_is_the_policys_own(root: Path) -> None:
    verdict = raw(RAW_CREATE, b'{"Image":"alp\\u0069ne",   "hostconfig":{"binds":[]}}', root)
    assert (
        verdict.body
        == json.dumps(
            {"Image": "alpine", "HostConfig": {"Binds": []}, "Labels": {TASK_LABEL: TASK, FRAME_LABEL: REF}},
            ensure_ascii=False,
        ).encode()
    )
    refused(raw(RAW_CREATE, b'{"Image": "\\ud800"}', root), "not Unicode")
    for path, body in (
        ("/v1.47/containers/abc/exec", b'{"privileged": false, "Cmd": ["sh"]}'),
        ("/v1.47/exec/e1/start", b'{"detach": true}'),
        ("/v1.47/networks/n1/connect", b'{"container": "abc"}'),
    ):
        verdict = raw(path, body, root)
        assert verdict.body is not None and verdict.body != body, path
    refused(raw("/v1.47/containers/abc/exec", b'{"privileged": true}', root), "privileged exec")
    assert not raw("/v1.47/exec/e1/start", b'{"detach": true}', root).raw, "Detach read in any case"
    assert ("container", "abc") in raw("/v1.47/networks/n1/connect", b'{"container": "abc"}', root).owned


def test_system_paths_and_device_requests_refused(root: Path) -> None:
    """`--security-opt systempaths=unconfined` sends empty MaskedPaths and ReadonlyPaths: a writable
    /proc/sys (core_pattern) is the Docker VM's root."""
    refused(create({"MaskedPaths": [], "ReadonlyPaths": []}, root), "MaskedPaths")
    refused(create({"ReadonlyPaths": ["/proc/sys"]}, root), "ReadonlyPaths")
    refused(create({"DeviceRequests": [{"Count": -1, "Capabilities": [["gpu"]]}]}, root), "DeviceRequests")


def test_compose_create_network_labelled(root: Path) -> None:
    body = {"Name": "demo_default", "Driver": "bridge", "Labels": {"com.docker.compose.project": "demo"}}
    verdict = vet("POST", "/v1.47/networks/create", body, root)
    assert verdict.allow and verdict.body is not None
    assert json.loads(verdict.body)["Labels"] == {
        "com.docker.compose.project": "demo",
        TASK_LABEL: TASK,
        FRAME_LABEL: REF,
    }


def test_lists_are_filtered_to_this_frame(root: Path) -> None:
    compose = json.dumps({"label": ["com.docker.compose.project=demo"]})
    verdict = vet("GET", f"/v1.47/containers/json?all=1&filters={compose}", None, root)
    assert verdict.allow and verdict.target is not None
    assert verdict.target.startswith("/v1.47/containers/json?")
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(verdict.target).query)
    assert query["all"] == ["1"]
    assert json.loads(query["filters"][0]) == {
        "label": ["com.docker.compose.project=demo", f"{FRAME_LABEL}={REF}"]
    }
    for path in ("/networks", "/v1.47/volumes"):
        listed = vet("GET", path, None, root)
        assert listed.target is not None and json.loads(
            parse_qs(urlsplit(listed.target).query)["filters"][0]
        ) == {"label": [f"{FRAME_LABEL}={REF}"]}
    assert vet("GET", "/v1.47/images/json", None, root).target is None, "images are not filtered"


def test_actions_on_an_object_need_its_label(root: Path) -> None:
    for method, path, owned in (
        ("POST", "/v1.47/containers/abc/start", ("container-start", "abc")),
        ("POST", "/containers/abc/stop?t=10", ("container", "abc")),
        ("DELETE", "/v1.47/containers/abc?force=1", ("container", "abc")),
        ("GET", "/containers/abc/json", ("container", "abc")),
        ("GET", "/containers/abc/logs?follow=1", ("container", "abc")),
        ("POST", "/exec/e1/start", ("exec", "e1")),
        ("GET", "/v1.47/networks/demo_default", ("network", "demo_default")),
        ("DELETE", "/networks/demo_default", ("network", "demo_default")),
        ("GET", "/volumes/pg", ("volume", "pg")),
        ("DELETE", "/volumes/pg", ("volume", "pg")),
    ):
        verdict = vet(method, path, {} if method == "POST" and "exec" in path else None, root)
        assert verdict.allow and owned in verdict.owned, (path, verdict)
    attach = vet("POST", "/v1.47/containers/abc/attach?stream=1&stdout=1", None, root)
    assert attach.raw and attach.owned == (("container", "abc"),)
    connect = vet("POST", "/networks/n1/connect", {"Container": "c1"}, root)
    assert set(connect.owned) == {("network", "n1"), ("container", "c1")}


def test_disconnect_is_this_frames_like_connect(root: Path) -> None:
    """`docker compose up` disconnects a container on a changed network: the same rule as connect."""
    gone = vet("POST", "/v1.47/networks/n1/disconnect", {"Container": "c1", "Force": True}, root)
    assert gone.allow and set(gone.owned) == {("network", "n1"), ("container", "c1")}
    lower = raw("/v1.47/networks/n1/disconnect", b'{"container": "c1", "force": true}', root)
    assert lower.body == b'{"Container": "c1", "Force": true}' and ("container", "c1") in lower.owned


@pytest.mark.parametrize("runtime", ["sysbox-runc", "kata", "runsc"])
def test_a_runtime_but_the_default_is_refused(root: Path, runtime: str) -> None:
    refused(create({"Runtime": runtime}, root), "Runtime")
    refused(raw(RAW_CREATE, json.dumps({"hostconfig": {"runtime": runtime}}).encode(), root), "Runtime")
    why, _ = recheck(inspected({"Runtime": runtime}), "web", roots=[str(root)])
    assert why is not None and "Runtime" in why


@pytest.mark.parametrize("runtime", ["", "runc", "io.containerd.runc.v2"])
def test_the_default_runtime_stands(root: Path, runtime: str) -> None:
    assert create({"Runtime": runtime}, root).allow
    assert recheck(inspected({"Runtime": runtime}), "web", roots=[str(root)]) == (None, ())


def test_exec_privileged_refused(root: Path) -> None:
    refused(vet("POST", "/containers/abc/exec", {"Cmd": ["sh"], "Privileged": True}, root), "privileged")
    assert vet("POST", "/containers/abc/exec", {"Cmd": ["sh"]}, root).owned == (("container", "abc"),)


def test_exec_start_detached_is_not_a_stream(root: Path) -> None:
    assert vet("POST", "/exec/e1/start", {"Detach": False}, root).raw
    assert not vet("POST", "/exec/e1/start", {"Detach": True}, root).raw


def test_unknown_endpoint_403(root: Path) -> None:
    for method, path in (
        ("POST", "/v1.47/containers/prune"),
        ("POST", "/v1.47/images/prune"),
        ("DELETE", "/v1.47/images/alpine"),
        ("PUT", "/v1.47/containers/abc/archive?path=/"),
        ("POST", "/v1.47/containers/abc/commit"),
        ("GET", "/v1.47/secrets"),
        ("POST", "/v1.47/swarm/init"),
        ("POST", "/v1.47/plugins/pull"),
        ("POST", "/v1.47/images/create?fromSrc=-"),
        ("GET", "/v1.47/../containers/json"),
        ("POST", "/v1.47/containers/abc%2F..%2Fdef/start"),
    ):
        verdict = vet(method, path, None, root)
        refused(verdict)
        assert "not allowed" in verdict.reason, (path, verdict.reason)


def test_the_allowlist(root: Path) -> None:
    for method, path in (
        ("GET", "/_ping"),
        ("HEAD", "/_ping"),
        ("GET", "/v1.47/version"),
        ("GET", "/v1.47/info"),
        ("GET", "/v1.47/images/json"),
        ("POST", "/v1.47/images/create?fromImage=alpine&tag=latest"),
        ("GET", "/v1.47/images/docker.io/library/alpine:latest/json"),
        ("POST", "/v1.47/build?t=demo"),
        ("POST", "/session"),
        ("POST", "/grpc"),
        ("POST", "/v1.47/containers/abc/wait?condition=next-exit"),
        ("POST", "/v1.47/containers/abc/kill"),
        ("GET", "/v1.47/exec/e1/json"),
        ("POST", "/v1.47/exec/e1/resize?h=1&w=1"),
        ("POST", "/v1.47/containers/abc/resize?h=1&w=1"),
    ):
        assert vet(method, path, None, root).allow, (method, path)


def test_malformed_bodies_refused(root: Path) -> None:
    refused(vet_request("POST", "/containers/create", b"not json", ref=REF, task_id=TASK, roots=[str(root)]))
    refused(vet("POST", "/containers/create", ["a", "list"], root))
    refused(vet("POST", "/containers/create", {"HostConfig": {"Binds": "a string"}}, root))


def test_events_are_filtered_to_this_frame(root: Path) -> None:
    from urllib.parse import parse_qs, urlsplit

    since = vet("GET", "/v1.47/events?since=0", None, root)
    assert since.allow and since.target is not None
    query = parse_qs(urlsplit(since.target).query)
    assert query["since"] == ["0"] and json.loads(query["filters"][0]) == {"label": [f"{FRAME_LABEL}={REF}"]}
    rename = vet("POST", "/v1.47/containers/abc/rename?name=old_web", None, root)
    assert rename.owned == (("container", "abc"),), "compose's recreate renames: only this frame's"
    resize = vet("POST", "/containers/abc/resize?h=10&w=80", None, root)
    assert resize.owned == (("container", "abc"),), "resize acts only on this frame's container"


def test_published_ports_stay_on_loopback(root: Path) -> None:
    """A published port reaches the LAN unless it is bound to loopback: an empty, wildcard address
    is rewritten, in any key spelling; any other address outside loopback is refused."""
    bindings = {
        "80/tcp": [{"HostIp": "", "HostPort": "8080"}, {"HostIp": "0.0.0.0", "HostPort": "8081"}],
        "81/tcp": [{"HostIp": "::", "HostPort": "8082"}, {"HostPort": "8083"}],
        "82/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8084"}, {"HostIp": "::1", "HostPort": "8085"}],
        "83/tcp": [{"hostip": "0.0.0.0", "hostport": "8086"}],
        "84/tcp": None,
    }
    verdict = create({"PortBindings": bindings}, root)
    assert verdict.allow, verdict.reason
    assert verdict.body is not None
    sent = json.loads(verdict.body)["HostConfig"]["PortBindings"]
    assert sent["80/tcp"] == [
        {"HostIp": "127.0.0.1", "HostPort": "8080"},
        {"HostIp": "127.0.0.1", "HostPort": "8081"},
    ]
    assert [b["HostIp"] for b in sent["81/tcp"]] == ["127.0.0.1", "127.0.0.1"]
    assert [b["HostIp"] for b in sent["82/tcp"]] == ["127.0.0.1", "::1"]
    assert sent["83/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": "8086"}]


@pytest.mark.parametrize("address", ["192.168.1.5", "10.0.0.1", "fe80::1", "example.test"])
def test_a_published_port_beyond_loopback_refused(root: Path, address: str) -> None:
    bindings = {"80/tcp": [{"HostIp": address, "HostPort": "8080"}]}
    refused(create({"PortBindings": bindings}, root), "loopback")


def test_publish_all_ports_refused(root: Path) -> None:
    """`-P` binds every exposed port on every address, at the daemon: nothing to rewrite."""
    refused(create({"PublishAllPorts": True}, root), "PublishAllPorts")
    assert create({"PublishAllPorts": False}, root).allow


@pytest.mark.parametrize("address", ["", "0.0.0.0", "::", "192.168.1.5"])
def test_start_revets_published_addresses(root: Path, address: str) -> None:
    host = {"PortBindings": {"80/tcp": [{"HostIp": address, "HostPort": "8080"}]}}
    why, _ = recheck(inspected(host), "c1", roots=[str(root)])
    assert why is not None and "loopback" in why
    fine = {"PortBindings": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}}
    assert recheck(inspected(fine), "c1", roots=[str(root)])[0] is None
    assert recheck(inspected({"PublishAllPorts": True}), "c1", roots=[str(root)])[0] is not None
