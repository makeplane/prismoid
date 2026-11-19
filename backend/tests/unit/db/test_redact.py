# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid
from typing import get_args

import pytest
from pydantic import BaseModel

from prismoid.db.base import utcnow
from prismoid.schemas import events
from prismoid.schemas.context import ContextItemView, ContextLaneView
from prismoid.schemas.events import (
    BotMemoryWritten,
    ContextAssembled,
    GateOpened,
    MemoryStub,
    MemoryWritten,
    StepPrompt,
    redact_for,
)
from prismoid.schemas.ids import GateId, MemoryId, StepId, TaskId
from prismoid.schemas.plan import GateKind
from tests.support.samples import SAMPLES

T = TaskId(uuid.uuid4())
ADA, BOB = uuid.uuid4(), uuid.uuid4()
NOW = utcnow()
USER_MEM, BOT_MEM, PROJECT_MEM = (MemoryId(uuid.uuid4()) for _ in range(3))


def _assembled() -> ContextAssembled:
    return ContextAssembled(
        seq=3,
        task_id=T,
        at=NOW,
        total_tokens=90,
        lanes=[
            ContextLaneView(
                lane="user",
                tokens=40,
                items=[ContextItemView(preview="My codeword is PINEAPPLE-42", memory_id=USER_MEM)],
            ),
            ContextLaneView(
                lane="bot",
                tokens=20,
                items=[ContextItemView(preview="Ada prefers tables", memory_id=BOT_MEM)],
            ),
            ContextLaneView(
                lane="project",
                tokens=30,
                items=[ContextItemView(preview="Launch is on Oct 9", memory_id=PROJECT_MEM)],
            ),
        ],
        memory_ids=[USER_MEM, BOT_MEM, PROJECT_MEM],
    )


def test_the_creator_sees_everything() -> None:
    ev = _assembled()
    assert redact_for(ev, viewer_id=ADA, creator_id=ADA) is ev


def test_others_see_counts_not_contents() -> None:
    out = redact_for(_assembled(), viewer_id=BOB, creator_id=ADA)
    assert isinstance(out, ContextAssembled)
    lanes = {lane.lane: lane for lane in out.lanes}
    assert lanes["user"].items == [] and lanes["user"].tokens == 40
    assert lanes["bot"].items == []
    assert lanes["project"].items[0].preview == "Launch is on Oct 9"
    assert out.seq == 3 and out.total_tokens == 90
    assert "PINEAPPLE" not in out.model_dump_json()


def test_memory_ids_go_with_their_private_lanes() -> None:
    """A private memory's id is the creator's too: only the ids the shared lanes carry stay."""
    out = redact_for(_assembled(), viewer_id=BOB, creator_id=ADA)
    assert isinstance(out, ContextAssembled)
    assert out.memory_ids == [PROJECT_MEM]
    assert str(USER_MEM) not in out.model_dump_json() and str(BOT_MEM) not in out.model_dump_json()
    whole = redact_for(_assembled(), viewer_id=ADA, creator_id=ADA)
    assert isinstance(whole, ContextAssembled) and whole.memory_ids == [USER_MEM, BOT_MEM, PROJECT_MEM]


def test_an_id_no_shared_lane_carries_is_dropped() -> None:
    """Fail closed: an id the event names without a shared item to vouch for it is not served."""
    stray = MemoryId(uuid.uuid4())
    ev = ContextAssembled(seq=1, task_id=T, at=NOW, memory_ids=[stray])
    out = redact_for(ev, viewer_id=BOB, creator_id=ADA)
    assert isinstance(out, ContextAssembled) and out.memory_ids == []


def test_a_shared_only_pack_passes_through_untouched() -> None:
    ev = ContextAssembled(
        seq=2,
        task_id=T,
        at=NOW,
        lanes=[
            ContextLaneView(
                lane="project", tokens=5, items=[ContextItemView(preview="x", memory_id=PROJECT_MEM)]
            )
        ],
        memory_ids=[PROJECT_MEM],
    )
    assert redact_for(ev, viewer_id=BOB, creator_id=ADA) is ev


