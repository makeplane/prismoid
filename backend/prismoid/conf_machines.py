# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The settings of a task's computer: the Machine per task, and Prismoid on the user's own Mac.

`prismoid.conf` composes these into `settings` (`settings.machines`, `settings.local`) and every caller
reads them there.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from prismoid.conf_env import cfg


class MachineSettings(BaseSettings):
    """The Machine per task. `enabled=False` is the rollback lever:
    `machines.acquire` hands back a throwaway frame per step, as before Machines existed, and the sweep keeps
    putting warm Machines to sleep."""

    model_config = cfg("PRISMOID_MACHINES_")

    enabled: bool = True
    idle_s: int = Field(default=600, gt=0)
    park_hold_s: int = Field(default=1800, gt=0)
    retention_days: int = Field(default=14, gt=0)
    workspace_warm_cap: int = Field(default=10, ge=1)  # 0 would make every step wait, then fail as infra
    wait_s: int = Field(default=600, gt=0)
    verify_wait_s: int = Field(default=900, ge=0)  # a receipt's wait on a full host
    workspace_asleep_cap: int = Field(default=4, ge=0)  # computers kept asleep on hosts
    # The early boot: on a driver in `frames.EARLY_BOOT_DRIVERS`, a task's first Machine starts when
    # its turn is picked up, under the intake round and the planner, so the first step binds it warm.
    # Only there: its profiles are one computer, where Docker's and sandbar's are different frames,
    # and a guess wrong there costs a re-create. False: the first step starts it, as before.
    boot_on_accept: bool = True
    # Paper: a step whose agent needs no computer (it reads what the system mounted and hands back
    # text, writing no file a park would have to keep) runs on an in-memory frame, never the task's Machine, unless the task
    # is on the device (its Machine holds no host slot). False: every serial step takes the Machine.
    # `paper_max_bytes` caps one step's files in the worker's memory; a step whose inputs and
    # upstream artifacts pass half of it takes the Machine (the rest is headroom for the thread,
    # skills and the deliverable).
    paper: bool = True
    paper_max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    # How long a wake keeps asking a natively asleep computer's provider that does not answer
    # (ADR-063): the provider restarting, a slow host. Retried with backoff (1 s doubling to
    # 15 s), each Wake cut at what is left; then the step fails as infra with the computer asleep,
    # never destroyed. 180 s is over ten times the slowest wake measured (9-15 s). 0: one Wake.
    wake_retry_s: int = Field(default=180, ge=0)
    checkpoint_max_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    # Both build a cron at import (`jobs/machines.py`, in the API and the worker) with the interval
    # in the seconds field, `*/N * * * * *` - so N must divide a minute, or the schedule drifts.
    sweep_interval_s: int = 30
    reconcile_interval_s: int = 60
    # `stuck_s + reconcile_interval_s` stays under `wait_s` (checked below): the reconciler judges a
    # stuck wake or sleep on its next run, up to one interval after `stuck_s`, and a step waiting
    # on that Machine must outlive the fix instead of failing as infra first. The stuck rule spares
    # a sleeping row whose job is still queued and a wake whose attempt still runs, so a short one
    # never judges live work.
    stuck_s: int = Field(default=300, gt=0)
    legacy_grace_s: int = 3600
    # Provisional machine-time rates, USD per minute per image profile (pricing lands before
    # the product is priced). A flat name=value grammar; an unknown profile is priced as `base`. A
    # `driver.profile` key prices one driver's profile: the user's
    # own computer (`device`) costs nothing.
    price_per_min_usd: str = "base=0.0005,browser=0.00075,device.base=0,device.browser=0"
    # Per-profile limits - today's hard-coded values (docker_driver.py MEMORY_LIMIT/CPU_QUOTA/
    # pids_limit, sandbar core.go defaults) made data.
    resources: str = "base=2cpu/2048mb/512pids,browser=2cpu/2048mb/512pids"

    @field_validator("sweep_interval_s", "reconcile_interval_s")
    @classmethod
    def _divides_a_minute(cls, value: int) -> int:
        if not 1 <= value <= 60 or 60 % value:
            raise ValueError(
                f"{value} s does not divide a minute (the cron's seconds field): "
                "use 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 or 60"
            )
        return value

    @model_validator(mode="after")
    def _stuck_before_a_wait_gives_up(self) -> MachineSettings:
        if self.stuck_s + self.reconcile_interval_s >= self.wait_s:
            raise ValueError(
                f"stuck_s ({self.stuck_s}) + reconcile_interval_s ({self.reconcile_interval_s}) must be "
                f"under wait_s ({self.wait_s}): the reconciler fixes a stuck Machine on its next run, "
                "and a step waiting on it would fail as infra first"
            )
        return self

    @field_validator("price_per_min_usd")
    @classmethod
    def _prices_parse(cls, value: str) -> str:
        """Every entry `profile=USD-per-minute`, a finite price no less than zero, and a `base`
        (an unknown profile is priced as `base`) - checked at boot, so a price the meter cannot
        read never surfaces in a sleep's finisher, a reset or a rewake. It reads the grammar of
        `schemas.machines.parse_prices`, which conf may not import (the layers contract makes them
        siblings), and reads it more strictly: an entry without `=` is refused, not skipped."""
        prices: dict[str, Decimal] = {}
        for entry in (e.strip() for e in value.split(",")):
            if not entry:
                continue
            name, sep, raw = entry.partition("=")
            try:
                price = Decimal(raw.strip())
            except InvalidOperation:
                price = None
            if not sep or not name.strip() or price is None or not price.is_finite() or price < 0:
                raise ValueError(f"price_per_min_usd: {entry!r} is not profile=USD-per-minute")
            prices[name.strip().lower()] = price
        if "base" not in prices:
            raise ValueError("price_per_min_usd needs a base price: an unknown profile is priced as base")
        return value


class LocalSettings(BaseSettings):
    """Prismoid on the user's own computer: where its data lives and how the
    device Machine's helper reaches Chrome. `home` is the data home - postgres, objects, run/,
    tasks/, scratch/, chrome/ and `helper.key` all live under it."""

    model_config = cfg("PRISMOID_LOCAL_")

    home: Path = Path("~/Library/Application Support/Prismoid").expanduser()
    away_limit_h: int = 24
    chrome_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    api_port: int = 8100
    web_dir: Path | None = None
    # What `prismoid up`'s preflight found (`local/runtime.preflight`): the lanes these two serve are
    # off without them, and a task degrades as it does for any preview that is not there.
    has_office: bool = False  # LibreOffice: renditions
    has_poppler: bool = False  # pdftoppm: the pages QA looks at
