# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Lead that writes its plan as text (a fenced `submit_plan` JSON) is sent back to call the tool;
the text never ends the turn as a reply."""

from __future__ import annotations

import json

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals.runners import decide
from evals.schema import Outcome, OutcomeRow
from tests.unit.evals.test_decide_runner import PLANNED, SEAT

# What the live Lead (claude-haiku-5-5) wrote on two turns that asked for a file, quoted in shape.
AS_TEXT = "I'll have the document built as a one-page PDF.\n\n```json\n" + json.dumps(PLANNED) + "\n```"


async def test_a_plan_written_as_text_is_not_a_reply() -> None:
    seen: list[int] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart(AS_TEXT)])

    row = OutcomeRow(id="t", message="Draft a one-page status update as a PDF", expected_outcome=Outcome.PLAN)
    trace = await decide.lead(SEAT, FunctionModel(model))(row)
    assert trace.outcome is Outcome.PLAN, (
        f"the Lead's plan, written as text, ended the turn as {trace.outcome} after {len(seen)} model call(s): "
        "the person reads raw JSON and no step runs"
    )
