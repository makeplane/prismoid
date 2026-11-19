# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's dials (ADR-065): the knobs and their bounds, the CONTROL
gate's wait under the Machine's park hold, and the screen key - parsed at boot in every
environment, required outside dev while computer use is on, and never echoed into an error.
No stack needed - a pure Settings suite (the `test_conf_email_gate.py` shape)."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from prismoid.conf import ApiSettings, ComputerUseSettings, EmailSettings, MachineSettings, Settings
from tests.support.hosted import use_real_secrets

SEED = bytes(range(32))


def key(kid: str = "k1", seed: bytes = SEED) -> SecretStr:
    return SecretStr(f"{kid}:{base64.b64encode(seed).decode()}")


def settings_with(**groups: Any) -> Settings:
    """A root Settings with the groups a test names, and a park hold and a mail backend that no
    developer's `.env` can tip over (the email gate refuses console mail in staging and prod)."""
    groups.setdefault("machines", MachineSettings(park_hold_s=1800))
    groups.setdefault("email", EmailSettings(backend="smtp"))
    return Settings(**groups)


def test_the_defaults_are_the_specs_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in os.environ if n.startswith(("PRISMOID_COMPUTER_USE_", "PRISMOID_API_SCREEN"))]:
        monkeypatch.delenv(name)
    use = ComputerUseSettings()
    assert (use.screen_fps_thumb, use.screen_fps_pane, use.screen_fps_holder) == (1, 5, 30)
    assert (use.screen_quality, use.screen_viewer_bps) == (60, 300_000)
    assert (use.screen_holder_quality, use.screen_holder_bps) == (45, 3_000_000)
    assert (use.screen_linger_s, use.screen_write_timeout_s, use.screen_input_per_s) == (5, 5, 120)
    assert (use.control_silence_s, use.control_private_cap_s, use.control_grace_s) == (60, 600, 30)
    assert (use.control_renew_every_s, use.control_wait_s) == (5, 1200)
    assert (use.mask_settle_ms, use.ticket_ttl_s) == (300, 30)
    assert ApiSettings(screen_signing_key=SecretStr("")).screen_key() is None


def test_every_knob_has_a_bound() -> None:
    bad: list[dict[str, int]] = [
        {"screen_fps_thumb": 0},
        {"screen_fps_holder": 31},  # the sidecar acks no faster than 30 fps
        {"screen_fps_pane": 31},
        {"screen_quality": 0},
        {"screen_quality": 101},
        {"screen_holder_quality": 0},
        {"screen_holder_quality": 101},
        {"screen_viewer_bps": 0},
        {"screen_holder_bps": 0},
        {"screen_linger_s": -1},
        {"screen_write_timeout_s": 0},
        {"screen_input_per_s": 0},
        {"control_silence_s": 0},
        {"control_private_cap_s": 0},
        {"control_grace_s": 0},
        {"control_renew_every_s": 0},
        {"control_wait_s": 0},
        {"mask_settle_ms": -1},
        {"ticket_ttl_s": 0},
        {"ticket_ttl_s": 31},  # sandbar refuses a display ticket that lives longer than 30 s
        {"control_renew_every_s": 60, "control_silence_s": 60},  # input could never renew in time
    ]
    for values in bad:
        with pytest.raises(ValidationError):
            ComputerUseSettings(**values)  # pyright: ignore[reportArgumentType]
    assert ComputerUseSettings(screen_linger_s=0, mask_settle_ms=0, ticket_ttl_s=10).ticket_ttl_s == 10


def test_a_control_gate_times_out_before_the_park_hold_could_sleep_its_machine() -> None:
    ok = settings_with(computer_use=ComputerUseSettings(control_wait_s=1799))
    assert ok.computer_use.control_wait_s < ok.machines.park_hold_s
    with pytest.raises(ValidationError, match="PRISMOID_COMPUTER_USE_CONTROL_WAIT_S"):
        settings_with(computer_use=ComputerUseSettings(control_wait_s=1800))
    with pytest.raises(ValidationError, match="PRISMOID_MACHINES_PARK_HOLD_S"):
        settings_with(machines=MachineSettings(park_hold_s=600))  # under the default 1200


def test_a_screen_key_parses_to_its_kid_and_seed() -> None:
    assert ApiSettings(screen_signing_key=key("k-2026.10_a", b"\x07" * 32)).screen_key() == (
        "k-2026.10_a",
        b"\x07" * 32,
    )
    assert ApiSettings(screen_signing_key=SecretStr(f"  {key().get_secret_value()} ")).screen_key() == (
        "k1",
        SEED,
    )


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_a_malformed_screen_key_fails_the_boot_and_is_never_echoed(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISMOID_ENV", environment)
    seed = base64.b64encode(b"\x09" * 32).decode()
    short = base64.b64encode(b"\x09" * 31).decode()
    for raw in (
        "no-colon-at-all",
        f":{seed}",
        f"bad kid:{seed}",
        f"{'k' * 33}:{seed}",
        "k1:not*base64",
        f"k1:{short}",
    ):
        with pytest.raises(ValidationError) as caught:
            settings_with(api=ApiSettings(screen_signing_key=SecretStr(raw)))
        message = str(caught.value)
        assert "PRISMOID_API_SCREEN_SIGNING_KEY" in message
        assert raw not in message and seed not in message and short not in message


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_staging_and_prod_refuse_to_boot_without_a_key_while_computer_use_is_on(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISMOID_ENV", environment)
    use_real_secrets(monkeypatch)
    on, off = ComputerUseSettings(enabled=True), ComputerUseSettings(enabled=False)
    with pytest.raises(ValidationError, match="PRISMOID_API_SCREEN_SIGNING_KEY is required"):
        settings_with(computer_use=on, api=ApiSettings(screen_signing_key=SecretStr("")))
    assert settings_with(computer_use=on, api=ApiSettings(screen_signing_key=key())).api.screen_key() == (
        "k1",
        SEED,
    )
    assert settings_with(computer_use=off, api=ApiSettings(screen_signing_key=SecretStr(""))).environment == (
        environment
    )


def test_dev_mints_its_own_key_so_it_boots_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMOID_ENV", "dev")
    booted = settings_with(
        computer_use=ComputerUseSettings(enabled=True), api=ApiSettings(screen_signing_key=SecretStr(""))
    )
    assert booted.api.screen_key() is None


def test_a_boot_error_never_echoes_an_env_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings puts every `.env` line no field claims into the input dict, and a
    validator's error printed that dict's tail - the last line's secret, in the boot log. Every
    `_cfg` group hides its input in errors (no plaintext secrets in logs)."""
    in_file, in_env = "sk-test-SHOULD-NOT-APPEAR-file", "sk-test-SHOULD-NOT-APPEAR-env"
    (tmp_path / ".env").write_text(f"PRISMOID_ENV=staging\nANTHROPIC_API_KEY={in_file}\n")
    monkeypatch.chdir(tmp_path)
    for name in ("PRISMOID_ENV", "PRISMOID_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", in_env)
    with pytest.raises(ValidationError, match="PRISMOID_API_SCREEN_SIGNING_KEY is required") as caught:
        settings_with(
            computer_use=ComputerUseSettings(enabled=True), api=ApiSettings(screen_signing_key=SecretStr(""))
        )
    message = str(caught.value)
    assert "PRISMOID_ENV=staging" in message
    assert "SHOULD-NOT-APPEAR" not in message and "input_value" not in message
