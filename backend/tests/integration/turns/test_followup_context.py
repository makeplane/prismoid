# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation contract (ADR-018), pinned.

A follow-up CONTINUES the task - same row, next turn, whole thread in hand - and fork is the
only branch. This file replaced the child-task-era pins when the pivot landed;
the two 2026-07 blindness bugs live on as transcript + verification assertions: the Lead
must know what earlier turns produced, and a prior turn's deliverable must resolve as a
verifiable source.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.feature import Feature
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, PolicyDenied
from prismoid.operations import artifacts as artifact_ops
from prismoid.operations import execution, planning, planning_steps, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations.execution.express import express_prompt
from prismoid.operations.execution.mounts import own_prior_artifacts, parent_artifacts
from prismoid.operations.verification import _sources_for  # pyright: ignore[reportPrivateUsage]
from prismoid.schemas import events as ev
from prismoid.schemas.artifacts import Manifest, ManifestEntry
from prismoid.schemas.ids import ArtifactId, TaskId
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.longform import FigureSpec, OutlineResult, SectionOutline
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import StepSpec, TaskMode, TaskStatus
from prismoid.schemas.steering import SafePoint
from prismoid.schemas.thread import ThreadTranscript
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


async def _settle(session: AsyncSession, task: Task, *, status: TaskStatus = TaskStatus.DELIVERED) -> None:
    """Walk the machine to a settled status the legal way - created → planning → running → end."""
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(status)
    await session.flush()


async def _narrate(session: AsyncSession, task: Task, markdown: str) -> None:
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        ev.StepNarration(seq=seq, task_id=TaskId(task.id), at=utcnow(), markdown=markdown),
    )


async def _delivered_event(session: AsyncSession, task: Task, summary: str) -> None:
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        ev.TaskDelivered(seq=seq, task_id=TaskId(task.id), at=utcnow(), summary=summary),
    )


# ── the turn machinery ────────────────────────────────────────────────────────────────────────


async def test_a_follow_up_continues_the_same_task(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    """The whole pivot in one test: same id, next turn, `planning`, the durable
    `task.continued` frame, the dials applied, and the per-turn job id on the wire."""
    async with session_scope() as session:
        actor, _ = await member(session, "Continuer")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="Write the Q3 memo")
        await _settle(session, task)
        await session.commit()

        same = await task_ops.continue_task(
            session,
            actor,
            task.id,
            goal="now shorten it to one page",
            budget={"usd": "2.50"},
            inputs=[RawInput(filename="style.md", data=b"# tone: dry\n")],
            client_token="turn-2",
        )

        assert same.id == task.id, "a follow-up NEVER mints a new task"
        assert same.turn == 2
        assert same.status == TaskStatus.PLANNING.value
        assert same.budget == {"usd": "2.50"}, "a follow-up may raise the cap"
        assert [i["filename"] for i in same.inputs] == ["style.md"], "new attachments append"

        latest = await TaskEvent.latest_of_type(session, task.id, "task.continued")
        assert isinstance(latest, ev.TaskContinued)
        assert latest.message == "now shorten it to one page"
        assert latest.turn == 2 and latest.client_token == "turn-2"
        task_id = task.id

    assert ("run_task", f"task-{task_id}-t2") in recording_dispatcher.enqueues, (
        "turn 2 runs under its own workflow id - a completed DBOS id never re-executes"
    )


