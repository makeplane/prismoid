# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up` for real (ADR-077): a private Postgres, migrations, the first run, the worker, the API
serving the web app, and the helper - started by the CLI against a temp home, then stopped.

Nothing here touches the developer's stack: the cluster lives in the temp home, `HOME` points at a temp
dir (so the CLI's credentials land there), and the user-config file points at nothing."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from prismoid.local.postgres import Cluster
from tests.support.markers import darwin_only

pytestmark = [
    darwin_only,
    pytest.mark.skipif(not Cluster.available(), reason="no embedded Postgres binaries for this platform"),
]

_SPA_MARKER = "prismoid-spa-under-test"
_DEV_TOKEN, _DEV_URL = "cs_dev-stack-token", "http://localhost:8100"
_PRISMOID = [sys.executable, "-m", "prismoid.runner.main"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def box(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    # a short home: pytest's tmp_path can push the Postgres socket past macOS's 104-byte limit
    home = Path(f"/tmp/prismoid-up-{secrets.token_hex(4)}")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(f"<!doctype html><title>{_SPA_MARKER}</title>")
    user_home = tmp_path / "user"
    (user_home / ".prismoid").mkdir(parents=True)
    # a developer's token for another Prismoid (the dev stack), in the older single-token shape
    (user_home / ".prismoid" / "credentials").write_text(
        json.dumps({"token": _DEV_TOKEN, "api_url": _DEV_URL})
    )
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PRISMOID_")},
        "HOME": str(user_home),
        "PRISMOID_CONFIG_FILE": "/nonexistent/prismoid-config",
        "PRISMOID_LOCAL_HOME": str(home),
        "PRISMOID_LOCAL_API_PORT": str(_free_port()),
        "PRISMOID_LOCAL_WEB_DIR": str(web),
        "PRISMOID_LOCAL_OWNER_EMAIL": "owner@example.com",
        "PRISMOID_LOCAL_OWNER_NAME": "Owner",
        "PRISMOID_LOCAL_OWNER_PASSWORD": "a-long-enough-password",
        # the preflight wants one model key; nothing here calls a model
        "PRISMOID_WORKER_ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key",
    }
    yield home, env
    if (home / "run" / "prismoid.lock").exists():
        subprocess.run([*_PRISMOID, "down", "--json"], env=env, cwd=tmp_path, timeout=120, check=False)
    Cluster(home).stop()
    subprocess.run(["rm", "-rf", str(home)], check=False)


def _prismoid(
    env: dict[str, str], cwd: Path, *args: str, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_PRISMOID, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _check_running_stack(web_url: str, user_home: Path) -> None:
    assert httpx.get(f"{web_url}/healthz", timeout=10).status_code == 200
    page = httpx.get(f"{web_url}/tasks/some-deep-link", timeout=10)
    assert page.status_code == 200
    assert _SPA_MARKER in page.text
    creds = user_home / ".prismoid" / "credentials"
    assert oct(creds.stat().st_mode & 0o777) == "0o600"
    saved = json.loads(creds.read_text())
    assert saved["active"] == web_url
    assert saved["tokens"][_DEV_URL] == _DEV_TOKEN  # the first run kept the other Prismoid's token
    # ... where an older CLI (another worktree's) still reads it
    assert (saved["token"], saved["api_url"]) == (_DEV_TOKEN, _DEV_URL)
    token = saved["tokens"][web_url]
    me = httpx.get(f"{web_url}/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "owner@example.com"
    _only_loopback(web_url)


def _only_loopback(web_url: str) -> None:
    """The API answers on 127.0.0.1 and on no other address of this computer."""
    port = int(web_url.rsplit(":", 1)[1])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET: nothing is sent, the route picks an interface
            address = probe.getsockname()[0]
        except OSError:
            address = ""
    if not address or address.startswith("127."):
        return  # no network interface on this machine: nothing to reach it through
    with socket.socket() as s:
        s.settimeout(2)
        assert s.connect_ex((address, port)) != 0, f"the API answers on {address}"


def _listening_ports(pid: int) -> str:
    """What `pid` listens on over TCP (lsof: empty when nothing)."""
    r = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip()


def test_prismoid_up_end_to_end_healthz_and_spa(box: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    home, env = box
    err = tmp_path / "up.err"  # a file, not a pipe: nobody drains stderr while the stack runs
    with (
        err.open("w") as err_out,
        subprocess.Popen(
            [*_PRISMOID, "up", "--json"],
            env=env,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=err_out,
            text=True,
        ) as up,
    ):
        try:
            assert up.stdout is not None
            line = up.stdout.readline()
            assert line, (up.wait(timeout=30), err.read_text())
            ready = json.loads(line)
            web_url = ready["web_url"]
            assert web_url == f"http://127.0.0.1:{env['PRISMOID_LOCAL_API_PORT']}"
            assert ready["children"] == {"worker": "running", "api": "running", "helper": "running"}
            _check_running_stack(web_url, Path(env["HOME"]))
            assert oct((home / "run" / "worker.log").stat().st_mode & 0o777) == "0o600"
            state = json.loads((home / "run" / "prismoid.state").read_text())
            worker_pid = state["children"]["worker"][0]
            assert _listening_ports(worker_pid) == ""  # no DBOS admin server (0.0.0.0:3001), no port at all
            # ... and none tried: a port already taken would hide a server that meant to listen
            assert "admin server" not in (home / "run" / "worker.log").read_text().lower()

            status = json.loads(_prismoid(env, tmp_path, "status", "--json").stdout)
            assert status["running"] is True
            assert status["web_url"] == web_url
            assert status["children"] == {
                "postgres": "running",
                "worker": "running",
                "api": "running",
                "helper": "running",
            }

            again = _prismoid(env, tmp_path, "up", "--json")
            assert again.returncode == 0, again.stderr
            assert json.loads(again.stdout)["attached"] is True

            helper = _prismoid(env, tmp_path, "device", "helper", "--home", str(home), timeout=60)
            assert helper.returncode == 1 and "already running" in helper.stderr

            up.send_signal(signal.SIGINT)  # Ctrl-C
            assert up.wait(timeout=120) == 0, err.read_text()
        finally:
            if up.poll() is None:
                up.kill()
                up.wait()
    assert not Cluster(home).running()  # Postgres last, and down
    status = json.loads(_prismoid(env, tmp_path, "status", "--json").stdout)
    assert status["running"] is False
    assert set(status["children"].values()) == {"stopped"}


def test_prismoid_up_background_then_down(box: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    home, env = box
    up = _prismoid(env, tmp_path, "up", "--background", "--json", timeout=300)
    assert up.returncode == 0, up.stderr
    web_url = json.loads(up.stdout)["web_url"]
    _check_running_stack(web_url, Path(env["HOME"]))
    status = json.loads(_prismoid(env, tmp_path, "status", "--json").stdout)
    assert status["running"] is True
    assert status["pid"] != os.getpid()
    os.kill(status["pid"], 0)  # the daemon lives on after the command returned
    # a CLI command finds this Prismoid on its non-default port with no PRISMOID_CLI_API_URL: the address the
    # first run signed in to is the one in use
    whoami = _prismoid(env, tmp_path, "whoami", "--json")
    assert whoami.returncode == 0, whoami.stderr
    assert json.loads(whoami.stdout)["email"] == "owner@example.com"

    down = _prismoid(env, tmp_path, "down", "--json")
    assert down.returncode == 0, down.stderr
    assert not Cluster(home).running()
    status = json.loads(_prismoid(env, tmp_path, "status", "--json").stdout)
    assert status["running"] is False
    assert (home / "run" / "supervisor.log").exists()
