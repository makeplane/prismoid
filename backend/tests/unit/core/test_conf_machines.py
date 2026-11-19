# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's dials read the flat `.env` like every other group (ADR-063):
`PRISMOID_MACHINES_ENABLED=false` is the rollback lever, and a lever that only the process
environment can pull is silently ignored in dev. No stack needed - a pure Settings suite.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prismoid.conf import MachineSettings
from tests.support.paths import REPO


def test_a_dotenv_value_reaches_machine_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PRISMOID_MACHINES_ENABLED", "PRISMOID_MACHINES_IDLE_S"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("PRISMOID_MACHINES_ENABLED=false\nPRISMOID_MACHINES_IDLE_S=5\n")
    monkeypatch.chdir(tmp_path)
    machines = MachineSettings()
    assert machines.enabled is False
    assert machines.idle_s == 5


def test_the_sweep_and_reconcile_intervals_must_divide_a_minute() -> None:
    """Both crons are built at import, in the API and the worker, with the interval in the
    seconds field (`*/N * * * * *`): 45 would fire at :00 and :45, not every 45 s."""
    from pydantic import ValidationError

    assert MachineSettings(sweep_interval_s=20, reconcile_interval_s=60).reconcile_interval_s == 60
    for bad in ({"sweep_interval_s": 45}, {"reconcile_interval_s": 90}, {"sweep_interval_s": 0}):
        with pytest.raises(ValidationError):
            MachineSettings(**bad)  # pyright: ignore[reportArgumentType]


def test_a_malformed_price_fails_at_boot() -> None:
    """Fix round 1, M2: a price the meter cannot read must fail when the settings load, never
    inside a sleep's finisher (a stuck `sleeping`), a reset (a 500) or a rewake (an infra failure)."""
    from pydantic import ValidationError

    from prismoid.schemas.machines import parse_prices

    ok = MachineSettings(price_per_min_usd=" Base = 0.5 , browser=0.75 ,").price_per_min_usd
    assert parse_prices(ok) == {"base": Decimal("0.5"), "browser": Decimal("0.75")}
    for bad in ("base=abc", "base=-1", "base=NaN", "browser=0.1", "base0.0005", "base=0.5,browser", ""):
        with pytest.raises(ValidationError):
            MachineSettings(price_per_min_usd=bad)


def test_the_dials_have_bounds_and_a_stuck_wake_is_fixed_before_a_waiting_step_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap of 0 or a non-positive interval is refused at boot, and `stuck_s +
    reconcile_interval_s` must be under `wait_s` - a step waiting on a stuck sleep then outlives the
    reconciler's fix instead of failing as infra first. The default `stuck_s` is 300 (under the
    default `wait_s`)."""
    from pydantic import ValidationError

    monkeypatch.chdir(tmp_path)  # no `.env`: the defaults
    for name in (
        "PRISMOID_MACHINES_STUCK_S",
        "PRISMOID_MACHINES_WAIT_S",
        "PRISMOID_MACHINES_RECONCILE_INTERVAL_S",
    ):
        monkeypatch.delenv(name, raising=False)
    assert MachineSettings().stuck_s == 300 < MachineSettings().wait_s
    bad: list[dict[str, int]] = [
        {"workspace_warm_cap": 0},
        {"idle_s": 0},
        {"park_hold_s": -1},
        {"wait_s": 0},
        {"stuck_s": 0},
        {"retention_days": 0},
        {"checkpoint_max_bytes": 0},
        {"stuck_s": 600, "wait_s": 600},
        {"stuck_s": 700, "wait_s": 600},
        # B7: the reconciler judges a stuck row on its next run, up to one interval late
        {"stuck_s": 540, "wait_s": 600},  # 540 + 60 is not under 600
        {"stuck_s": 571, "wait_s": 600, "reconcile_interval_s": 30},
    ]
    for values in bad:
        with pytest.raises(ValidationError):
            MachineSettings(**values)  # pyright: ignore[reportArgumentType]
    assert MachineSettings(workspace_warm_cap=1, stuck_s=539, wait_s=600).workspace_warm_cap == 1
    assert MachineSettings(stuck_s=569, wait_s=600, reconcile_interval_s=30).stuck_s == 569


def test_the_self_host_stack_ships_with_the_machine_lever_on() -> None:
    """Sandbar's four gates were met on 2026-10-06 (ADR-063 "Operating it"), so the self-host
    stack runs Machines. Every service that reads the lever says so: the API (a fork's seed) and
    the worker."""
    import yaml

    compose = REPO / "deploy" / "compose.prod.yaml"
    services = yaml.safe_load(compose.read_text())["services"]
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert str(env.get("PRISMOID_MACHINES_ENABLED")).lower() == "true", name
