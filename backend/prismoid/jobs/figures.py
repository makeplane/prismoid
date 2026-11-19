# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The figure pre-generation job (ADR-030) - `figures-{task}-t{n}`, on the verification
queue (both are post-answer side work that must never starve the step lanes).

One workflow, one journalled step: generate every outline-promised image concurrently while
the section wave writes, publish them as working artifacts on the outline step, and let the
existing outline → assembly edge mount them into every assembly attempt. Failure costs
placeholders - the assembly contract already says so - never the task.
"""

from __future__ import annotations

from prismoid.jobs import durable, effects


@durable.job("pregenerate_figures")
async def pregenerate_figures(task_id: str, turn: int) -> int:
    return await effects.pregenerate_figures(task_id, turn)