async def test_a_replayed_continue_gesture_returns_the_same_turn(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Twice")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="draft the memo")
        await _settle(session, task)
        await session.commit()

        first = await task_ops.continue_task(
            session, actor, task.id, goal="now shorten it", client_token="gesture-2"
        )
        assert first.turn == 2

        # The replay lands while the turn is LIVE (planning) - absorbed, never a 409, never turn 3.
        again = await task_ops.continue_task(
            session, actor, task.id, goal="now shorten it", client_token="gesture-2"
        )
        assert again.id == task.id and again.turn == 2

        continued = await TaskEvent.of_types(session, task.id, ("task.continued",))
        assert len(continued) == 1, "one gesture, one turn - no matter how often it replays"


async def test_continue_of_a_live_task_is_refused_toward_steering() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Eager")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="draft the memo")
        with pytest.raises(Conflict, match="steer"):
            await task_ops.continue_task(session, actor, task.id, goal="also add a chart")


async def test_continue_refuses_an_archived_task() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Archivist")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="draft the memo")
        await _settle(session, task)
        task.archived_at = utcnow()
        await session.flush()

        with pytest.raises(Conflict, match="unarchive"):
            await task_ops.continue_task(session, actor, task.id, goal="one more thing")


async def test_failed_and_cancelled_threads_reopen_into_planning() -> None:
    """ChatGPT parity: you can keep talking after an error or a stop - the follow-up carries
    the failure context (via the transcript) into the next attempt."""
    async with session_scope() as session:
        actor, _ = await member(session, "Retrier")
        await session.flush()

        for status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            task = await task_ops.create_task(session, actor, goal=f"a task that ends {status.value}")
            await _settle(session, task, status=status)
            await session.commit()

            same = await task_ops.continue_task(session, actor, task.id, goal="try again, but smaller")
            assert same.id == task.id and same.turn == 2
            assert same.status == TaskStatus.PLANNING.value


async def test_a_turn_may_not_shadow_an_earlier_turns_attachment() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Shadow")
        await session.flush()

        task = await task_ops.create_task(
            session, actor, goal="draft it", inputs=[RawInput(filename="brief.md", data=b"v1")]
        )
        await _settle(session, task)
        await session.commit()

        with pytest.raises(PolicyDenied, match=r"brief\.md"):
            await task_ops.continue_task(
                session,
                actor,
                task.id,
                goal="use the new brief",
                inputs=[RawInput(filename="brief.md", data=b"v2")],
            )


async def test_a_stale_turn_workflow_cannot_write_over_the_next() -> None:
    """The ghost-writer guard: a crash-recovered turn-1 workflow re-running an un-journalled
    deliver/fail/mark while turn 2 is live must write NOTHING - without it, the recovered
    deliver finds turn 2's steps 'unfinished' and fails the task the user just continued."""
    async with session_scope() as session:
        actor, _ = await member(session, "Ghost")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="turn one")
        await _settle(session, task)
        await session.commit()
        await task_ops.continue_task(session, actor, task.id, goal="turn two")

        await execution.deliver(session, task.id, "stale summary", turn=1)
        await session.refresh(task)
        assert task.status == TaskStatus.PLANNING.value, "a superseded turn cannot deliver"

        await execution.fail(session, task.id, "stale crash", turn=1)
        await session.refresh(task)
        assert task.status == TaskStatus.PLANNING.value, "…or fail"

        await execution.advance(session, task.id, TaskStatus.RUNNING, turn=1)
        await session.refresh(task)
        assert task.status == TaskStatus.PLANNING.value, "…or flip any status"

        # The turn that owns the run still moves it.
        await execution.advance(session, task.id, TaskStatus.RUNNING, turn=2)
        await session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value


# ── materials continuity ──────────────────────────────────────────────────────────────────────


def _artifact(task: Task, name: str, body: bytes, *, created_at: dt.datetime) -> Artifact:
    digest = storage.put(body)
    row_id = uuid.uuid4()
    return Artifact(
        id=row_id,
        lineage_id=row_id,
        task_id=task.id,
        type="document",
        title=name,
        sha256=digest,
        bytes=len(body),
        storage_key=storage.key_for(digest),
        provenance={"path": f"/artifacts/{name}", "sources": []},
        created_at=created_at,
    )


