# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounting a step's world: upstream artifacts, task inputs, pinned skills.

Split from operations/execution.py, which had reached 1,173 lines.
Same layer, same rules; `execution/__init__` re-exports every public name.

Skills mount read-only and root-owned like the sources: a skill is guidance the system gave the agent,
and an agent that can rewrite its loaded skill has laundered what a verifier may hold it to. Their
folders are the agent's, made first by one `mkdir`, because `reset_mounts` empties `/skills` as the
agent at every step boundary and cannot delete inside a folder `write_bytes` made as root. That
command is built only from a stored name and paths re-checked against the skill rules, and quoted.
Nothing is pinned until plan-time resolution lands; `prismoid dev skill-frame` and the tests exercise it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import shlex
import uuid
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath

import logfire
import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.frames.layout import FramePaths, input_path
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied
from prismoid.operations import sources as sources_ops
from prismoid.schemas.events import ArtifactProduced, SteerApplied
from prismoid.schemas.ids import ArtifactId
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.plan import StepSpec, StepState

# A delta is a *preview*, not the document: 3500 bytes is what fits inside NOTIFY's ceiling
# alongside the envelope, and what a reader can take in.


async def upstream_artifacts(session: AsyncSession, task: Task, step: PlanStep) -> list[Artifact]:
    """What the steps this one depends on **published**.

    This is the whole difference between a chain and a charade. The planner puts a researcher in
    front of a writer because the writer needs what the researcher found - and if those findings do
    not arrive in the writer's sandbox, the writer reads the original sources, writes the memo it
    would have written anyway, and the research step is an expense with a narration attached. It
    would still *look* like it worked.
    """
    edges = await PlanEdgeRow.for_task(session, task.id)
    upstream = {e.from_step_id for e in edges if e.to_step_id == step.id}
    if not upstream:
        return []
    # Output a steer overtook is on the record, never a source.
    return [
        a
        for a in await Artifact.for_task(session, task.id)
        if a.step_id in upstream and a.role != "superseded"
    ]


async def superseded_output(session: AsyncSession, task: Task, step: PlanStep) -> list[Artifact]:
    """The output a steer overtook that this step runs again for: its own
    superseded attempts (fence 1) or the done sink it re-drafts (fence 2). Newest per name - the
    name it mounts under (`superseded_paths`)."""
    owners = {step.id, StepSpec.model_validate(step.spec).redrafts}
    rows = [
        a for a in await Artifact.for_task(session, task.id) if a.step_id in owners and a.role == "superseded"
    ]
    newest: dict[str, Artifact] = {}
    for artifact in sorted(rows, key=lambda a: a.created_at, reverse=True):
        newest.setdefault(upstream_filename(artifact), artifact)
    return list(newest.values())


def superseded_paths(artifacts: Sequence[Artifact]) -> tuple[str, ...]:
    """Where a re-run finds the output it revises - the words of its brief and its mount agree."""
    from prismoid.ai.frames.layout import input_path

    return tuple(dict.fromkeys(input_path(upstream_filename(a)) for a in artifacts))


async def has_dependents(session: AsyncSession, task: Task, step: PlanStep) -> bool:
    """Whether a later step reads what this one publishes - the other end of
    `upstream_artifacts`' edges."""
    return any(e.from_step_id == step.id for e in await PlanEdgeRow.for_task(session, task.id))


def seat_filename(model_ref: str) -> str:
    """A fleet answer's mount name, derived from its producing model.

    **One definition, because two sides depend on it agreeing.** The executor mounts each answer
    under this name into the chair's frame (`materialise_inputs`), and the verifier reverses it to
    resolve the chair's sources back to those answers (`verification._sources_for`). When the two
    drifted, the chair's sources matched nothing, the fleet suite starved, and every table shipped
    `unverified` - the debt this closes. `:` and `/` become `-` so the name is a legal path.
    """
    safe = model_ref.replace(":", "-").replace("/", "-")
    return f"answer-{safe}.md"


def upstream_filename(artifact: Artifact, *, seat_names: bool = False) -> str:
    """The name an upstream artifact wears when it becomes a downstream source.

    The path the producer published under (`/work/findings.md` → `findings.md`), because that is the
    name it already calls itself - in its own narration, in the stream the user watched, and in the
    manifest. Renaming it here would mean the writer cites `artifact-3.md` for a document the user
    saw called `findings.md`, and nobody could line the two up.

    `seat_names` is the fleet chair's exception, and it is load-bearing twice over: the
    chair's Model column needs the producing model's name, and two writers that both called their
    file `memo.md` would otherwise MOUNT OVER EACH OTHER - the first fleet run's chair saw one
    answer where two existed. Provenance carries the producing ref; the seat name is derived from
    it, so every answer file is unique and self-identifying.
    """
    model = str(artifact.provenance.get("model") or "")
    if seat_names and model:
        return seat_filename(model)
    published = str(artifact.provenance.get("path") or "")
    return PurePosixPath(published).name or f"{artifact.sha256[:12]}.md"


