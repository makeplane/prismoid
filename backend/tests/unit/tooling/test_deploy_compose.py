# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The self-host stack (deploy/compose.prod.yaml) runs the Machine, the browser and the screen on
sandbar. A static read plus `docker compose config` where a Docker CLI exists: a stack that boots
with a screen that 503s, or a browser profile the worker refuses at create, is a dead feature with
nothing in the code to show for it."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from prismoid.conf import ApiSettings
from tests.support.paths import REPO

DEPLOY = REPO / "deploy"
COMPOSE = DEPLOY / "compose.prod.yaml"
ENV_EXAMPLE = DEPLOY / ".env.example"

SEED = b"\x07" * 32


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())


def _shared_env() -> dict[str, Any]:
    return _compose()["x-backend-env"]


def _public_half(seed: bytes) -> bytes:
    return (
        Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def _env_file(tmp_path: Path, *, drop: str = "") -> Path:
    values = {
        "POSTGRES_PASSWORD": "pw",
        "MINIO_PASSWORD": "pw",
        "SANDBAR_TOKEN": "tok",
        "SIGNING_PRIVATE_KEY": base64.b64encode(SEED).decode(),
        "CONNECTORS_ENCRYPTION_KEY": "k",
        "S3_PUBLIC_ENDPOINT": "https://s3.example.com",
        "API_PUBLIC_URL": "https://api.example.com",
        "WEB_ORIGIN": "https://example.com",
        "DOMAIN": "example.com",
        "ACME_EMAIL": "ops@example.com",
        "DOCKER_GID": "999",
        "SCREEN_SIGNING_KEY": "s1:" + base64.b64encode(SEED).decode(),
        "SANDBAR_DISPLAY_KEYS": "s1:" + base64.b64encode(_public_half(SEED)).decode(),
    }
    values.pop(drop, None)
    path = tmp_path / "env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def _config(env: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(env), "config"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_compose_turns_machines_on() -> None:
    assert _shared_env()["PRISMOID_MACHINES_ENABLED"] == "true"


def test_compose_turns_computer_use_on() -> None:
    """Off is the code's default: a browser ask then falls to a writer and the screen answers
    "off", so the stack's Machines, browser image and screen keys serve nothing (found live, E1). On by
    default; COMPUTER_USE=false in deploy/.env is the kill switch."""
    assert _shared_env()["PRISMOID_COMPUTER_USE_ENABLED"] == "${COMPUTER_USE:-true}"
    assert re.search(r"^COMPUTER_USE=true$", ENV_EXAMPLE.read_text(), re.M)


def test_compose_sets_browser_image() -> None:
    value = _shared_env()["PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE"]
    assert value == "${FRAME_BROWSER_IMAGE:-prismoid-frame-browser:prod}"


def test_compose_turns_the_screen_on_by_default() -> None:
    assert _shared_env()["PRISMOID_COMPUTER_USE_SANDBAR_SCREEN"] == "${SANDBAR_SCREEN:-true}"


def test_compose_requires_both_screen_keys_and_the_docker_group() -> None:
    text = COMPOSE.read_text()
    for var in ("SCREEN_SIGNING_KEY", "SANDBAR_DISPLAY_KEYS", "DOCKER_GID"):
        assert f"${{{var}:?" in text, f"{var} is not required in compose.prod.yaml"
    group_add = _compose()["services"]["sandbard"]["group_add"]
    assert group_add == ["${DOCKER_GID:?getent group docker | cut -d: -f3}"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_compose_requires_screen_keys(tmp_path: Path) -> None:
    ok = _config(_env_file(tmp_path))
    assert ok.returncode == 0, ok.stderr
    for missing in ("SANDBAR_DISPLAY_KEYS", "SCREEN_SIGNING_KEY"):
        out = _config(_env_file(tmp_path, drop=missing))
        assert out.returncode != 0
        assert "deploy/.env.example" in out.stderr, out.stderr
        assert "openssl" in out.stderr, out.stderr


def test_env_example_declares_the_keys_and_the_api_parses_a_key_in_its_format() -> None:
    """The example's generation commands print a key the API parses and a public half sandbar's
    ParseKeys reads (kid:base64 of the raw 32-byte public key)."""
    key = "s1:" + base64.b64encode(SEED).decode()
    api = ApiSettings.model_validate({"screen_signing_key": key})
    assert api.screen_key() == ("s1", SEED)
    assert len(_public_half(SEED)) == 32
    text = ENV_EXAMPLE.read_text()
    assert re.search(r"^SCREEN_SIGNING_KEY=", text, re.M)
    assert re.search(r"^SANDBAR_DISPLAY_KEYS=", text, re.M)


SEARCH_NAMES = (
    "PRISMOID_SEARCH_ENGINES",
    "PRISMOID_SEARCH_KEYS",
    "PRISMOID_SEARCH_URLS",
    "PRISMOID_SEARCH_PRICES_PER_1K",
    "PRISMOID_SEARCH_MODELS",
)


def test_compose_shares_search_settings_with_the_api_and_the_worker() -> None:
    """The worker searches, and the API plans too (a replan writes the planner's brief, which says
    whether a step can read the internet): a chain only the worker read left a replan's brief on a
    stack with search saying no step could."""
    shared, services = _shared_env(), _compose()["services"]
    for name in SEARCH_NAMES:
        assert shared[name] == f"${{{name}:-}}", name
        for service in ("api", "worker"):
            assert services[service]["environment"][name] == shared[name], (service, name)


@pytest.mark.parametrize("name", [*SEARCH_NAMES, "SEARXNG_SECRET"])
def test_env_example_sets_each_search_setting_once(name: str) -> None:
    """Enabling search is filling a line in, never uncommenting a second one: compose takes the last
    line of a name, so an uncommented example above an empty line was still empty to compose while
    deploy.sh read the example and started SearXNG."""
    text = ENV_EXAMPLE.read_text()
    assert len(re.findall(rf"^{name}=", text, re.M)) == 1, name
    assert not re.search(rf"^#\s*{name}=", text, re.M), f"{name} has a commented example assignment"


def test_searxng_is_opt_in() -> None:
    service = _compose()["services"]["searxng"]
    assert service["profiles"] == ["search"]
    assert "ports" not in service
    assert "@sha256:" in service["image"]
    assert "./searxng/settings.yml:/etc/searxng/settings.yml:ro" in service["volumes"]
    # The secret comes from the environment: the mounted settings must not carry a literal.
    assert "secret_key" not in (DEPLOY / "searxng" / "settings.yml").read_text().replace("No secret_key", "")


def test_searxng_secret_is_guarded_with_the_profile() -> None:
    """compose interpolates a profile-disabled service too, so `${SEARXNG_SECRET:?}` would break
    every stack with search off; the service refuses to start on an empty secret instead, naming
    the generation command, and deploy.sh refuses the same case up front."""
    service = _compose()["services"]["searxng"]
    guard = " ".join(service["entrypoint"])
    assert "openssl rand -hex 32" in guard
    assert service["environment"]["SEARXNG_SECRET"] == "${SEARXNG_SECRET:-}"
    assert "SEARXNG_SECRET=" in ENV_EXAMPLE.read_text()
    assert "SEARXNG_SECRET is empty" in (DEPLOY / "deploy.sh").read_text()


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_search_profile_config_is_valid_and_off_by_default(tmp_path: Path) -> None:
    env = _env_file(tmp_path)
    off = _config(env)
    assert off.returncode == 0, off.stderr
    assert "searxng" not in off.stdout
    on = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "search", "--env-file", str(env), "config"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert on.returncode == 0, on.stderr
    assert "searxng:" in on.stdout


def test_migrate_waits_for_the_buckets_it_seeds() -> None:
    """The skills seed writes to the bucket minio-init makes: a fresh box's first `up` raced it and
    the migrate failed on NoSuchBucket (found live, E1). Not required, so a stack on a managed store
    (the minio profile off) still starts."""
    edge = _compose()["services"]["migrate"]["depends_on"]["minio-init"]
    assert edge == {"condition": "service_completed_successfully", "required": False}


def _env_value(tmp_path: Path, env: str, name: str) -> str:
    script = (DEPLOY / "deploy.sh").read_text()
    found = re.search(r"^env_value\(\) \{.*?^\}", script, re.S | re.M)
    assert found, "deploy.sh has no env_value()"
    (tmp_path / ".env").write_text(env)
    run = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + found.group(0) + '\nenv_value "$1"', "_", name],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    return run.stdout.rstrip("\n")


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ("SEARXNG_SECRET=   # REQUIRED with option 2: openssl rand -hex 32\n", ""),
        ("SEARXNG_SECRET=\n", ""),
        ("# SEARXNG_SECRET=abc\n", ""),
        ("DOMAIN=x\n", ""),
        ("SEARXNG_SECRET=abc123\n", "abc123"),
        ("SEARXNG_SECRET=  abc123   # mine\n", "abc123"),
        ("SEARXNG_SECRET=abc#123\n", "abc#123"),
        ("SEARXNG_SECRET=old\nSEARXNG_SECRET=new\n", "new"),
    ],
)
def test_deploy_reads_a_value_without_its_inline_comment(tmp_path: Path, env: str, want: str) -> None:
    """An uncommented but unfilled template line (`SEARXNG_SECRET=   # REQUIRED ...`) passed the
    secret guard with its comment as the value, and compose hands SearXNG that comment as its secret.
    deploy.sh reads every deploy/.env value through one helper that drops an inline comment and the
    blanks around it, and takes a name's last line, as compose does."""
    assert _env_value(tmp_path, env, "SEARXNG_SECRET") == want