async def test_own_prior_artifacts_cut_at_the_turn_boundary() -> None:
    """Turn N mounts what the thread had ALREADY delivered when the turn opened - stable
    across retries, and blind to same-turn parallel publishes (two fleet seats sharing a
    filename must never read each other as 'prior')."""
    async with session_scope() as session:
        actor, _ = await member(session, "Mounter")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="write the memo")
        await _settle(session, task)
        before = _artifact(task, "memo.md", b"turn one's memo", created_at=utcnow() - dt.timedelta(hours=1))
        session.add(before)
        await session.commit()

        await task_ops.continue_task(session, actor, task.id, goal="now shorten it")
        after = _artifact(task, "draft2.md", b"mid-turn scratch", created_at=utcnow() + dt.timedelta(hours=1))
        session.add(after)
        await session.flush()

        prior = await own_prior_artifacts(session, task)
        assert [a.id for a in prior] == [before.id], (
            "earlier turns' deliverables mount; the current turn's own publishes do not"
        )


async def test_verification_resolves_a_prior_turns_artifact() -> None:
    """The old inherited-source blindness, on the new spine: a turn-2 deliverable that cites
    the thread's own earlier memo must verify against those exact bytes."""
    async with session_scope() as session:
        actor, _ = await member(session, "Verifier")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="write the memo")
        memo = b"# Q3 memo\nRevenue rose 12%.\n"
        session.add(_artifact(task, "memo.md", memo, created_at=utcnow() - dt.timedelta(hours=1)))
        await session.flush()

        entry = ManifestEntry.model_validate(
            {
                "path": "/artifacts/memo-fr.md",
                "type": "document",
                "title": "Le mémo",
                "sha256": "0" * 64,
                "bytes": 1,
                "sources": ["/task/inputs/memo.md"],
                "agent": "writer",
                "model": "anthropic:claude-opus-4-8",
                "skill_pins": [],
                "at": "2026-08-06T00:00:00Z",
            }
        )
        sources = await _sources_for(session, task, entry)

    assert sources == {"memo.md": memo}, "a prior turn's deliverable is a verifiable source"


async def test_harvest_chains_a_republished_filename_across_turns() -> None:
    """A turn-2 re-publish of turn 1's `memo.md` is the deliverable's next revision on the
    SAME lineage - announced as `artifact.revised`, exactly like a human edit - so
    lineage-latest mounting never collides two files wearing one name."""
    async with session_scope() as session:
        actor, _ = await member(session, "Chainer")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="write the memo")
        await _settle(session, task)
        first = _artifact(task, "memo.md", b"turn one's memo", created_at=utcnow() - dt.timedelta(hours=1))
        session.add(first)
        await session.commit()

        await task_ops.continue_task(session, actor, task.id, goal="revise the memo")
        view = await planning.draft_plan(session, task.id)  # the Lead's one step
        await session.commit()

        body = b"turn two's revision"
        manifest = Manifest.model_validate(
            {
                "entries": [
                    {
                        "path": "/artifacts/memo.md",  # the SAME published basename
                        "type": "document",
                        "title": "memo, revised",
                        "sha256": storage.sha256(body),
                        "bytes": len(body),
                        "sources": [],
                        "agent": "",
                        "model": "",
                        "skill_pins": [],
                        "at": "2026-08-06T01:00:00Z",
                    }
                ]
            }
        )
        [chained] = await artifact_ops.harvest(
            session, task.id, view.steps[0].id, manifest, {"/artifacts/memo.md": body}
        )
        await session.commit()

        assert chained.lineage_id == first.lineage_id
        assert chained.revision == 2 and chained.supersedes_id == first.id

        revised = await TaskEvent.of_types(session, task.id, ("artifact.revised",))
        assert len(revised) == 1, "the record announces which id is the lineage's face now"