def test_memory_stubs_and_prompts_are_hidden() -> None:
    written = MemoryWritten(
        seq=4,
        task_id=T,
        at=NOW,
        count=1,
        items=[MemoryStub(id=MemoryId(uuid.uuid4()), kind="fact", text="PINEAPPLE-42")],
    )
    prompt = StepPrompt(
        seq=5,
        task_id=T,
        step_id=StepId(uuid.uuid4()),
        at=NOW,
        attempt=1,
        agent="writer",
        prompt="...PINEAPPLE-42...",
    )
    w = redact_for(written, viewer_id=BOB, creator_id=ADA)
    p = redact_for(prompt, viewer_id=BOB, creator_id=ADA)
    assert isinstance(w, MemoryWritten) and isinstance(p, StepPrompt)
    assert w.count == 1 and w.items == []
    assert "PINEAPPLE" not in p.prompt and p.seq == 5


# ── completeness: every event type is classified ──────────────────────────────────────────────
#
# `redact_for` is a denylist: a type it does not know passes through untouched. So every type the
# wire union defines is classified HERE, as redacted for non-creators or reviewed safe, and a new
# type in neither set fails by name until someone decides which it is. The bar for redaction is
# private memory, a private conversation, or a step prompt; the task's own work
# (its goal, plan, answers, narration, artifacts) is what the Project shares by design.

_SAFE: dict[str, str] = {
    # the task's own record: what it was asked, how it was run, what people said to it
    "task.created": "the ask; the goal is the shared work, and a Project task reads no private lane",
    "task.clarifying": "a gate id",
    "task.planned": "the plan is the shared work (a moved task's earlier wording is an accepted residual)",
    "task.status": "a status and the machine's reason",
    "task.dequeued": "queue timing",
    "step.dequeued": "queue timing",
    "task.moved": "project ids",
    "task.renamed": "a title every member already reads on the task",
    "task.cancelled": "a reason",
    "task.delivered": "the answer: the work product the Project shares",
    "task.continued": "a person's follow-up, said to the task, and its budget dials",
    "task.proposed": "the Lead's proposed goal, on the shared thread",
    "task.feedback": "a thumb",
    "plan.edited": "a steer or replan, said to the task",
    "step.state": "step lifecycle, title and error detail",
    "step.narration": "the step's own output: work product",
    "tool.called": "locators only, never args or results (Rule 9)",
    "gate.resolved": "a person's answer, said to the task",
    "usage.recorded": "provider, model, units, cost",
    "artifact.produced": "artifact metadata",
    "artifact.revised": "artifact metadata",
    "artifact.snapshot": "the artifact's own text: work product",
    "verification.signed": "a verdict on an artifact",
    "qa.reviewed": "an editorial verdict on the work product",
    "escalation.raised": "a diagnosis of the work",
    # Lane 2: live twins of the record above, never persisted, never replayed
    "message.start": "the narration stream's opening (twin of step.narration)",
    "message.delta": "the narration stream (twin of step.narration)",
    "message.end": "the narration stream's close",
    "artifact.delta": "the artifact stream (twin of artifact.snapshot)",
    "progress": "a sign of life",
    "activity": "tool labels are locators (Rule 9); thinking is the step's own live reasoning",
    # presence: who is here, state not content
    "presence.join": "who is here",
    "presence.leave": "who left",
    "presence.here": "who is still here",
    "presence.typing": "who is typing, and in which mode",
    # the Machine: lifecycle facts - a profile, reasons, timings, sizes, the actor
    "machine.created": "a profile and the fork point it was seeded from",
    "machine.slept": "why and when it slept, and how many bytes the checkpoint dropped",
    "machine.woke": "the turn, the profile and whether the browser came back",
    "machine.rebuilt": "why it started fresh, and the retention it ran out of",
    "machine.reset": "who reset it; no payload",
    # the screen: who drives, and where an agent acted - host and path, never a query
    "machine.control_claimed": "who took control, and whether privately",
    "machine.control_ended": "why and when a lease ended, and who ended it",
    "screen.state": "who drives, whether privately, and until when",
    "screen.action": "the verb, the box it flashed and the host and path (never a query string)",
    # steering: the steer's text rides the shared thread already; these are its outcome
    "steer.applied": "the step title every member sees, the author's name and the safe point",
    "steer.rejected": "the rule's reason, shown to everyone by design",
    "steer.retracted": "which steer was withdrawn",
    "pause.applied": "where the pause took hold",
    "task.budget_set": "the new and previous ceilings",
}

_PRIVATE = "PINEAPPLE-42"  # the creator's codeword; it must never reach a teammate
_STEP = StepId(uuid.uuid4())


