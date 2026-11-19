# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The join law (ADR-028) - what "my upstream is ready" MEANS, as a pure total function.

`finish.promote_dependents` holds the task row lock and must stay thin; the DECISION it takes per
step lives here, where every (join, states) pair can be asserted exhaustively. Three verbs:

- PROMOTE - flip the step READY; its join is satisfied.
- SKIP    - settle the step SKIPPED; its reason to exist is gone (a dead primary).
- WAIT    - the join is not settled yet; read again at the next completion.

The law, per join kind, over SETTLED upstream (settled = every producer done, or - for a
tolerant step - done/skipped/escalated):

- "all":       every producer DONE. `tolerates_missing_inputs` (the fleet chair)
               softens it to settled-plus-at-least-one-done - exactly the pre-028 contract.
- "quorum":    SETTLED promotes (down to zero done); `join_quorum` is the EXECUTOR's floor
               (`assembly_ready_guard`), refused there with a diagnosis. Promotion must never
               hold the floor: a settled wave below quorum has nothing left to complete, so a
               promotion-layer refusal would strand the graph in WAIT forever - the executor's
               refusal is the honest, legible task failure instead.
- "consensus": SETTLED promotes - the merge judges whatever proposals survived, and refuses an
               empty table legibly rather than the graph stalling on a wave already over.

`primary_input` outranks all three: a step that exists FOR one upstream (a harmonize pass
for its own section) is SKIPPED the moment that upstream settles without DONE, and WAITs
while it is in flight - neighbours are optional context, never a reason to run.
"""

from __future__ import annotations

from enum import StrEnum

from prismoid.schemas.plan import StepSpec, StepState

SETTLED_NOT_DONE = (StepState.SKIPPED, StepState.ESCALATED)


class JoinDecision(StrEnum):
    PROMOTE = "promote"
    SKIP = "skip"
    WAIT = "wait"


def decide(spec: StepSpec, states: list[StepState], primary_state: StepState | None = None) -> JoinDecision:
    """The whole law. `states` are this step's producers' states; `primary_state` is the
    state of `spec.primary_input` when the spec names one (the caller resolves the id)."""
    if spec.primary_input is not None:
        if primary_state in SETTLED_NOT_DONE:
            return JoinDecision.SKIP
        if primary_state is not StepState.DONE:
            return JoinDecision.WAIT

    tolerant = spec.tolerates_missing_inputs or spec.join in ("quorum", "consensus")
    settled = all(s is StepState.DONE or (tolerant and s in SETTLED_NOT_DONE) for s in states)
    if not settled:
        return JoinDecision.WAIT

    done = sum(1 for s in states if s is StepState.DONE)
    if spec.join in ("quorum", "consensus"):
        # Settled is enough. The floor (quorum's `assembly_ready_guard`; the merge's own
        # empty-input refusal) is the EXECUTOR's, never promotion's - so even a settled wave
        # with ZERO done PROMOTES, letting the executor fail legibly instead of stranding a
        # finished wave in WAIT forever (the silent deadlock the doctrine forbids; finding 11).
        promotable = True
    else:
        promotable = done == len(states) or (tolerant and done >= 1)
    return JoinDecision.PROMOTE if promotable else JoinDecision.WAIT