class _Frame:
    """The frame's write side: what a step could read under /task, and how it was written."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, int, str]] = {}

    async def write_bytes(self, path: str, data: bytes, *, mode: int, owner: str) -> None:
        self.files[path] = (data, mode, owner)


def _section_outline(*briefs: str, kept: str = "") -> OutlineResult:
    """Two sections; `kept` makes the first a kept section (a revision's `kept_from`)."""
    return OutlineResult(
        filename="egress-report",
        title="Egress Controls",
        format_target="pdf",
        continuity_bible="Voice: practitioner. The sandbox is always 'the sandbox'.",
        summary="A report on egress controls.",
        sections=[
            SectionOutline(
                id=f"s{i + 1:02d}",
                title=title,
                brief=brief,
                target_words=900,
                kept_from=kept if i == 0 else "",
            )
            for i, (title, brief) in enumerate(zip(("Threat Model", "DNS"), briefs, strict=True))
        ],
    )


async def test_a_longform_follow_up_mounts_the_text_its_last_document_was_assembled_from() -> None:
    """Shown only the delivered PDF, a revising outline sent each kept section's writer to copy from
    a file `read_file` cannot read. Turn 2's outline step now mounts the markdown turn 1's assembly
    read beside the delivered file, and every part of the revision mounts those files too, so the
    parts share one head; the part an outline keeps is published from its file."""
    from prismoid.ai.frames.layout import input_path
    from prismoid.db.models.plan import PlanEdit as PlanEditRow
    from prismoid.db.models.plan import PlanStep
    from prismoid.operations import longform as longform_ops
    from prismoid.operations.execution.mounts import (
        materialise_inputs,
        prior_figures,
        prior_sections,
        upstream_filename,
    )
    from prismoid.schemas.plan import StepState

    async with session_scope() as session:
        actor, _ = await member(session, "Reviser")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="a report on egress controls")
        await _settle(session, task)

        # Turn 1's lane, as its outline expanded: two parts in order, one assembly.
        [(outline_id, architect)], _, _ = longform_ops.longform_plan(task.goal, target_pages=45)
        wave, edges = longform_ops.expand(
            _section_outline("Cover the threat model.", "Cover DNS."),
            source_paths=[],
            goal=task.goal,
            outline_step=outline_id,
        )
        rows = await planning_steps.write_steps(
            session, task, [(outline_id, architect), *wave], edges, base_ordinal=0
        )
        for row in rows:
            row.state = StepState.DONE.value
        step = {row.title: row.id for row in rows}
        earlier = utcnow() - dt.timedelta(hours=1)
        published = {
            "01-threat-model.md": (step["01 Threat Model"], b"# Threat Model\n\nkept"),
            "02-dns.md": (step["02 DNS"], b"# DNS\n\nraw"),
            "egress-controls.pdf": (step["Assemble - Egress Controls"], b"%PDF-1.7"),
            "fig-s01-1.png": (outline_id, b"\x89PNG kept figure"),  # bought by turn 1's outline
        }
        for name, (step_id, body) in published.items():
            row = _artifact(task, name, body, created_at=earlier)
            row.step_id, row.role = step_id, "deliverable" if name.endswith(".pdf") else "working"
            session.add(row)
        # A later turn bought the same id again and never delivered (cancelled, live): its image is
        # not the one the last document shows, so it is not the one a kept section keeps.
        rebought = _artifact(task, "fig-s01-1.png", b"\x89PNG never delivered", created_at=utcnow())
        rebought.step_id, rebought.role = outline_id, "working"
        session.add(rebought)
        await session.commit()

        await task_ops.continue_task(session, actor, task.id, goal="revise DNS, keep the threat model")
        last = await prior_sections(session, task)
        assert [upstream_filename(a) for a in last] == ["01-threat-model.md", "02-dns.md"]

        # Turn 2's architect: shown every one, mounted read-only beside the delivered file.
        shown = [input_path(upstream_filename(a)) for a in last]
        [(revise_id, reviser)], _, _ = longform_ops.longform_plan(
            "revise DNS, keep the threat model", target_pages=45, prior_sections=shown
        )
        assert reviser.contract is not None and reviser.contract.inputs == shown
        frame = _Frame()
        mounted = await materialise_inputs(
            session,
            task,
            frame,
            carried=await prior_sections(session, task, named=reviser.contract.inputs),
        )
        assert frame.files["/task/inputs/01-threat-model.md"] == (b"# Threat Model\n\nkept", 0o444, "system")
        assert frame.files["/task/inputs/02-dns.md"][0] == b"# DNS\n\nraw"
        assert "/task/inputs/egress-controls.pdf" in mounted, "the delivered file stays mounted"

        # Its outline keeps section 01: that part is published from its file.
        [revise_row] = await planning_steps.write_steps(
            session, task, [(revise_id, reviser)], [], base_ordinal=len(rows)
        )
        revision = _section_outline(
            "Unchanged.",
            "Add DNS rebinding and DNS-over-HTTPS, each with its control.",
            kept="/task/inputs/01-threat-model.md",
        )
        # The architect repeats the kept section's figure spec, as it did live: nothing is bought.
        kept_spec = revision.sections[0].model_copy(
            update={"figures": [FigureSpec(id="fig-s01-1", spec="reuse the published diagram")]}
        )
        revision = revision.model_copy(update={"sections": [kept_spec, *revision.sections[1:]]})
        await longform_ops.expand_after_outline(session, task, revise_row, revision)
        expanded = await PlanEditRow.latest_expand(session, task.id)
        assert expanded is not None
        assert OutlineResult.model_validate(expanded.payload).sections[0].figures == [], (
            "the figure job reads this payload: a kept section's spec would buy a new image"
        )
        turn_two = [
            (row.title, StepSpec.model_validate(row.spec))
            for row in await PlanStep.for_task(session, task.id)
            if row.id not in step.values()
        ]
        writers = {title: spec.contract for title, spec in turn_two if spec.lane_role == "section"}
        assert [spec.kept_from for _title, spec in turn_two if spec.lane_role == "section"] == [
            "/task/inputs/01-threat-model.md",
            None,
        ]
        # The kept section's figure marker still points at turn 1's image: assembly mounts it.
        [assembly] = [spec.contract for _title, spec in turn_two if spec.lane_role == "assembly"]
        assert assembly is not None and assembly.inputs == ["/task/inputs/fig-s01-1.png"]
        frame = _Frame()
        await materialise_inputs(
            session, task, frame, carried=await prior_figures(session, task, named=assembly.inputs)
        )
        assert frame.files["/task/inputs/fig-s01-1.png"] == (b"\x89PNG kept figure", 0o444, "system")
        kept, revised = writers["01 Threat Model"], writers["02 DNS"]
        assert kept is not None and revised is not None
        both = ["01-threat-model.md", "02-dns.md"]
        assert [upstream_filename(a) for a in await prior_sections(session, task, named=kept.inputs)] == both
        assert kept.inputs == revised.inputs, "every part mounts the same files: one cached head"


