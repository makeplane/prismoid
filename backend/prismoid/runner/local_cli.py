# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up`, `prismoid down`, `prismoid status` - Prismoid whole on this Mac (ADR-077).

`up` is the one place that knows the order: the private Postgres, the migrations, the first run (the
owner and the CLI's key), the model-key check, then the helper, the worker and the API (which serves
the web app), each waited for - and they stop the other way round. It holds the home's lock from before the first of those, so a second
`up` attaches instead of migrating or prompting beside the first. Like the operator commands
(`models doctor`, `dev`), `up` opens the database itself: there is no API to ask until it has run.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import typer
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import ComputerUseSettings, Settings, settings
from prismoid.conf_env import USER_CONFIG
from prismoid.device.chrome import missing as chrome_missing
from prismoid.device.helper import KEY_HEADER
from prismoid.local.postgres import Cluster, PgUrls, new_password
from prismoid.local.runtime import RuntimeBuildFailed, ensure_runtime, lanes_off, preflight
from prismoid.local.supervisor import (
    ROLE_HELPER,
    ChildFailed,
    ChildSpec,
    InstanceLock,
    ReadyTimeout,
    Supervisor,
    run_dir,
)
from prismoid.runner import client
from prismoid.schemas.device import RUNTIME_DIR, RUNTIME_STAMP, runtime_python

CHILDREN = ("helper", "worker", "api")  # the order they start in
# `up --background` hands its held lock to the process it starts, by descriptor number.
_LOCK_FD_ENV = "PRISMOID_UP_LOCK_FD"
# v1 installs from the repo: the web app is `just web-local`'s react-router build.
_REPO_WEB = Path(__file__).resolve().parents[3] / "web" / "build" / "client"
_FIRST_PARTY = (
    "anthropic",
    "openai",
    "google",
)  # their keys are PRISMOID_WORKER_*, the rest PRISMOID_GATEWAY_*
_BACKGROUND_TIMEOUT_S = 300.0


def _home() -> Path:
    return settings.local.home.expanduser()


def _web_url(port: int) -> str:
    return str(httpx.URL(scheme="http", host="127.0.0.1", port=port))  # this computer only


# ── what the children are told ───────────────────────────────────────────────────────────────


def child_env(home: Path, urls: PgUrls, web_dir: Path, port: int) -> dict[str, str]:
    """The environment every child (and this process) runs with: local mode on this cluster. Built
    on the caller's own (PATH, model keys); every setting `up` depends on is set here, never left
    to a `.env` or `~/.prismoid/config` that could say otherwise, but for the kill switch, which is the
    owner's (`_computer_use`)."""
    return {
        **_inherited(),
        "PRISMOID_ENV": "local",
        "PRISMOID_WORKER_FRAME_DRIVER": "device",
        "PRISMOID_WORKER_STORAGE_BACKEND": "fs",
        "PRISMOID_CONNECTORS_ALLOW_INSECURE_LOCALHOST": "true",  # the person's own MCP server
        **_computer_use(),
        "PRISMOID_API_DATABASE_URL": urls.async_url,
        "PRISMOID_WORKER_DATABASE_URL": urls.sync_url,
        "PRISMOID_LOCAL_HOME": str(home),
        "PRISMOID_LOCAL_WEB_DIR": str(web_dir),
        "PRISMOID_LOCAL_API_PORT": str(port),
        "PRISMOID_API_HOST": "127.0.0.1",  # this computer only, never 0.0.0.0
        "PRISMOID_API_PORT": str(port),
        # every link the API mints (the blob door's among them) and the web app's own address
        "PRISMOID_API_PUBLIC_BASE_URL": _web_url(port),
        "PRISMOID_API_WEB_BASE_URL": _web_url(port),
        **preflight(),
        "PYTHONUNBUFFERED": "1",
    }


def _computer_use() -> dict[str, str]:
    """On for the coding seat's browser, unless the owner said otherwise: the kill switch
    set in the environment stays there, and one in `~/.prismoid/config` is read by the child itself, so
    here the variable must stay unset (a real variable beats the file)."""
    return (
        {}
        if "enabled" in ComputerUseSettings().model_fields_set
        else {"PRISMOID_COMPUTER_USE_ENABLED": "true"}
    )


