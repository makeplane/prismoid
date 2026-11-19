# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine's warm-time segment and the conf grammars its drivers read (ADR-063): pure values.

The leaves of `schemas/machines.py`, which re-exports every name here: nothing in this module knows
a Machine's state, and nothing does I/O or reads a clock.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal


def segment_seconds(start: dt.datetime | None, end: dt.datetime) -> int:
    """Whole seconds of one warm segment. None (never metered) is zero, and a start after the
    end (a clock that went backwards between two `now()` reads) is zero, never negative."""
    if start is None:
        return 0
    return max(0, int((end - start).total_seconds()))


def parse_prices(text: str) -> dict[str, Decimal]:
    """`base=0.0005,browser=0.00075,device.base=0` → {profile or driver.profile: USD per minute}.
    The shared name=value grammar."""
    out: dict[str, Decimal] = {}
    for part in text.split(","):
        name, _, value = part.partition("=")
        if name.strip() and value.strip():
            out[name.strip().lower()] = Decimal(value.strip())
    return out


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch of warm frame time, closed and owed to the ledger. A hand-back closes it
    under the Machine's row lock and returns it, never records it: the
    caller bills it where taking the task's row is safe. Its watermark has already moved, so a
    segment that is lost on the way is under-billed, never billed twice. `at` is its end on the
    database clock; None (a throwaway frame, timed on the worker) means the clock when billed."""

    task_id: uuid.UUID
    profile: str
    seconds: int
    at: dt.datetime | None = None
    # The driver that ran it (the Machine row's, a throwaway frame's own); "" = this worker's.
    driver: str = ""


def parse_rfc3339(stamp: str) -> dt.datetime:
    """`2026-09-30T12:34:56.123456789Z` → an aware datetime (microseconds; the rest dropped)."""
    head, _, frac = stamp.strip().rstrip("Z").partition(".")
    frac = frac.split("+")[0].split("-")[0]
    return dt.datetime.fromisoformat(f"{head}.{(frac or '0')[:6].ljust(6, '0')}+00:00")


@dataclass(frozen=True, slots=True)
class Resources:
    cpus: float
    mem_mb: int
    pids: int


def parse_resources(text: str) -> dict[str, Resources]:
    """`base=2cpu/2048mb/512pids,browser=…` → {profile: Resources}."""
    out: dict[str, Resources] = {}
    for part in text.split(","):
        name, _, value = part.partition("=")
        if not (name.strip() and value.strip()):
            continue
        cpu, mem, pids = (v.strip().lower() for v in value.split("/"))
        out[name.strip().lower()] = Resources(
            cpus=float(cpu.removesuffix("cpu")),
            mem_mb=int(mem.removesuffix("mb")),
            pids=int(pids.removesuffix("pids")),
        )
    return out