# ── the conversation reaches the Lead ─────────────────────────────────────────────────────────


async def test_turn_two_planning_reads_the_whole_conversation() -> None:
    """The context-reset bug, pinned dead: the turn-2 brief leads with the NEW message and
    carries turn 1's actual words - not a 400-token précis of a title."""
    async with session_scope() as session:
        actor, _ = await member(session, "Historian")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="Write the Q3 memo")
        await _narrate(session, task, "Revenue rose 12%; margins held at 41%.")
        await _delivered_event(session, task, "Revenue rose 12%; margins held at 41%.")
        await _settle(session, task)
        await session.commit()

        await task_ops.continue_task(session, actor, task.id, goal="now shorten it to one page")

        transcript = await thread_ops.render_thread(
            session, task, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS
        )
        assert transcript.current_ask == "now shorten it to one page"
        assert not transcript.is_empty()

        brief = express_prompt(transcript.current_ask, transcript, ambient="")
        thread, _, ask = brief.rpartition("now shorten it to one page")
        assert thread and "now shorten" not in ask, "the turn-N Lead reads the NEW ask after the thread"
        assert "Write the Q3 memo" in thread, "turn 1's ask rides in the conversation"
        assert "margins held at 41%" in brief, "turn 1's actual words, not a précis"
        assert "[delivered]" in brief