async def materialise_inputs(
    session: AsyncSession,
    task: Task,
    frame: object,
    upstream: Sequence[Artifact] = (),
    *,
    carried: Sequence[Artifact] = (),
    seat_names: bool = False,
    superseded: Sequence[Artifact] = (),
) -> tuple[str, ...]:
    """The attachments, from the object store into `/task/inputs/`. Returns their frame paths.

    Written as **bytes**, not text: a source PDF that round-trips through `str` is corrupted into
    something that still looks like a file, and only the parser three steps later finds out.

    Written **0o444 and root-owned**, which is the load-bearing part. The verifier's whole job is to
    ask "does this number appear in a source?" - so a source the agent can edit is not a source, it
    is a scratchpad, and the check it feeds is theatre. The frame image locks `/task` itself for the
    same reason (an agent that owns the parent directory can rename the child); this is the other
    half of that lock.
    """
    from prismoid import storage
    from prismoid.ai.context.blocks import render_context_md
    from prismoid.ai.frames.layout import ANSWERS, CONTEXT, THREAD, input_path
    from prismoid.operations import context as context_ops

    inputs = InputSet(items=[TaskInput.model_validate(i) for i in task.inputs])
    paths: list[str] = []

    # **What the user said when asked**. Read-only, root-owned, next to the sources.
    #
    # An answer that lives only in the database is an answer the agent doing the work never sees -
    # and then the question was an interruption the user paid for and got nothing back from. They
    # will remember that, and they will type `--clarify never` for ever after.
    if task.answers:
        await frame.write_bytes(  # type: ignore[attr-defined]
            ANSWERS,
            orjson.dumps(task.answers, option=orjson.OPT_INDENT_2),
            mode=0o444,
            owner="system",
        )
        paths.append(ANSWERS)

    # **What the system remembers** - the Context Pack frozen at planning, rendered as prose and
    # mounted exactly like the answers: read-only, root-owned, and framed as instruction rather
    # than source (`brief.prompt_for` repeats the framing). Every step of the task sees the same
    # file, because the pack is on the row, not recomputed per mount - read through the fence:
    # a Project task mounts no private lane, whoever stored the pack.
    if (pack := await context_ops.readable_pack(session, task)) is not None:
        await frame.write_bytes(  # type: ignore[attr-defined]
            CONTEXT,
            render_context_md(pack).encode(),
            mode=0o444,
            owner="system",
        )
        paths.append(CONTEXT)

    # **The conversation so far** - the thread transcript, rendered to the planning
    # boundary: byte-identical to what this turn's planner read, stable across every retry of
    # the turn, and never including the agent's own in-flight narrations. Same wall as the
    # answers and the pack: an instruction, never a source (`brief.prompt_for` says so too).
    from prismoid.ai.context.thread import render_thread_md
    from prismoid.operations import thread as thread_ops

    planned = await TaskEvent.latest_of_type(session, task.id, "task.planned")
    transcript = await thread_ops.render_thread(
        session,
        task,
        budget_tokens=thread_ops.MOUNT_THREAD_TOKENS,
        until_seq=int(getattr(planned, "seq", 0)) or None,
    )
    if not transcript.is_empty():
        raw = render_thread_md(transcript).encode()
        if len(raw) > thread_ops.THREAD_MD_MAX_BYTES:  # belt; the token budget sizes it under
            raw = raw[: thread_ops.THREAD_MD_MAX_BYTES].decode("utf-8", errors="ignore").encode()
        await frame.write_bytes(THREAD, raw, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(THREAD)

    for item in inputs.items:
        data = await asyncio.to_thread(storage.get, item.sha256)
        if storage.sha256(data) != item.sha256:
            # The key IS the hash, so this cannot happen without something being badly wrong.
            # Saying so costs one line and turns a silent substitution into a loud stop.
            raise PolicyDenied(f"{item.filename}: stored bytes do not match their own digest")
        path = input_path(item.filename)
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)

    # **What the upstream steps published, mounted exactly like a source - because it is one.**
    #
    # The writer's findings arrive read-only and root-owned, next to the brief they were drawn from,
    # and the verifier will hold the writer's memo to *both*. That is the point: a fabrication the
    # researcher smuggled through would be a source the writer could legitimately cite, so the
    # researcher's output is verified before it ever becomes anybody's input (see `researcher.py`).
    for artifact in upstream:
        data = await asyncio.to_thread(storage.get, artifact.sha256)
        path = input_path(upstream_filename(artifact, seat_names=seat_names))
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)

    # **A steer's re-run carries the output it supersedes in, read-only** -
    # the follow-up's mount below, for the same reason: on a fresh frame (Machines off, a seat)
    # `/work` starts empty, and "revise it" needs the thing to revise. Its brief names these paths.
    for artifact in superseded:
        path = input_path(upstream_filename(artifact))
        if path in paths:
            continue
        data = await asyncio.to_thread(storage.get, artifact.sha256)
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)

    # **A continuation carries its own earlier turns' deliverables in, read-only**.
    # "Now shorten that memo" begins with the memo already in `/task/inputs/` - same mount, same
    # permissions, same lineage-latest rule as a fork's inheritance below. An upstream step of
    # THIS turn that already re-published the name keeps the path (its bytes are newer).
    prior_turns = await own_prior_artifacts(session, task)
    for artifact in prior_turns:
        path = input_path(upstream_filename(artifact))
        if path in paths:
            continue
        data = await asyncio.to_thread(storage.get, artifact.sha256)
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)
    await _mount_ledger(session, task, frame)

    # **A long-form follow-up carries the last document's own files in, read-only**,
    # to the steps whose contract names them: its section text to the outline step and to each part
    # writer pointed at one (the deliverable above may be a .pdf or .docx no model can read), and
    # the figures a kept section's markers point at to assembly.
    for artifact in carried:
        path = input_path(upstream_filename(artifact))
        if path in paths:
            continue
        data = await asyncio.to_thread(storage.get, artifact.sha256)
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)

    # **A fork carries the parent's output in, read-only**. "Now translate that
    # memo into French" begins with the memo already in `/task/inputs/`, and cannot rewrite it - the
    # parent's work is a *source*, not a scratchpad. Same mount, same permissions as any other input.
    # A path already mounted (an own revision of the parent's lineage) keeps the newer bytes.
    inherited = await parent_artifacts(session, task) if task.parent_task_id else []
    for artifact in inherited:
        path = input_path(upstream_filename(artifact))
        if path in paths:
            continue
        data = await asyncio.to_thread(storage.get, artifact.sha256)
        await frame.write_bytes(path, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
        paths.append(path)

    logfire.info(
        "materialised {n} input(s) into the frame ({u} upstream, {r} superseded, {t} prior-turn, {p} inherited)",
        n=len(paths),
        u=len(upstream),
        r=len(superseded),
        t=len(prior_turns),
        p=len(inherited),
    )
    return tuple(paths)


async def _mount_ledger(session: AsyncSession, task: Task, frame: object) -> None:
    """A follow-up's or a fork's `sources.jsonl` beside its earlier deliverables: the pages the task kept,
    by source id. A ledger, not a source anything is derived from, so not among the paths a publish stamps."""
    if (task.turn > 1 or task.parent_task_id) and (ledger := await sources_ops.ledger(session, task.id)):
        await frame.write_bytes(input_path(sources_ops.LEDGER), ledger, mode=0o444, owner="system")  # type: ignore[attr-defined]


def _mounted(path: str, data: bytes, paths: FramePaths) -> bytes:
    """A bundled file as it lands. A cloud frame's are byte-for-byte. On the device a `.md` is text
    the model reads and pastes, and its shell knows only real paths, so it is rendered as `SKILL.md`
    is; a file that is not UTF-8 is not text, whatever it is called."""
    if not paths.on_host or not path.lower().endswith(".md"):
        return data
    try:
        return paths.render(data.decode()).encode()
    except UnicodeDecodeError:
        return data


async def materialise_skills(session: AsyncSession, spec: StepSpec, frame: object) -> list[str]:
    """The step's pinned skills, into `/skills/<name>/`: files read-only and root-owned, folders the agent's.
    A stored row is re-checked against today's name and path rules first; one that breaks them is refused."""
    from prismoid.ai.frames.layout import SKILLS
    from prismoid.ai.frames.layout import render as spell
    from prismoid.ai.skills.format import SkillError, check_stored, render
    from prismoid.operations import skills as skill_ops

    if not spec.skill_pins:
        return []

    roots = frame.paths  # type: ignore[attr-defined]
    paths: list[str] = []
    for pin in spec.skill_pins:
        resolved = await skill_ops.resolve_pin(session, pin)
        try:
            name = check_stored(resolved.name, [file.path for file in resolved.files])
        except SkillError as exc:
            raise PolicyDenied(f"a pinned skill cannot be mounted: {exc}") from exc
        skill_dir = f"{SKILLS}/{name}"
        # Bundled files: schema references, validate scripts, templates - mounted
        # read-only beside SKILL.md, which names them. Progressive disclosure: the prompt still
        # says only "read the SKILL.md"; the body points at its own files. A validate script is
        # run as `python3 <path>` - read-only is enough, +x is not needed.
        mounts = {f"{skill_dir}/SKILL.md": spell(render(resolved.manifest, resolved.body), roots).encode()}
        for file in resolved.files:
            mounts[f"{skill_dir}/{file.path}"] = _mounted(file.path, file.data, roots)
        folders = sorted({target.rsplit("/", 1)[0] for target in mounts})
        await frame.exec(shlex.join(["mkdir", "-p", "--", *folders]))  # type: ignore[attr-defined]
        # the model reads SKILL.md and bundled documents, so they speak the frame's paths (`_mounted`)
        for target, data in mounts.items():
            await frame.write_bytes(target, data, mode=0o444, owner="system")  # type: ignore[attr-defined]
            paths.append(target)

    logfire.info(
        "materialised {n} skill(s) into the frame: {refs}",
        n=len(paths),
        refs=[pin.ref() for pin in spec.skill_pins],
    )
    return paths


async def mounted_artifacts(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, upstream: Sequence[Artifact]
) -> list[Artifact]:
    """Every artifact `materialise_inputs` mounts for this step, as `equip` hands them; what a paper frame
    must hold (a double mount counts twice, which errs safe)."""
    named = spec.contract.inputs if spec.contract else ()
    return [
        *upstream,
        *await superseded_output(session, task, step),
        *await prior_sections(session, task, named=named),
        *await prior_figures(session, task, named=named),
        *await own_prior_artifacts(session, task),
        *(await parent_artifacts(session, task) if task.parent_task_id else []),
    ]


async def parent_artifacts(session: AsyncSession, task: Task) -> list[Artifact]:
    """The parent task's published deliverables - what a FORK inherits.

    Lineage-latest AS OF THE FORK: a deliverable the user edited
    before the fork mounts as they last saved it then; a revision or a file the parent made
    afterwards is the other branch's. Nothing when the fork's creator can no longer open the
    parent (`thread.forked_from`, the transcript's own rule). A steer's fence supersedes output
    by changing its role in place, so a row superseded only after the fork is read as
    it stood at the fork's bound: still the parent's deliverable then.
    """
    from prismoid.operations import thread as thread_ops

    fork = await thread_ops.forked_from(session, task, reader_id=task.creator_actor_id)
    if fork is None:
        return []
    parent, bound = fork
    shelf = await Artifact.latest_for_task(session, parent.id, as_of=task.created_at)
    live_then: set[ArtifactId] = set()
    if bound is not None and any(a.role == "superseded" for a in shelf):
        events = await TaskEvent.of_types(
            session, parent.id, ("artifact.produced", "steer.applied"), until=bound
        )
        produced = {
            e.artifact_id for e in events if isinstance(e, ArtifactProduced) and e.role == "deliverable"
        }
        live_then = produced - {a for e in events if isinstance(e, SteerApplied) for a in e.superseded}
    # Deliverables only: a fork inherits the DOCUMENT, never a parent long-form run's
    # 24 section files - the lane's machinery stays with the lane.
    return [a for a in shelf if a.role == "deliverable" or (a.role == "superseded" and a.id in live_then)]


async def own_prior_artifacts(session: AsyncSession, task: Task) -> list[Artifact]:
    """The task's OWN deliverables from earlier turns - what a continuation carries forward,
    exactly as a fork inherits its parent's.

    Lineage-latest, cut at the turn boundary: only artifacts that already existed when the
    current turn opened (the latest `task.continued`'s timestamp). The cut is load-bearing
    twice - it keeps the read STABLE (a mid-turn publish must not change what a retry of the
    same turn mounts) and it keeps same-turn parallel publishes (fleet seats) from ever
    reading each other as "prior". Harvest uses the same helper to decide what a re-published
    filename revises, so the mount and the lineage can never disagree.
    """
    started = await _turn_opened(session, task)
    if started is None:
        return []
    return [
        a
        for a in await Artifact.latest_for_task(session, task.id)
        # Deliverables only - a follow-up continues from the document, not the scaffolding.
        if a.created_at < started and a.role == "deliverable"
    ]


async def _turn_opened(session: AsyncSession, task: Task) -> dt.datetime | None:
    """When the current turn opened (its `task.continued`), the cut every earlier-turn read makes:
    only what existed then, so a retry of the turn reads the same files. None on turn 1."""
    if task.turn <= 1:
        return None
    boundary = await TaskEvent.latest_of_type(session, task.id, "task.continued")
    return getattr(boundary, "at", None)


# A section file as the assemble walkers name it (`md_walk.SECTION_FILE`): `NN-<slug>.md`, and
# its seam pass's `NN-<slug>-final.md`.
_SECTION_FILE = re.compile(r"^(?P<num>\d{2})-.+\.md$")


async def prior_sections(
    session: AsyncSession, task: Task, *, named: Collection[str] | None = None
) -> list[Artifact]:
    """The section markdown the thread's last delivered document was assembled from, cut at the turn
    boundary; `named` keeps the ones a step's contract names."""
    from prismoid.ai.frames.layout import input_path

    if named is not None and not any(_SECTION_FILE.match(PurePosixPath(p).name) for p in named):
        return []
    assembled = await _last_assembly(session, task)
    if assembled is None:
        return []
    last, done = assembled
    fed = {
        e.from_step_id
        for e in await PlanEdgeRow.for_task(session, task.id)
        if e.to_step_id == last and done.get(e.from_step_id) in ("section", "harmonize")
    }
    chosen: dict[str, Artifact] = {}
    for artifact in await Artifact.latest_for_task(session, task.id):
        name = upstream_filename(artifact)
        match = _SECTION_FILE.match(name)
        if artifact.step_id not in fed or match is None or not artifact.bytes:
            continue  # an empty file is no section to the walkers either
        held = chosen.get(match["num"])
        if held is None or name.endswith("-final.md") or not upstream_filename(held).endswith("-final.md"):
            chosen[match["num"]] = artifact
    found = [chosen[number] for number in sorted(chosen)]
    if named is None:
        return found
    return [a for a in found if input_path(upstream_filename(a)) in named]


async def _last_assembly(
    session: AsyncSession, task: Task
) -> tuple[uuid.UUID, dict[uuid.UUID, str | None]] | None:
    """The last delivered document's assembly step - the latest done before this turn opened - and
    every done step's lane role, for reading what fed it. None before any long-form delivery."""
    started = await _turn_opened(session, task)
    if started is None:
        return None
    done = {
        s.id: StepSpec.model_validate(s.spec).lane_role
        for s in await PlanStep.for_task(session, task.id)
        if s.step_state is StepState.DONE and s.created_at < started
    }
    assemblies = [sid for sid, role in done.items() if role == "assembly"]
    # `for_task` reads in ordinal order: the last one is the latest turn's assembly
    return (assemblies[-1], done) if assemblies else None


# A figure as the lane buys it (`operations.figures`): `fig-<id>.png`, the id its marker names.
_FIGURE_FILE = re.compile(r"^fig-[a-z0-9][a-z0-9-]*\.png$")


async def prior_figures(
    session: AsyncSession, task: Task, *, named: Collection[str] | None = None
) -> list[Artifact]:
    """The figures the last delivered document shows: per id, the newest bought before its
    assembly published - what a kept section's `Figure (fig-…)` marker still points at, since a
    revision's outline buys figures only for the sections it writes. A turn
    that bought an id again and never delivered does not count. `named` keeps the ones a step's
    contract names (assembly's), at no query when it names none.
    """
    from prismoid.ai.frames.layout import input_path

    if named is not None and not any(_FIGURE_FILE.match(PurePosixPath(p).name) for p in named):
        return []
    assembled = await _last_assembly(session, task)
    published = [a.created_at for a in await Artifact.for_step(session, assembled[0])] if assembled else []
    if not published:
        return []
    delivered = max(published)
    newest: dict[str, Artifact] = {}
    for artifact in await Artifact.latest_for_task(session, task.id):  # oldest first: newest wins
        name = upstream_filename(artifact)
        if (
            artifact.created_at <= delivered
            and artifact.role == "working"
            and artifact.bytes
            and _FIGURE_FILE.match(name)
        ):
            newest[name] = artifact
    found = [newest[name] for name in sorted(newest)]
    if named is None:
        return found
    return [a for a in found if input_path(upstream_filename(a)) in named]


def narration_of(output: object) -> str:
    summary = getattr(output, "summary", None)
    return str(summary if summary is not None else output)