def _inherited() -> dict[str, str]:
    """This process's environment, less what no child may see: the first run's answers (a password)
    and PRISMOID_ENV's other spelling, which could contradict it."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PRISMOID_LOCAL_OWNER_") and k != "PRISMOID_ENVIRONMENT"
    }


# The helper is the one process that touches this computer: it gets the little a process needs and
# its own settings, never a model key, a database URL or the worker's secrets.
_HELPER_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM", "TZ")
_HELPER_ENV_PREFIXES = ("LC_", "PRISMOID_LOCAL_")


def helper_env(env: dict[str, str]) -> dict[str, str]:
    kept = {k: v for k, v in env.items() if k in _HELPER_ENV_KEYS or k.startswith(_HELPER_ENV_PREFIXES)}
    kept |= {
        k: env[k] for k in ("PRISMOID_ENV", "PRISMOID_WORKER_FRAME_DRIVER", "PRISMOID_WORKER_STORAGE_BACKEND")
    }
    if "PRISMOID_CONFIG_FILE" in env:
        kept["PRISMOID_CONFIG_FILE"] = env["PRISMOID_CONFIG_FILE"]
    return {**kept, "PYTHONUNBUFFERED": "1"}


def child_specs(home: Path, env: dict[str, str], port: int) -> list[ChildSpec]:
    prismoid = [sys.executable, "-m", "prismoid.runner.main"]
    return [
        # The helper first: the worker's boot recovers device steps and must find their computer
        # there, and at Ctrl-C it stops last - no step sees its device go away while it winds down.
        ChildSpec(
            "helper",
            [*prismoid, "device", "helper", "--home", str(home)],
            helper_env(env),
            _helper_answers(home),
            keeps_its_stop=True,
        ),
        # The worker before the API: on a fresh database the worker makes DBOS's system database
        # (`<db>_dbos_sys`), and the API's enqueue-only DBOS client cannot boot without it.
        ChildSpec("worker", [*prismoid, "run", "worker"], env, _printed("worker ready"), 120.0),
        ChildSpec(
            "api",
            [*prismoid, "run", "api", "--host", "127.0.0.1", "--port", str(port)],
            env,
            _answers(f"{_web_url(port)}/healthz"),
        ),
    ]


def _printed(prefix: str) -> Callable[[str | None], bool]:
    return lambda line: line is not None and line.startswith(prefix)


def _answers(url: str) -> Callable[[str | None], bool]:
    def ready(line: str | None) -> bool:
        if line is not None:
            return False
        try:
            return httpx.get(url, timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False

    return ready


def _helper_answers(home: Path) -> Callable[[str | None], bool]:
    def ready(line: str | None) -> bool:
        if line is not None:
            return False
        try:
            key = (home / "helper.key").read_text().strip()
            transport = httpx.HTTPTransport(uds=str(home / "run" / "helper.sock"))
            base = httpx.URL(scheme="http", host="helper")  # the socket is the address
            with httpx.Client(transport=transport, base_url=base, timeout=1.0) as c:
                return c.get("/healthz", headers={KEY_HEADER: key}).status_code == 200
        except (OSError, httpx.HTTPError):
            return False

    return ready


# ── before the children: this process on the same cluster ───────────────────────────────────


def _adopt(env: dict[str, str]) -> None:
    """Read the settings the children will read, in place (every module holds the same object), so
    the migrations and the first run below speak to the private cluster in local mode."""
    os.environ.update(env)
    fresh = Settings()
    for name in type(settings).model_fields:
        setattr(settings, name, getattr(fresh, name))


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    import prismoid.db

    cfg = Config()  # no ini: env.py takes the URL from the settings tree
    cfg.set_main_option("script_location", str(Path(prismoid.db.__file__).parent / "migrations"))
    command.upgrade(cfg, "head")


def _in_session[T](work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """One unit of work on its own loop, its pool gone with it (a pool is bound to its loop)."""
    from prismoid.db.engine import dispose_engine, reset_engine
    from prismoid.db.session import session_scope

    async def go() -> T:
        try:
            async with session_scope() as session:
                return await work(session)
        finally:
            await dispose_engine()
            reset_engine()

    return asyncio.run(go())


def _ensure_owner(json_out: bool, web_url: str, owner: dict[str, str]) -> bool:
    """The first run: make the owner and save the CLI's key (0600). Never a second time: an owner
    that exists without a key on this terminal (a first run that died after signup) signs in once
    the stack is up - True asks the caller to say how (`_emit_up`)."""
    from prismoid.errors import PrismoidError
    from prismoid.local.bootstrap import AlreadySetUp, first_run, needs_first_run

    if not _in_session(needs_first_run):
        return client.token_for(web_url) is None  # another Prismoid's token is not a key to this one
    email, name, password = _owner_answers(json_out, owner)
    try:
        raw = _in_session(lambda s: first_run(s, email=email, name=name, password=password))
    except AlreadySetUp:  # another first run won the lock
        return client.token_for(web_url) is None
    except PrismoidError as e:
        fields = getattr(e, "fields", {})
        detail = "; ".join(f"{k}: {v}" for k, v in fields.items())
        client.fail(f"first run refused: {e}" + (f" ({detail})" if detail else ""), as_json=json_out)
    client.save_token(raw, web_url)
    return False


def _owner_answers(json_out: bool, owner: dict[str, str]) -> tuple[str, str, str]:
    """Email, name, password. The password is typed (twice, never echoed) or, for a script under
    `--json`, given in PRISMOID_LOCAL_OWNER_PASSWORD (taken out of the environment at the door, so no
    child ever inherits it) - never from the command line."""
    from prismoid.local.bootstrap import MIN_PASSWORD_LENGTH

    if json_out:
        email = owner.get("PRISMOID_LOCAL_OWNER_EMAIL", "")
        password = owner.get("PRISMOID_LOCAL_OWNER_PASSWORD", "")
        if not email or not password:
            client.fail(
                "the first run needs an owner: set PRISMOID_LOCAL_OWNER_EMAIL and PRISMOID_LOCAL_OWNER_PASSWORD "
                "(and PRISMOID_LOCAL_OWNER_NAME), or run `prismoid up` without --json to be asked",
                as_json=True,
            )
        return email, owner.get("PRISMOID_LOCAL_OWNER_NAME") or email.split("@")[0], password
    typer.echo("First run: the owner of Prismoid on this computer.")
    email = typer.prompt("Email")
    name = typer.prompt("Your name", default=email.split("@")[0])
    while True:
        password = getpass.getpass(f"Password ({MIN_PASSWORD_LENGTH} or more characters): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            typer.echo(f"At least {MIN_PASSWORD_LENGTH} characters, please.")
        elif getpass.getpass("Password again: ") != password:
            typer.echo("The two did not match.")
        else:
            return email, name, password


def _catalog_health() -> tuple[list[str], list[dict[str, str]]]:
    """The enabled models and `models doctor`'s findings for them."""
    from prismoid.db.models.model_catalog import ModelCatalogEntry
    from prismoid.operations import models

    async def read(session: AsyncSession) -> tuple[list[str], list[dict[str, str]]]:
        rows = await ModelCatalogEntry.all(session)
        return [f"{r.provider}:{r.model_id}" for r in rows], await models.doctor(session)

    return _in_session(read)