async def test_a_forks_transcript_carries_the_parent_thread_and_its_launch_steer() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Brancher")
        await session.flush()

        parent = await task_ops.create_task(session, actor, goal="Write the Q3 memo")
        await _narrate(session, parent, "Revenue rose 12%; margins held.")
        await _delivered_event(session, parent, "Wrote the memo.")
        await session.commit()

        child = await task_ops.fork(session, actor, parent.id)
        await steering.steer(session, actor, child.id, "make it about the EU market")

        transcript = await thread_ops.render_thread(
            session, child, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS
        )
        rendered = " ".join(e.text for t in transcript.turns for e in t.entries)
        assert "Revenue rose 12%; margins held." in rendered, "the parent's thread rides in"
        assert "make it about the EU market" in rendered, "the launch directive reaches planning"
        assert "[the user forked the conversation here]" in rendered
        opening = [
            e
            for t in transcript.turns
            for e in t.entries
            if e.kind == "user" and e.text == "Write the Q3 memo"
        ]
        assert len(opening) == 1, "the fork's copied goal is never read out twice"


# ── a fork reads its parent as it stood at the fork ───────────────────────────────────────────


async def _parent_goes_on_privately(
    session: AsyncSession, actor: Actor, parent: Task, memo: Artifact
) -> None:
    """The parent's NEXT turn, after the fork: its words, a revision of the memo the fork
    inherited, and a new file - everything the fork must never read."""
    await task_ops.continue_task(session, actor, parent.id, goal="PRIVATE: add my salary numbers")
    await _narrate(session, parent, "Ada's salary is $310k.")
    later = utcnow() + dt.timedelta(hours=1)
    revised = _artifact(parent, "memo.md", b"memo with the salary", created_at=later)
    revised.lineage_id, revised.revision, revised.supersedes_id = memo.lineage_id, 2, memo.id
    session.add_all([revised, _artifact(parent, "salary.md", b"$310k", created_at=later)])
    await session.commit()


async def _settled_parent(
    session: AsyncSession, actor: Actor, *, project_id: uuid.UUID | None
) -> tuple[Task, Artifact]:
    parent = await task_ops.create_task(session, actor, goal="Write the Q3 memo", project_id=project_id)
    await _narrate(session, parent, "Revenue rose 12%; margins held.")
    await _settle(session, parent)
    memo = _artifact(parent, "memo.md", b"turn one's memo", created_at=utcnow() - dt.timedelta(hours=1))
    session.add(memo)
    await session.commit()
    return parent, memo


def _said(transcript: ThreadTranscript) -> str:
    return " ".join(e.text for t in transcript.turns for e in t.entries)


async def test_a_fork_reads_its_parent_only_up_to_the_fork() -> None:
    """A fork is a branch of the conversation as it stood: the parent's later turns, and what
    they publish, are the other branch. Ada's own fork, moved into a Project, must not carry her
    personal parent's later private turn to the room - the bound, not visibility, guards this."""
    async with session_scope() as session:
        ada, _ = await member(session, "Brancher")
        project = (await project_ops.create(session, ada, name=f"room-{uuid.uuid4().hex[:6]}")).project
        parent, memo = await _settled_parent(session, ada, project_id=None)

        fork = await task_ops.fork(session, ada, parent.id)
        await steering.move(session, ada, fork.id, project.id)
        await _parent_goes_on_privately(session, ada, parent, memo)

        said = _said(
            await thread_ops.render_thread(session, fork, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS)
        )
        assert "Revenue rose 12%; margins held." in said, "what the fork branched from rides in"
        assert "salary" not in said.lower()
        inherited = await parent_artifacts(session, fork)
        assert [a.id for a in inherited] == [memo.id], "the parent's shelf as it stood at the fork"


