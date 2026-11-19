# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The production compose file's S2 posture (ADR-063): sandbard (which holds the
Docker socket) makes a network of its own for each sandbox with domains and joins the relay to it,
so compose declares no shared frame network; sandbard never joins one, and the relay is the only
service it names. A static read - compose drift is how a posture quietly regresses."""

from __future__ import annotations

from typing import Any

import yaml

from tests.support.paths import REPO

COMPOSE = REPO / "deploy" / "compose.prod.yaml"


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())


def test_no_network_is_shared_by_sandboxes() -> None:
    """C13-13: every workspace's sandboxes once shared `sandbar_frame_net` and reached each other on it.
    sandbard now makes each sandbox's network from its own pool; compose declares none."""
    networks = _compose().get("networks") or {}
    assert all(
        name != "sandbar_frames" and (n or {}).get("name") != "sandbar_frame_net"
        for name, n in networks.items()
    )


def test_sandbard_names_its_pool_and_relay_and_publishes_no_proxy_port() -> None:
    sandbard = _compose()["services"]["sandbard"]
    assert not sandbard.get("networks")
    assert not any("7080" in str(p) for p in sandbard.get("ports") or [])
    assert "extra_hosts" not in sandbard
    env = sandbard["environment"]
    assert "SANDBAR_DOCKER_EGRESS_NET" not in env
    assert env["SANDBAR_DOCKER_EGRESS_POOL"] == "10.213.0.0/16"
    assert env["SANDBAR_RELAY_CONTAINER"] == "sandbar-egress"
    assert env["SANDBAR_EGRESS_URL"] == "http://sandbar-egress:7080"


def test_the_relay_is_named_and_restarts_sandbard_when_recreated() -> None:
    """sandbard joins the relay to each sandbox's network by name; a recreated relay has left them
    all, so sandbard restarts with it and its boot joins it again (adoption)."""
    services = _compose()["services"]
    relay = services["sandbar-egress"]
    assert relay["container_name"] == "sandbar-egress"
    assert relay["command"] == ["relay"] and relay["environment"]["SANDBAR_RELAY_TO"] == "sandbard:7080"
    assert set(relay.get("networks") or {"default": {}}) == {"default"}
    assert "sandbard" not in (relay.get("depends_on") or {})
    assert services["sandbard"]["depends_on"]["sandbar-egress"] == {
        "condition": "service_started",
        "restart": True,
    }


def test_sandbard_has_time_to_adopt_before_it_is_healthy() -> None:
    """The API serves only after adoption, which joins the relay to every network again."""
    assert _compose()["services"]["sandbard"]["healthcheck"]["start_period"] == "5m"


def test_the_egress_pair_rotates_its_logs() -> None:
    """A sandbox provoking relay refusals or proxy denials in a loop writes log lines;
    unrotated json-file logs on the one box would fill its disk, Postgres's included."""
    services = _compose()["services"]
    for name in ("sandbard", "sandbar-egress"):
        assert services[name]["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }, name
