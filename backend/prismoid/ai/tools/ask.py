# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`ask_user` - the deferred tool that lets a step stop and ask.

**This tool does no I/O and knows nothing about gates, tasks, or databases.** It packages the
questions and raises `CallDeferred`; pydantic-ai then ends the run with a `DeferredToolRequests`
carrying the tool's arguments, and `operations` - which *is* allowed to touch the world - reads the
batch out of that, opens ONE gate, and parks the workflow. When the human answers, the run resumes
with every answer slotted in where the tool call was.

That division is the whole point of the deferred mechanism, and it is why `ai/` stays
persistence-free even though the feature is "pause a running agent on a human." The engine describes
what it wants; the composer makes it happen.

**One BATCH per call - never a conversation** (intake already had this law:
ONE numbered batch per gate). The first shape of this tool took a single `question`, so a model
needing five facts interrogated the user serially - ask, park, answer, resume, ask again -
fifteen "One question before I continue" gates on one live task. Each round costs a park, a
human context-switch, and a resume; a batch costs one. The tool now takes `questions` (1–6),
the gate renders them as one carousel, and the engine enforces a hard budget of asks per step -
after that the tool answers itself with "proceed on your judgment"
(`operations.execution.OUT_OF_QUESTIONS`), because a step that cannot stop asking is a step
that never finishes.

**Granted to top-level step agents only** (`can_ask_user`). A sub-agent cannot hold this: a
question raised three frames deep, from a toolset the parent did not model, parks a task on a gate
nobody can see the origin of. A sub-agent that needs something escalates through its contract.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import CallDeferred
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog
from prismoid.schemas.connector_catalog import CATALOG

# `operations` reads this name back off a deferred call to know the deferral is a question for a person.
ASK_USER: Final = catalog.tool("ask_user").name


class AskQuestion(BaseModel):
    """One question in the batch. Flat, MCP-elicitation-aligned - a prompt and, when it
    is a choice, the allowed answers."""

    model_config = ConfigDict(frozen=True)

    # A blank question is refused HERE, so the model is asked again: deferred, it parked nothing
    # (no readable question) and the run counted as ended, an approval in its turn dropped.
    question: str = Field(pattern=r"\S", description="one thing you need to know, as a complete question")
    options: list[str] = Field(
        default_factory=list,
        description="the allowed answers if it is a choice; empty for free text",
    )


def with_ask_user(toolset: FunctionToolset[object]) -> FunctionToolset[object]:
    """Add `ask_user` to a toolset. Called by `build()` only when the agent may ask."""

    async def ask_user(
        questions: Annotated[
            list[AskQuestion],
            Field(
                min_length=1,
                max_length=6,
                description="EVERYTHING you need to know, as one batch of 1-6 questions",
            ),
        ],
    ) -> str:
        """Stop and ask the user - ONCE, with everything you need in a single batch.

        This parks the work on a human, which is the most expensive thing you can do. Before
        calling: gather every unknown you can name NOW into this one batch (1-6 questions, each
        with options when it is a choice). Never ask one question, wait, then ask another - a
        second call may not be granted, and a drip of questions is how users abandon a task. A
        choice among options you have not seen yet (what a site or a file turns out to offer) is
        not an unknown yet: look first, then ask it with the real options. Prefer a reasonable
        assumption you state in the output over a question; ask only what you truly cannot
        decide. The task parks at no cost until they reply, and then you continue exactly where
        you left off with all their answers in hand.
        """
        # The metadata rides along on the deferred request so the *reason* survives the round trip;
        # `operations` reads the batch from the call's `args` to build the gate.
        raise CallDeferred(
            metadata={
                "kind": ASK_USER,
                "questions": [q.model_dump(mode="json") for q in questions],
            }
        )

    toolset.add_function(ask_user, name=ASK_USER)
    return toolset


# ── request_connector: the in-conversation connect block ──────────────────────────────────────

REQUEST_CONNECTOR: Final = catalog.tool("request_connector").name
# The one home of this rule: the step brief (`context/brief.py`) and the tool's own description both
# render it, so the two can never disagree about what a connector is for.
SUBJECT_IS_NOT_A_SERVICE: Final = (
    "A product the goal names only as its SUBJECT - compared, priced, reviewed, researched "
    '("compare Notion and Obsidian") - is not a service to connect: research it like any other topic.'
)
_CONNECT_TOLD: Final = (
    "Ask the user to connect an external service, right in the conversation - use this when the "
    "goal needs a service (their Notion, their mail, their tracker) that is not among your mounted "
    f"connector tools - their own account, to read their data or act in it. {SUBJECT_IS_NOT_A_SERVICE} "
    "ONE request at a time, and only for a service the task truly needs; the user can connect it "
    "on the spot and you continue with its tools available, or they skip and you continue without "
    "it (say what was skipped). Never pretend an action on an unconnected service happened."
)
# The grant that holds it, by the catalog's record name like any other, never implied by
# `can_ask_user`: the computer asks the user and is never pointed at a connector.
CONNECT_GRANT: Final = REQUEST_CONNECTOR


def _known_services() -> str:
    """The catalog's slugs, for the tool description - schemas are data ai/ may read."""
    return ", ".join(e.slug for e in CATALOG)


def with_request_connector(toolset: FunctionToolset[object]) -> FunctionToolset[object]:
    """Add `request_connector` - the same deferred spine as `ask_user`, for a different human
    act: the task needs a SERVICE it does not hold, and instead of a dead-end sentence the
    user gets a connect block right in the conversation. They authorize there; the run resumes
    with the connector's tools mounted (accepting the gate attaches it). Granted by
    `CONNECT_GRANT`, to agents that can also ask - a deferral needs the widened output type, and
    a sub-agent's connect request would park a task on a gate nobody can see the origin of."""

    async def request_connector(
        service: Annotated[
            str,
            Field(
                max_length=48,
                description=(
                    "which service - one of the known slugs when it matches "
                    f"({_known_services()}), else the plain service name (e.g. 'gmail')"
                ),
            ),
        ],
        why: Annotated[
            str,
            Field(
                max_length=200,
                description="one sentence, for the user: what you need it for on THIS task",
            ),
        ],
    ) -> str:
        raise CallDeferred(metadata={"kind": REQUEST_CONNECTOR, "service": service, "why": why})

    toolset.add_function(request_connector, name=REQUEST_CONNECTOR, description=_CONNECT_TOLD)
    return toolset
