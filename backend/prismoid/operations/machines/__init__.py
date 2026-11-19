# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine per task: the lifecycle over `FrameDriver`.

Only this package calls lifecycle verbs (`bind`, `freeze`, `thaw`, `archive`, `unpack`,
`destroy`) on a Machine's frame. Its public surface is re-exported here as the tasks land. The
control lease's operations are reached as `machines.screen.<fn>`, never
re-exported one by one: `machines.release` is the step's hand-back."""

from prismoid.operations.machines import screen  # the control lease, as `machines.screen.<fn>`
from prismoid.operations.machines.acquired import (
    Acquired,
    AcquiredKind,
    driver_named,
    driver_of,
    recording_tag,
)
from prismoid.operations.machines.device import ack_interrupted, grant_fence, hold_parked, interrupted
from prismoid.operations.machines.egress_push import push_egress
from prismoid.operations.machines.handback import (
    ParkReleased,
    discard_legacy,
    holds,
    parked_frame,
    release,
    release_parked,
    widen,
)
from prismoid.operations.machines.lifecycle import acquire, boot, boot_early, boots_early, seat_profile
from prismoid.operations.machines.meter import bill, meter_throwaway, settle, summary
from prismoid.operations.machines.need import plan_need, plan_need_for, takes_machine
from prismoid.operations.machines.paper import PAPER_LABEL, fits_on_paper, paper, starts_on_paper
from prismoid.operations.machines.reconcile import orphans_once, reconcile_once, sweep_once, view_of
from prismoid.operations.machines.seed import SeedNotReady, run_seed, seed_fork, seed_retryable
from prismoid.operations.machines.sleep import (
    Forgotten,
    SleepClaim,
    claim_sleep,
    cleanup,
    expire,
    forget,
    forgettable,
    release_asleep,
    request_sleep,
    reset,
    reset_now,
    reset_retryable,
    retire_or_sleep,
    sleep,
    sleep_now,
    stop_browser,
)

__all__ = [
    "PAPER_LABEL",
    "Acquired",
    "AcquiredKind",
    "Forgotten",
    "ParkReleased",
    "SeedNotReady",
    "SleepClaim",
    "ack_interrupted",
    "acquire",
    "bill",
    "boot",
    "boot_early",
    "boots_early",
    "claim_sleep",
    "cleanup",
    "discard_legacy",
    "driver_named",
    "driver_of",
    "expire",
    "fits_on_paper",
    "forget",
    "forgettable",
    "grant_fence",
    "hold_parked",
    "holds",
    "interrupted",
    "meter_throwaway",
    "orphans_once",
    "paper",
    "parked_frame",
    "plan_need",
    "plan_need_for",
    "push_egress",
    "reconcile_once",
    "recording_tag",
    "release",
    "release_asleep",
    "release_parked",
    "request_sleep",
    "reset",
    "reset_now",
    "reset_retryable",
    "retire_or_sleep",
    "run_seed",
    "screen",
    "seat_profile",
    "seed_fork",
    "seed_retryable",
    "settle",
    "sleep",
    "sleep_now",
    "starts_on_paper",
    "stop_browser",
    "summary",
    "sweep_once",
    "takes_machine",
    "view_of",
    "widen",
]