def test_deploy_reads_deploy_env_only_through_env_value() -> None:
    script = (DEPLOY / "deploy.sh").read_text()
    assert '[[ -z "$(env_value SEARXNG_SECRET)" ]]' in script
    assert "env_value PRISMOID_SEARCH_ENGINES | grep" in script
    raw = [line for line in script.splitlines() if "cut -d=" in line]
    assert len(raw) == 1 and "sed " in raw[0], raw


def _with_search(given: str) -> str:
    script = (DEPLOY / "deploy.sh").read_text()
    found = re.search(r"^with_search\(\) \{.*?^\}", script, re.S | re.M)
    assert found, "deploy.sh has no with_search()"
    run = subprocess.run(
        ["bash", "-c", found.group(0) + '\nwith_search "$1"', "_", given],
        capture_output=True,
        text=True,
        check=True,
    )
    return run.stdout.strip()


@pytest.mark.parametrize(
    ("given", "want"),
    [
        ("minio", "minio,search"),
        ("", "search"),
        ("minio,search", "minio,search"),
        (" minio , search", "minio,search"),
    ],
)
def test_deploy_adds_search_to_the_profiles_the_env_names(given: str, want: str) -> None:
    """A `--profile` flag replaces COMPOSE_PROFILES: deploy.sh's `--profile search` left Silo off and
    the migrate failing (found live, E1). It adds `search` to deploy/.env's list instead."""
    assert _with_search(given) == want
    code = [
        line for line in (DEPLOY / "deploy.sh").read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if "--profile" in line]


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_the_search_profile_keeps_the_object_store(tmp_path: Path) -> None:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(_env_file(tmp_path)),
            "config",
            "--services",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COMPOSE_PROFILES": _with_search("minio")},
    )
    assert out.returncode == 0, out.stderr
    assert {"minio", "minio-init", "migrate", "searxng"} <= set(out.stdout.split())
