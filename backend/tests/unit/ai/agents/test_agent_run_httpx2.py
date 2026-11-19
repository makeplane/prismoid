# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The gateway's clients speak httpx2 (`gateway/throttle.client_for`), and a provider SDK that does
not wrap a mid-stream transport error (google-genai) lets it out raw: a stream that stalls past its
read timeout raises `httpx2.ReadTimeout`, a dropped one `httpx2.RemoteProtocolError`. `httpx2`'s
errors are not `httpx`'s, so they must be named on their own - a provider death, like any other
dropped stream, never a task-ending crash and never "the run hit its request ceiling"."""

from __future__ import annotations

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.run import model_incapable
from prismoid.ai.deps import TaskDeps
from prismoid.errors import ModelIncapable
from tests.support.runners import runner_for

_RAW = [httpx2.ReadTimeout("timed out"), httpx2.RemoteProtocolError("peer closed connection")]


@pytest.mark.parametrize("exc", _RAW, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("guarded", [True, False])
def test_a_raw_httpx2_error_is_a_provider_death(exc: Exception, guarded: bool) -> None:
    death = model_incapable(exc, guarded=guarded)
    assert death.context == {"provider": True}
    assert str(death).startswith("the provider connection failed mid-run")


@pytest.mark.parametrize("exc", _RAW, ids=lambda e: type(e).__name__)
async def test_a_run_whose_stream_dies_raw_ends_as_an_attempt_death(exc: Exception) -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    agent = Agent(FunctionModel(model), deps_type=TaskDeps, output_type=str, name="writer")
    with pytest.raises(ModelIncapable) as caught:
        await runner_for(agent, TaskDeps()).run("go")
    assert caught.value.context.get("provider") is True