def key_variable(provider: str) -> str:
    prefix = "PRISMOID_WORKER" if provider in _FIRST_PARTY else "PRISMOID_GATEWAY"
    return f"{prefix}_{provider.upper()}_API_KEY"


def model_key_error(enabled: list[str], findings: list[dict[str, str]]) -> str | None:
    """None when at least one enabled model has its key; else what to set, and where."""
    missing = {f["ref"] for f in findings if f["check"] == "key-missing"}
    if not enabled:
        return "no model is enabled in the catalog: `prismoid models seed`, then `prismoid up` again"
    if any(ref not in missing for ref in enabled):
        return None
    names = " or ".join(key_variable(p) for p in sorted({ref.split(":")[0] for ref in missing}))
    return f"no model key is set: put {names} in your environment or {USER_CONFIG}, then `prismoid up` again"


def _check_model_keys(json_out: bool) -> None:
    enabled, findings = _catalog_health()
    if error := model_key_error(enabled, findings):
        client.fail(error, as_json=json_out)
    unkeyed = sorted({f["ref"].split(":")[0] for f in findings if f["check"] == "key-missing"})
    if unkeyed:
        typer.echo(f"note: no key for {', '.join(unkeyed)} - those models will not serve", err=True)


def _web_dir(json_out: bool) -> Path:
    web = (settings.local.web_dir or _REPO_WEB).expanduser()
    if not (web / "index.html").is_file():
        client.fail(f"the web app is not built at {web}: run `just web-local`", as_json=json_out)
    return web


