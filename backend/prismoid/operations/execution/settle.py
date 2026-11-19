# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The typed ending, read: a guarded run's Outcome, settled. The nudges (drive first, hand the
browser over, prove the `done`) ran inside the run, as output checks
(`ai/capabilities/output_checks.py`); this is operations' side of them: the proof check's eyes on
the frame (`Ending.proof`, the check itself is `proof.py`) and the system's word after the run
(`Ending.settled`): a `done` whose proof still fails is `partial`, beside the model's own word."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import logfire
from pydantic_ai.messages import ModelMessage

from prismoid.ai.capabilities.output_checks import Checked
from prismoid.operations.execution.proof import Browse, check_proof, unproven
from prismoid.schemas.outputs import Outcome


@dataclass(frozen=True)
class Settled:
    """What reading a guarded run's Outcome came to."""

    result: Any  # the run's result
    override: Outcome | None = None  # what the step settles on instead (a `done` still unproven)
    blocked: Outcome | None = None  # the block a hand-off nudge answered: the row says it while parked


@dataclass
class Ending:
    """A step attempt's `EndingChecks`: the frame the proof is checked on."""

    frame: Any
    browser: Browse | None
    can_hand_off: bool
    step: str
    acceptance: Sequence[str] = ()

    async def proof(self, outcome: Outcome, messages: Sequence[ModelMessage]) -> str | None:
        """Why `outcome`'s proof fails now, or None when it holds."""
        published = await _published_paths(self.frame, step=self.step)
        return await check_proof(
            outcome, frame=self.frame, browser=self.browser, published=published, messages=messages
        )

    def settled(self, result: Any, checked: Checked) -> Settled:
        """The run's result and the system's word beside it, from what its output checks left."""
        output = result.output
        failed = checked.unconfirmed if isinstance(output, Outcome) and output.status == "done" else None
        override = None if failed is None else unproven(output, failed)
        return Settled(result, override=override, blocked=checked.handed_off)


async def _published_paths(frame: Any, *, step: str) -> list[str]:
    """The manifest's paths, as the proof check reads them (the harvest reads the same file after)."""
    from prismoid.schemas.artifacts import Manifest

    try:
        return list(Manifest.model_validate(await frame.manifest()).latest())
    except Exception as exc:
        # Fail-soft: an unreadable manifest is the harvest's failure to name; here it proves nothing.
        logfire.warn(
            "step {step}: the proof check could not read the manifest ({error})",
            step=step,
            error=type(exc).__name__,
        )
        return []
