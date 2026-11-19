# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The engine's own invariant failures. Raised in every build, where an `assert` would vanish under
`python -O`, and typed, so the failure names what was wrong instead of a bare `AssertionError`.
"""

from __future__ import annotations

from prismoid.errors import PrismoidError
from prismoid.schemas.contract import Contract
from prismoid.schemas.plan import StepSpec


class IncompleteStep(PrismoidError):
    """A step reached the engine without an agent or without a contract. Planning never writes one,
    so this is a broken invariant, never a rung on the ladder: nothing catches it, and the step
    fails loudly with the step's id."""

    code = "internal"

    def __init__(self, step: object, missing: str) -> None:
        super().__init__(f"step {step} has no {missing} - its spec cannot run", step=str(step))


def runnable_contract(spec: StepSpec, step: object) -> Contract:
    """The spec's contract, once the spec is known to name both an agent and a contract."""
    if spec.agent is None or spec.contract is None:
        raise IncompleteStep(step, "agent" if spec.agent is None else "contract")
    return spec.contract
