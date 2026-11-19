# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What an attempt's death costs the step: a model attempt, or an infra attempt the weather took.

The ladder gives the MODEL three attempts (`MAX_ATTEMPTS`). An attempt the model never lost - a
provider that failed (a 529 past the transport's own same-model retries, every fallback candidate
down), a sandbox or service that stopped answering mid-tool, a wall-clock stop - is an INFRA
attempt instead: it nets out of `model_attempts()`, so an outage never spends the user's ladder.
Infra attempts are capped on their own (`settings.worker.max_infra_attempts`), or a wall-clock stop
repeats every half hour until the budget gate; and a provider death waits before the next attempt
(exponential, on the workflow's durable clock), because asking a failing provider again at once is
how a blip becomes an outage.

Pure: the failure's flags in, a frozen verdict out.
"""

from __future__ import annotations

from dataclasses import dataclass

from prismoid.errors import ModelIncapable, PolicyDenied


@dataclass(frozen=True, slots=True)
class Death:
    """How one attempt's death is booked."""

    infra: bool  # not the model's: nets out of `model_attempts()`
    counted: bool  # spends the infra cap (a Machine that would not come up paces itself instead)
    backoff: bool  # the provider failed: wait before asking it again


def booked(failure: PolicyDenied | ModelIncapable) -> Death:
    """The verdict on one dead attempt, read from the flags its raise set.

    `infra`: the raise said so (a sandbox, a wall clock, a Machine). `provider`: the provider
    failed, not the model - unless it `refused` the request (a 400 or 413 is a fact about what we
    sent, and the model owns it). `machine`: acquiring the Machine failed; that wait is paced by
    `wait_s` and capped by the Machine's own rules, so it does not spend the infra cap."""
    context = failure.context
    provider = bool(context.get("provider")) and not context.get("refused")
    infra = bool(context.get("infra")) or provider
    return Death(infra=infra, counted=infra and not context.get("machine"), backoff=provider)


def spent(failures: int, cap: int) -> bool:
    """Whether the step has had every infra attempt it may take: `cap` retries, then escalate."""
    return failures > cap


def backoff_s(failures: int, *, base: float, ceiling: float) -> float:
    """The wait before the attempt after the `failures`-th infra death: `base`, doubling, at most
    `ceiling`. 0 when `base` is 0 (retry at once)."""
    if base <= 0 or failures <= 0:
        return 0.0
    return min(ceiling, base * 2 ** (failures - 1))