async def test_a_fork_carries_no_steer_its_parent_had_not_applied() -> None:
    """A fork carries only applied instructions (a fork made while Bob's steer was open
    followed it). The steer belongs to the parent's run: the parent applies it after the fork, and
    still the fork's Lead and mounted thread read only that it was not carried."""
    async with session_scope() as session:
        ada, workspace = await member(session, "Ada")
        bob, _ = await member(session, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(session, ada, name=f"room-{uuid.uuid4().hex[:6]}")).project
        session.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        parent = await task_ops.create_task(session, ada, goal="Write the Q3 memo", project_id=project.id)
        parent.transition(TaskStatus.PLANNING)
        parent.transition(TaskStatus.RUNNING)
        await _narrate(session, parent, "Drafting the memo.")
        await session.commit()
        await steering.steer(session, bob, parent.id, "use euros")
        await session.commit()

        fork = await task_ops.fork(session, ada, parent.id)
        await session.commit()
        await session.refresh(parent)
        await steering.apply_planned(session, parent, parent.plan_epoch)  # the parent's run takes it
        await session.commit()

        forked = await thread_ops.render_thread(session, fork, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS)
        entries = [(e.kind, e.text) for t in forked.turns for e in t.entries]
        assert (
            "event",
            "[not carried: the fork was made before this applied] Bob steered: use euros",
        ) in entries
        assert all(kind != "steering" for kind, _ in entries)
        own = await thread_ops.render_thread(session, parent, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS)
        assert ("steering", "use euros") in [(e.kind, e.text) for t in own.turns for e in t.entries]


async def _published_by_a_step(session: AsyncSession, task: Task, row: Artifact) -> None:
    """The harvest's record of a deliverable (`artifact.produced`)."""
    session.add(row)
    await session.flush()
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        ev.ArtifactProduced(
            seq=seq,
            task_id=TaskId(task.id),
            at=utcnow(),
            artifact_id=ArtifactId(row.id),
            artifact_type=ArtifactType.DOCUMENT,
            title=row.title,
            sha256=row.sha256,
            bytes=row.bytes,
        ),
    )


async def _superseded(session: AsyncSession, task: Task, rows: list[Artifact]) -> None:
    """A steer's fence overtook this output: the rows go `superseded` in place."""
    await Artifact.supersede(session, [r.id for r in rows])
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        ev.SteerApplied(
            seq=seq,
            task_id=TaskId(task.id),
            at=utcnow(),
            command_id=uuid.uuid4(),
            safe_point=SafePoint.RERUN,
            superseded=[ArtifactId(r.id) for r in rows],
        ),
    )


async def test_a_fork_inherits_the_deliverable_its_parent_superseded_after_the_fork() -> None:
    """A fork inherits its parent's shelf as it stood at the fork. A steer's fence supersedes
    output by changing its role in place, so a deliverable the parent's run superseded AFTER the
    fork was still its deliverable then, and the fork mounts it (the parent's fence 2
    superseded its memo after the fork, and the fork's writer asked twice where the memo was).
    One superseded before the fork stays out."""
    async with session_scope() as session:
        ada, _ = await member(session, "Brancher")
        parent = await task_ops.create_task(session, ada, goal="Write the Q3 memo")
        parent.transition(TaskStatus.PLANNING)
        parent.transition(TaskStatus.RUNNING)
        early = utcnow() - dt.timedelta(minutes=5)
        draft = _artifact(parent, "draft.md", b"a draft a steer overtook", created_at=early)
        memo = _artifact(parent, "memo.md", b"the memo", created_at=early)
        await _published_by_a_step(session, parent, draft)
        await _superseded(session, parent, [draft])
        await _published_by_a_step(session, parent, memo)
        await session.commit()

        fork = await task_ops.fork(session, ada, parent.id)
        await session.commit()
        await _superseded(session, parent, [memo])  # the parent's fence, after the fork
        await session.commit()

        assert [a.id for a in await parent_artifacts(session, fork)] == [memo.id]