def _private_samples() -> dict[str, BaseModel]:
    """One sample per redacted type, each carrying private content in the field that hides it."""
    stub = [MemoryStub(id=MemoryId(uuid.uuid4()), kind="fact", text=f"My codeword is {_PRIVATE}")]
    return {
        "context.assembled": ContextAssembled(
            seq=1,
            task_id=T,
            at=NOW,
            lanes=[
                ContextLaneView(
                    lane="user", tokens=9, items=[ContextItemView(preview=_PRIVATE, memory_id=USER_MEM)]
                ),
                ContextLaneView(
                    lane="bot", tokens=9, items=[ContextItemView(preview=_PRIVATE, memory_id=BOT_MEM)]
                ),
            ],
            memory_ids=[USER_MEM, BOT_MEM],
        ),
        "memory.written": MemoryWritten(seq=2, task_id=T, at=NOW, count=1, items=stub),
        "bot_memory.written": BotMemoryWritten(seq=3, task_id=T, at=NOW, bot="scout", count=1, items=stub),
        "step.prompt": StepPrompt(
            seq=4, task_id=T, step_id=_STEP, at=NOW, attempt=1, agent="writer", prompt=f"remember {_PRIVATE}"
        ),
        # a connector approval's arguments run on the creator's credential; teammates read the call
        "gate.opened": GateOpened(
            seq=5,
            task_id=T,
            at=NOW,
            gate_id=GateId(uuid.uuid4()),
            kind=GateKind.APPROVE,
            title="Allow Gmail: send_email?",
            reason=f"Gmail: send_email\nbody: {_PRIVATE}",
            source="connector",
        ),
    }


_REDACTED = frozenset(_private_samples())


def _wire_types() -> dict[str, type[BaseModel]]:
    """Every member of the wire union, by its `type` - read off `TaskEvent` itself (the union the
    stream validates and the contract exports), never a hand list that could miss a new one."""
    union, *_ = get_args(events.TaskEvent.__value__)  # Annotated[A | B | ..., Field(discriminator)]
    members: tuple[type[BaseModel], ...] = get_args(union)
    return {str(m.model_fields["type"].default): m for m in members}


def test_every_event_model_is_in_the_wire_union() -> None:
    """A task event model defined in `schemas/events.py` but left out of `TaskEvent` would dodge
    the classification below: the module and the union must agree."""
    defined = {
        str(v.model_fields["type"].default)
        for v in vars(events).values()
        if isinstance(v, type) and issubclass(v, BaseModel) and {"type", "task_id"} <= v.model_fields.keys()
    }
    assert defined == set(_wire_types())


def test_every_event_type_is_classified_for_redaction() -> None:
    wire = set(_wire_types())
    unclassified = wire - _REDACTED - _SAFE.keys()
    assert not unclassified, (
        f"new event type(s) {sorted(unclassified)}: decide whether each can carry private memory, a "
        "private conversation or a step prompt - then redact it in schemas/events.redact_for (and "
        "add a sample to _private_samples) or list it in _SAFE with the reason it is safe"
    )
    assert not (_REDACTED & _SAFE.keys()), "a type is either redacted or safe, never both"
    assert not (_REDACTED | _SAFE.keys()) - wire, "classified types that no longer exist"
    assert all(reason.strip() for reason in _SAFE.values()), "every safe type says why"


@pytest.mark.parametrize("name", sorted(_REDACTED))
def test_a_redacted_type_hides_its_private_content_from_everyone_but_the_creator(name: str) -> None:
    event = _private_samples()[name]
    assert _PRIVATE in event.model_dump_json()

    assert redact_for(event, viewer_id=ADA, creator_id=ADA) is event  # the creator reads it all

    seen = redact_for(event, viewer_id=BOB, creator_id=ADA)
    assert type(seen) is type(event)
    assert _PRIVATE not in seen.model_dump_json(), f"{name} leaks to a teammate"
    assert str(USER_MEM) not in seen.model_dump_json() and str(BOT_MEM) not in seen.model_dump_json()
    dumped = event.model_dump()
    assert seen.model_dump()["seq"] == dumped["seq"]  # cursors hold: only contents go
    assert seen.model_dump().get("count") == dumped.get("count")  # meters hold


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: str(s.model_dump()["type"]))
def test_a_safe_type_reaches_a_teammate_untouched(sample: BaseModel) -> None:
    """`_SAFE` means what it says: `redact_for` hands a non-creator the very same frame. A type
    that gains redaction must move to the redacted set (with its private sample)."""
    name = str(sample.model_dump()["type"])
    if name in _REDACTED:
        pytest.skip("redacted: covered by its private sample")
    assert redact_for(sample, viewer_id=BOB, creator_id=ADA) is sample