def _port_taken(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── the commands ─────────────────────────────────────────────────────────────────────────────


def up(
    background: bool = typer.Option(False, "--background", help="Run detached; `prismoid down` stops it."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Start Prismoid on this Mac: Postgres, migrations, the first run, the device helper, the worker, the
    API and web app. Ctrl-C stops them in reverse. A second `up` reports the running one."""
    # The first run's answers leave the environment before anything can inherit it (Postgres included).
    owner = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("PRISMOID_LOCAL_OWNER_")}
    if sys.platform != "darwin":
        client.fail("`prismoid up` runs on macOS only (ADR-077)", as_json=json_out)
    # The first run's seeds log through logfire, which only the children configure.
    os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
    home = _home()
    lock = InstanceLock(home)
    handed = os.environ.pop(_LOCK_FD_ENV, None)  # the detached half of `up --background`
    if handed is not None:
        lock.adopt(int(handed))
    elif not lock.acquire():
        holder = lock.holder()
        if holder is not None and holder.role == ROLE_HELPER:
            client.fail(
                f"a standalone helper (pid {holder.pid}) holds {home}: run `prismoid down` or stop it, then "
                "`prismoid up` again",
                as_json=json_out,
            )
        _emit_status(home, json_out, attached=True)
        return
    try:
        _up(
            home,
            lock,
            owner,
            background=background and handed is None,
            prepared=handed is not None,
            json_out=json_out,
        )
    finally:
        lock.release()


def _up(
    home: Path, lock: InstanceLock, owner: dict[str, str], *, background: bool, prepared: bool, json_out: bool
) -> None:
    if not Cluster.available():
        client.fail("the embedded Postgres is not installed (pixeltable-pgserver)", as_json=json_out)
    web_dir = _web_dir(json_out)
    port = settings.local.api_port
    if not prepared and _port_taken(port):
        client.fail(f"127.0.0.1:{port} is in use: free it or set PRISMOID_LOCAL_API_PORT", as_json=json_out)
    cluster = Cluster(home)
    try:
        handed_off = _run(
            home,
            cluster,
            lock,
            owner,
            web_dir,
            port=port,
            background=background,
            prepared=prepared,
            json_out=json_out,
        )
    except _LeftRunning:
        raise  # never beneath the processes a killed detached supervisor may have left
    except BaseException:
        _stop(cluster, json_out, secondary=True)  # what failed first is what the exit says
        raise
    if not handed_off:
        _stop(cluster, json_out, secondary=False)


def _run(
    home: Path,
    cluster: Cluster,
    lock: InstanceLock,
    owner: dict[str, str],
    web_dir: Path,
    *,
    port: int,
    background: bool,
    prepared: bool,
    json_out: bool,
) -> bool:
    """Everything between Postgres up and Postgres down. True when a detached supervisor took the
    stack over (it stops Postgres itself)."""
    web_url = _web_url(port)
    env = child_env(home, _postgres_up(cluster, json_out), web_dir, port)
    _adopt(env)
    needs_login = False
    if not prepared:  # the detached half finds it done
        _migrate()
        needs_login = _ensure_owner(json_out, web_url, owner)
        _check_model_keys(json_out)
        if why := chrome_missing(settings.local.chrome_path):
            typer.echo(f"note: {why}", err=True)  # the stack runs; those tasks say so when they start
        _prepare_runtime(home, env)
    if background:
        _detach(home, lock, web_url, json_out, needs_login)
        return True
    sup = Supervisor(home, child_specs(home, env, port), lock=lock, facts={"web_url": web_url})

    def ready() -> None:
        _emit_up(
            home,
            web_url,
            os.getpid(),
            sup.status(),
            json_out=json_out,
            needs_login=needs_login,
            how="Ctrl-C stops it",
        )

    try:
        sup.up(on_ready=ready)
    except ReadyTimeout as e:
        client.fail(f"{e}; the end of {e.log}:\n{e.tail}", client.EXIT_TIMEOUT, json_out)
    except ChildFailed as e:
        client.fail(f"{e}; the end of {e.log}:\n{e.tail}", as_json=json_out)
    return False


def _postgres_up(cluster: Cluster, json_out: bool) -> PgUrls:
    """The embedded Postgres made if new and started; what it refused is the command's error. Only
    these two calls: `typer.Exit`, which every refusal further on raises, is a `RuntimeError` too."""
    try:
        cluster.ensure(new_password())
        return cluster.start()
    except RuntimeError as e:
        client.fail(str(e), as_json=json_out)


def _prepare_runtime(home: Path, env: dict[str, str]) -> None:
    """Prismoid's own Python, before the worker starts. A build that fails (offline, no uv and no pip)
    stops nothing: it says so once, and a publish that needs it says how to retry."""
    try:
        ensure_runtime(home, building=lambda: typer.echo("building Prismoid's Python runtime...", err=True))
    except RuntimeBuildFailed as e:
        if (home / RUNTIME_DIR / RUNTIME_STAMP).exists() and runtime_python(home).exists():
            typer.echo(f"note: Prismoid's Python runtime was not rebuilt, the one here stays: {e}", err=True)
        else:
            typer.echo(
                f"note: Prismoid's Python runtime was not built, office files will not publish: {e}", err=True
            )
    if why := lanes_off(env):
        typer.echo(f"note: {why}", err=True)


def _stop(cluster: Cluster, json_out: bool, *, secondary: bool) -> None:
    """Postgres last, after every child that talks to it. Its failure never hides an earlier one:
    then it is a second line, and the first error's exit code stands."""
    try:
        cluster.stop()
    except RuntimeError as e:
        if not secondary:
            client.fail(f"Postgres did not stop: {e}", as_json=json_out)
        typer.echo(f"also: Postgres did not stop: {e}", err=True)


def _detach(home: Path, lock: InstanceLock, web_url: str, json_out: bool, needs_login: bool) -> None:
    """`up --background`: start this command again in its own session with our lock handed down (no
    gap another `up` could take it in), its output in `run/supervisor.log`; return once it is up.
    Anything that ends the wait first (it died, it timed out, Ctrl-C) ends the detached one too, and
    only once it is gone does the caller stop Postgres - never beneath it."""
    log = run_dir(home) / "supervisor.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "prismoid.runner.main", "up", "--json"],
            env={**_inherited(), _LOCK_FD_ENV: str(lock.fileno())},
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lock.fileno(),),
        )
    finally:
        os.close(fd)
    sup = Supervisor(home, [])
    deadline = time.monotonic() + _BACKGROUND_TIMEOUT_S
    try:
        while not ((state := sup.state()).get("ready") and state.get("pid") == proc.pid):
            if proc.poll() is not None:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
                client.fail(f"Prismoid did not start; the end of {log}:\n{tail}", as_json=json_out)
            if time.monotonic() > deadline:
                client.fail(
                    f"Prismoid was not up after {_BACKGROUND_TIMEOUT_S:.0f}s; see {log}",
                    client.EXIT_TIMEOUT,
                    json_out,
                )
            time.sleep(0.2)
    except BaseException as e:
        if _end(proc):
            raise
        typer.echo(
            "error: the detached Prismoid did not stop in time and was killed; Postgres is left running "
            "for `prismoid down` to finish",
            err=True,
        )
        code = e.exit_code if isinstance(e, typer.Exit) else 130 if isinstance(e, KeyboardInterrupt) else 1
        raise _LeftRunning(code) from e
    _emit_up(
        home,
        web_url,
        proc.pid,
        sup.status(),
        json_out=json_out,
        needs_login=needs_login,
        how="`prismoid down` stops it",
    )


class _LeftRunning(typer.Exit):
    """The detached supervisor was killed before it had stopped: its children may live on, holding
    the lock, so Postgres stays up beneath them (`prismoid down` stops the orphans, then Postgres)."""


def _end(proc: subprocess.Popen[bytes]) -> bool:
    """Stop the detached supervisor and wait for it. True once it is gone (it stops its children,
    then Postgres); False when it had to be killed. Ctrl-C is ignored meanwhile: one more would stop
    Postgres beneath a supervisor still stopping."""
    typer.echo("stopping the detached Prismoid...", err=True)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=120)
            return True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return False
    finally:
        signal.signal(signal.SIGINT, previous)


