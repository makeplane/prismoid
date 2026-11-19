# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The checks that send an ending back share the agent's output retries with its schema: each draws at
most its own few (`NUDGES`) and only while the pool keeps one, so a model that keeps citing an unread page
ends with its answer standing (its publish refuses the id) instead of dying with the attempt."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.capabilities.citations import CitationCheck
from prismoid.ai.capabilities.output_checks import NUDGES, OUTPUT_RETRIES
from prismoid.ai.capabilities.record_sources import RecordSources
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.swarm import RecordSet, ResearchRecord


def _sent_back(messages: list[ModelMessage]) -> int:
    return sum(isinstance(m, ModelResponse) for m in messages) - 1


async def test_an_answer_that_keeps_citing_an_unread_page_stands_after_its_own_nudges() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("It grew [S9].")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        retries={"output": OUTPUT_RETRIES},
        capabilities=[CitationCheck()],
    )
    result = await agent.run("go", deps=TaskDeps())
    assert result.output == "It grew [S9]."
    assert _sent_back(result.all_messages()) == NUDGES


async def test_a_check_never_takes_the_last_retry_of_the_pool() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("It grew [S9].")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        retries={"output": 1},
        capabilities=[CitationCheck()],
    )
    result = await agent.run("go", deps=TaskDeps())
    assert _sent_back(result.all_messages()) == 1


async def test_records_that_keep_naming_an_unread_page_are_dropped_after_their_own_nudges() -> None:
    wrong = RecordSet(records=[ResearchRecord(entity="Bolt", source="S9", excerpts=["raised $12M"])])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    wrong.model_dump(mode="json", exclude={"records": {"__all__": {"url"}}}),
                )
            ]
        )

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=RecordSet,
        retries={"output": OUTPUT_RETRIES},
        capabilities=[RecordSources()],
    )
    result = await agent.run("go", deps=TaskDeps())
    assert result.output.records == []
    assert _sent_back(result.all_messages()) == NUDGES