async def test_a_teammates_fork_reads_nothing_of_a_parent_taken_home() -> None:
    async with session_scope() as session:
        ada, workspace = await member(session, "Ada")
        bob, _ = await member(session, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(session, ada, name=f"room-{uuid.uuid4().hex[:6]}")).project
        session.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await session.flush()
        parent, memo = await _settled_parent(session, ada, project_id=project.id)

        fork = await task_ops.fork(session, bob, parent.id)
        await steering.move(session, ada, parent.id, None)  # she takes it home...
        await _parent_goes_on_privately(session, ada, parent, memo)  # ...and works on it alone

        said = _said(
            await thread_ops.render_thread(session, fork, budget_tokens=thread_ops.MOUNT_THREAD_TOKENS)
        )
        assert "salary" not in said.lower()
        assert await parent_artifacts(session, fork) == [], "Bob can no longer open her task"


# ── fork = the real branch ────────────────────────────────────────────────────────────────────


async def test_fork_carries_the_parents_dials_and_holds() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Forker")
        await session.flush()

        parent = await task_ops.create_task(session, actor, goal="draft the memo", budget={"usd": "3.00"})
        await steering.update_meta(session, actor, parent.id, title="Q3 memo")

        child = await task_ops.fork(session, actor, parent.id, client_token="fork-1")
        assert child.title == "[Forked] Q3 memo"
        assert child.goal == parent.goal and child.parent_task_id == parent.id
        assert child.budget == {"usd": "3.00"}, "the branch inherits the cap"
        assert child.clarify == "never", "the first steer is the branch's composer"
        assert child.status == "created" and child.held_at is not None
        stale = await Task.stale_created(session, utcnow() + dt.timedelta(days=1))
        assert child.id not in {t.id for t in stale}, "held forks are parked, not orphaned"

        replay = await task_ops.fork(session, actor, parent.id, client_token="fork-1")
        assert replay.id == child.id, "the guardrail covers forks too"

        grandchild = await task_ops.fork(session, actor, child.id)
        assert grandchild.title == "[Forked] Q3 memo", "forking a fork wears ONE prefix"

        await steering.steer(session, actor, child.id, "make it about the EU market")
        await session.refresh(child)
        assert child.held_at is None, "the first directive launches the fork"


async def test_cancel_clears_a_held_forks_hold() -> None:
    """Without this a cancelled held fork said 'holding' forever - terminal, unsteerable,
    and wearing a hold nothing could ever clear."""
    async with session_scope() as session:
        actor, _ = await member(session, "Canceller")
        await session.flush()

        parent = await task_ops.create_task(session, actor, goal="draft the memo")
        child = await task_ops.fork(session, actor, parent.id)
        assert child.held_at is not None

        await steering.cancel(session, actor, child.id)
        await session.refresh(child)
        assert child.status == "cancelled" and child.held_at is None


async def test_fork_of_a_fleet_task_skips_the_flag_recheck() -> None:
    """Mode is thread-DNA: the flag gated the CHOICE at the parent's submit; flipping it
    later must not orphan a settled thread's branch with a 409."""
    async with session_scope() as session:
        actor, _ = await member(session, "Fleet")
        await session.flush()
        project = Project(name="fleet-room", owner_actor_id=actor.id)
        session.add(project)
        await session.flush()
        flag = Feature(name="prismoid:fleet", project_id=project.id, enabled=True)
        session.add(flag)
        await session.flush()

        parent = await task_ops.create_task(
            session, actor, goal="compare options", mode=TaskMode.FLEET, project_id=project.id
        )
        flag.enabled = False
        await session.flush()

        child = await task_ops.fork(session, actor, parent.id)
        assert child.mode == TaskMode.FLEET.value == parent.mode