def _emit_up(
    home: Path,
    web_url: str,
    pid: int,
    children: dict[str, str],
    *,
    json_out: bool,
    needs_login: bool,
    how: str,
) -> None:
    data: dict[str, object] = {"web_url": web_url, "home": str(home), "pid": pid, "children": children}
    # By address: a plain `prismoid login` goes wherever this terminal last signed in, or is configured to.
    sign_in = f"PRISMOID_CLI_API_URL={web_url} prismoid login"
    if json_out:
        if needs_login:
            data["sign_in"] = sign_in
        typer.echo(json.dumps(data))  # one line: a script reading `up`'s stream reads one line
        return
    lines = [f"Prismoid is up: {web_url}"]
    if needs_login:
        lines.append(f"Sign this terminal in with: {sign_in}")
    typer.echo("\n".join([*lines, f"({how})"]))


def _emit_status(home: Path, json_out: bool, *, attached: bool = False) -> None:
    sup = Supervisor(home, [])
    holder = sup.lock.holder()
    running = sup.lock.taken()
    web_url = sup.state().get("web_url") or _web_url(settings.local.api_port)
    postgres = Cluster.available() and Cluster(home).running()
    children = {
        "postgres": "running" if postgres else "stopped",
        **dict.fromkeys(CHILDREN, "stopped"),
        **sup.status(),
    }
    pid = holder.pid if holder else None
    data = {
        "running": running,
        "pid": pid,
        "role": holder.role if holder else None,
        "home": str(home),
        "web_url": web_url,
        "children": children,
    }
    if attached:
        data = {"attached": True, **data}
    head = f"Prismoid is running (pid {pid}): {web_url}" if running else "Prismoid is not running"
    rows = [f"  {name:<9} {state}" for name, state in children.items()]
    client.emit(data, json_out, human="\n".join([head, *rows, f"  home      {home}"]))


def status(json_out: bool = typer.Option(False, "--json")) -> None:
    """Is Prismoid up on this Mac: each piece running or stopped, the web address, the data home."""
    _emit_status(_home(), json_out)


def down(json_out: bool = typer.Option(False, "--json")) -> None:
    """Stop the Prismoid `up` started on this Mac (and wait until it has)."""
    home = _home()
    sup = Supervisor(home, [])
    if not sup.lock.taken():
        client.emit({"stopped": False, "running": False}, json_out, human="Prismoid is not running")
        return
    holder = sup.lock.holder()
    pid = holder.pid if holder else None
    try:
        sup.down()
    except TimeoutError as e:
        client.fail(str(e), client.EXIT_TIMEOUT, json_out)
    # A supervisor killed outright stopped no Postgres. Stop it under the lock, so never beneath a
    # new `up` that started in the meantime.
    if sup.lock.acquire():
        try:
            if Cluster.available() and Cluster(home).running():
                Cluster(home).stop()
        except RuntimeError as e:
            client.fail(f"Postgres did not stop: {e}", as_json=json_out)
        finally:
            sup.lock.release()
    client.emit({"stopped": True, "pid": pid}, json_out, human=f"Prismoid stopped (pid {pid})")
