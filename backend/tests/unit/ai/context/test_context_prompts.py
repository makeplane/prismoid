# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Context's *wording*, everywhere it reaches a model: the mounted file and the planner brief
frame it as instruction-never-source, the step prompt walls it off from the evidence, and
loaded skills are finally NAMED (being on disk is not the same as being known about)."""

from __future__ import annotations

import uuid

from prismoid.ai.context.blocks import render_context_md, reply_memory_section
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.frames.layout import ANSWERS, CONTEXT
from prismoid.schemas.context import ContextItem, ContextLane, ContextPack
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec


def a_pack() -> ContextPack:
    return ContextPack(
        lanes=[
            ContextLane(lane="instructions", items=[ContextItem(text="Cite sources. UK English.")], tokens=7),
            ContextLane(
                lane="user",
                items=[
                    ContextItem(text="Prefers numbers tables.", memory_id=uuid.uuid4(), kind="preference")
                ],
                tokens=6,
                truncated=True,
            ),
        ],
        total_tokens=13,
    )


def a_spec(*, inputs: list[str], pins: tuple[SkillPin, ...] = ()) -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        title="a memo",
        skill_pins=list(pins),
        contract=Contract(
            goal="Write a memo on Q3.",
            acceptance=["every figure comes from the brief"],
            inputs=inputs,
            output=OutputRef(type=ArtifactType.DOCUMENT, title="memo"),
        ),
    )


def test_renderings_open_with_the_instruction_not_source_wall() -> None:
    md = render_context_md(a_pack())
    assert "not a source" in md and "never cite" in md.lower()
    assert "Cite sources. UK English." in md and "Prefers numbers tables." in md
    assert "trimmed to fit" in md, "a clipped lane says so where the model reads it"


def test_every_memory_framing_keeps_memory_unspoken_and_the_request_first() -> None:
    """A remembered note the request contradicts is set aside in silence, never narrated to the person."""
    for framing in (render_context_md(a_pack()), reply_memory_section(a_pack())):
        said = " ".join(framing.lower().split())
        assert "never mention" in said and "the request wins" in said, framing


def test_prompt_walls_context_off_from_the_sources() -> None:
    prompt = prompt_for(a_spec(inputs=[CONTEXT, ANSWERS, "/task/inputs/brief.md"]))
    head, _, sources_block = prompt.partition("Your sources are already in the sandbox")
    assert CONTEXT in head and "instruction" in head, "context.md is introduced, as an instruction"
    assert CONTEXT not in sources_block and ANSWERS not in sources_block, (
        "neither instruction file may appear under the evidence heading"
    )
    assert "/task/inputs/brief.md" in sources_block

    assert CONTEXT not in prompt_for(a_spec(inputs=["/task/inputs/brief.md"])), "no file, no paragraph"


def test_prompt_names_loaded_skills_iff_pinned() -> None:
    pinned = prompt_for(a_spec(inputs=[], pins=(SkillPin(name=SkillName("report"), version=2),)))
    assert "/skills/report/SKILL.md" in pinned
    assert "/skills/" not in prompt_for(a_spec(inputs=[])), "no pins, no skills line"


def test_prompt_walls_the_thread_off_from_the_sources() -> None:
    """`/task/thread.md` is the conversation - named so the agent reads it, walled
    so it can never be cited as evidence: same law as answers.json and context.md."""
    from prismoid.ai.frames.layout import THREAD

    prompt = prompt_for(a_spec(inputs=[THREAD, "/task/inputs/brief.md"]))
    assert THREAD in prompt and "conversation" in prompt
    assert "never cite it" in prompt

    sources_at = prompt.index("Your sources are already in the sandbox")
    assert prompt.index(THREAD) < sources_at, "the thread paragraph precedes the evidence block"
    assert THREAD not in prompt[sources_at:], "the thread is never listed as a source"


def _typed(output: OutputRef | None) -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("maker"),
        title="t",
        contract=Contract(goal="Make it.", acceptance=["the totals cross-foot"], output=output),
    )


def test_the_brief_says_where_the_deliverable_goes_and_acceptance_stays_the_persons() -> None:
    """How the file is handed over is the agent's instruction, said once, never a line it is judged on."""
    prompt = prompt_for(_typed(OutputRef(type=ArtifactType.SPREADSHEET, title="budget")))
    head, _, judged = prompt.partition("You will be judged on:")
    assert "type='spreadsheet'" in head and "/work" in head
    assert "type='" not in judged and "/work" not in judged

    change = prompt_for(_typed(OutputRef(type=ArtifactType.CHANGE, title="fix")))
    assert "/out/change" in change and "type='change'" in change
    assert "publish" not in prompt_for(_typed(None)), "a step that owes no file is told of none"


def test_the_brief_holds_every_answer_to_the_work() -> None:
    """An answer the person gave is a requirement the work shows, never a hint it may drop."""
    prompt = prompt_for(a_spec(inputs=[ANSWERS]))
    assert "Use every answer" in prompt
    assert "Use every answer" not in prompt_for(a_spec(inputs=[])), "no answers, no line"


def test_the_brief_marks_the_threads_own_deliverables_for_revision() -> None:
    """A follow-up's earlier deliverables are named as the work to revise in place, not material to redo."""
    memo = "/task/inputs/memo.md"
    prompt = prompt_for(a_spec(inputs=[memo, "/task/inputs/brief.md"]), revising=[memo])
    head, _, sources_block = prompt.partition("Your sources are already in the sandbox")
    assert f"`{memo}`" in head and "revise" in head and "same file name" in head
    assert "/task/inputs/brief.md" not in head
    assert memo in sources_block, "the earlier deliverable stays evidence its facts may come from"
    assert "revise" not in prompt_for(a_spec(inputs=[memo])), "a first turn has nothing to revise"
