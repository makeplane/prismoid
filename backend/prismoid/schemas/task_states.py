# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task state machine: which status may follow which.

`Task.transition()` is the only way a status changes, so `ALLOWED` is the machine itself.
"""

from __future__ import annotations

from typing import Final

from prismoid.schemas.plan import TaskStatus

# ── the state machine ────────────────────────────────────────────────────────────────────────────
#
#   created ──▶ clarifying? ──▶ planning ──▶ running ◀──▶ waiting ──▶ delivered
#                    │              │            │  ▲        │
#                    ▼              ▼            ▼  │        ▼
#                cancelled       failed       paused ┘   cancelled/failed
#
ALLOWED: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.CLARIFYING, TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    # CLARIFYING → FAILED is the clarifier-crash edge (deliberate; not drawn in the diagram):
    # an intake agent that dies mid-question fails the task rather than stranding it in `clarifying`.
    TaskStatus.CLARIFYING: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING,
            TaskStatus.PAUSED,
            TaskStatus.PLANNING,  # replan-as-edit re-enters planning
            TaskStatus.DELIVERED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.PAUSED}
    ),
    # PAUSED → FAILED: a step that cannot park runs on through a pause, and its failure is
    # a fact - refusing it left the task `paused` with nothing driving it. PAUSED → WAITING is not
    # an edge: a park leaves a paused task paused (`Task.stays_paused`).
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    # Settled, not sealed: a follow-up continues the SAME task - settled → planning
    # opens turn N+1 on the same row, same record, same stream. Everything else stays refused:
    # `TERMINAL` still means "settled" for every archive/delete/steer guard, and only
    # `continue_task` takes this edge. Fork is the branching action; continuation is not a branch.
    TaskStatus.DELIVERED: frozenset({TaskStatus.PLANNING}),
    TaskStatus.FAILED: frozenset({TaskStatus.PLANNING}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.PLANNING}),
}

TERMINAL: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.DELIVERED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
