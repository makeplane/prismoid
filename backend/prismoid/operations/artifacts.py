# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Harvest - the manifest becomes Artifacts.

The pipeline, and there is no other:

    frame.publish()  →  /artifacts/<file> + a manifest entry     (inside the sandbox)
    harvest()        →  MinIO (content-addressed) + Artifact rows + `artifact.produced`

**Harvest reads the manifest and nothing else.** Not a directory listing, not "every file that
looks like a deliverable" - the manifest. A file that arrived in `/artifacts` without going through
`publish` has no entry, no declared type, no sources, and no provenance, and so it is **not
harvested**. That is what makes "artifacts exist only via publish" a mechanism rather than a hope.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import logfire
import orjson
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.project import Project, Role
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, Forbidden, NotFound, PolicyDenied, PrismoidError, ValidationFailed
from prismoid.schemas.artifacts import ArtifactView, Manifest, ManifestEntry
from prismoid.schemas.collection import Collection
from prismoid.schemas.collection_repair import CollectionRepair, repair_collection
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.events import ArtifactProduced, ArtifactRevised
from prismoid.schemas.ids import ArtifactId, ProjectId, StepId, TaskId
from prismoid.schemas.outputs import ARTIFACT_TYPES, ArtifactType, render_for
from prismoid.schemas.verification import Verdict


async def harvest(
    session: AsyncSession,
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    manifest: Manifest,
    payloads: dict[str, bytes],
    previews: dict[str, bytes] | None = None,
    renditions: dict[str, tuple[bytes, str]] | None = None,
    role: str = "deliverable",
) -> list[Artifact]:
    """Read the manifest, store the bytes, insert the rows, emit the events.

    Takes a **`Manifest`**, not a `dict` - Rule 1: no bare dicts across boundaries. The first
    version took `dict[str, object]` and pyright refused the call site, which was the type system
    pointing at the rule: a dict of unknown shape is a promise the caller cannot keep and the
    callee cannot check. Parsing happens once, at the edge, where the JSON stops being JSON.

    `previews` are the render sidecars the caller built in the still-live frame,
    keyed by published path: stored as their own CAS blobs so the row - and therefore
    `artifact.produced` - is born with `has_preview` true, instead of waiting on the verify
    job. Optional and best-effort by contract.

    `role` comes from the STEP SPEC - never the manifest, never the model. A working
    row (outline, section, figure) crosses steps through this same door but stays off the shelf
    and out of the verify queue.
    """

    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")

    agent, model, pins = await _who_made_it(session, step_id)
    priors = await _thread_priors(session, task)
    earlier = await _attempt_priors(session, step_id) if role == "deliverable" else {}
    priors |= earlier
    published = manifest.latest()
    sole = _sole_draft(earlier, published)
    produced: list[Artifact] = []

    for raw in published.values():
        # **The frame cannot know which model served the step**, and that is not an oversight -
        # `resolve()` builds a *fallback chain*, so the model that actually answered is only known
        # after it answers. Nor can it know the skill pins: the plan holds those. The sandbox
        # writes what it can (path, type, hash, sources); harvest completes the chain from the
        # ledger and the plan, which recorded the truth as it happened.
        entry = raw.model_copy(update={"agent": agent, "model": model, "skill_pins": pins})
        # The bytes were read out of the frame by the caller: the engine has no database, and this
        # operation has no sandbox. `operations` is where the two meet, and the only place they do.
        data = payloads.get(entry.path)
        if data is None:
            continue

        _check_hash(entry, data)
        data, repairs = normalize_for_harvest(entry, data)
        digest = await storage.aput(data)
        if repairs:
            entry = entry.model_copy(update={"sha256": digest, "bytes": len(data), "repairs": repairs})

        preview_raw = (previews or {}).get(entry.path)
        preview_sha = await storage.aput(preview_raw) if preview_raw else None
        rendition = (renditions or {}).get(entry.path)
        rendition_sha = await storage.aput(rendition[0]) if rendition else None
        sidecars = _Sidecars(preview_sha, rendition_sha, rendition[1] if rendition else None)

        # Idempotent per STEP: a replayed harvest, or this step publishing the same bytes again,
        # gets its own row back. Another step's identical bytes are that step's own publish - its
        # provenance, and the row the plan's edges find its output by (`upstream_artifacts` keys
        # on the step): a writer that keeps a section verbatim still hands its seam pass a file.
        # And a LIVE row only (R52): output a steer superseded is never taken back. A re-run that
        # republishes the very bytes it overtook is a new artifact - a fresh row on the same
        # content-addressed blob, announced once like any other - and the old row stays
        # superseded, its id never re-sent.
        existing = await Artifact.by_digest(session, step_id, digest)
        if existing is not None:
            _top_up_sidecars(existing, sidecars)
            produced.append(existing)  # a re-publish of identical bytes: idempotent, by the hash
            continue

        prior = priors.get(entry.path.rsplit("/", 1)[-1]) or sole
        row = _new_row(
            task, step_id, entry, prior, digest=digest, size=len(data), sidecars=sidecars, role=role
        )
        session.add(row)
        await session.flush()
        produced.append(row)
        await _announce(session, task, step_id, row, entry, prior=prior)

    return produced


@dataclass(frozen=True, slots=True)
class _Sidecars:
    """The stored render sidecars of one published file: its preview and its rendition."""

    preview_sha: str | None
    rendition_sha: str | None
    rendition_media: str | None


async def _thread_priors(session: AsyncSession, task: Task) -> dict[str, Artifact]:
    """The latest artifact the THREAD delivered under each published basename.

    A re-publish of a filename the thread already delivered is the agent's revision of that
    deliverable: same basename → same lineage, next revision - whether the prior came from an
    earlier TURN of this task or from a fork's PARENT. Anything else roots a fresh lineage. Matched
    on the published basename because that is the name the user saw and the name the frame
    mounted. Own prior turns win over the parent's when both carry the name; `own_prior_artifacts`
    cuts at the turn boundary, so two fleet seats publishing the same name in ONE turn stay
    independent lineages, never each other's "revision"."""
    from prismoid.operations.execution.mounts import own_prior_artifacts

    priors: dict[str, Artifact] = {}
    if task.parent_task_id is not None:
        for prior in await Artifact.latest_for_task(session, task.parent_task_id):
            priors[filename(prior)] = prior
    for prior in await own_prior_artifacts(session, task):
        priors[filename(prior)] = prior
    return priors


async def _attempt_priors(session: AsyncSession, step_id: uuid.UUID) -> dict[str, Artifact]:
    """This step's live deliverables from its earlier attempts, by published basename: a retry or a
    review's fix that republishes the name is that draft's next revision, never a second lineage."""
    return {
        filename(row): row for row in await Artifact.for_step(session, step_id) if row.role == "deliverable"
    }


def _sole_draft(earlier: Mapping[str, Artifact], published: Mapping[str, ManifestEntry]) -> Artifact | None:
    """The earlier attempt's one draft a renamed retry revises: this step's earlier attempts left
    exactly one live deliverable, this attempt publishes exactly one file of the same type under a
    new name. The step is the deliverable's identity, not the name the model chose, so the retry is
    that draft's next revision - never a second deck beside the abandoned one."""
    drafts: dict[uuid.UUID, Artifact] = {}
    for row in earlier.values():  # the newest revision speaks for its lineage
        if row.lineage_id not in drafts or row.revision > drafts[row.lineage_id].revision:
            drafts[row.lineage_id] = row
    if len(drafts) != 1 or len(published) != 1:
        return None
    [draft] = drafts.values()
    [entry] = published.values()
    if entry.path.rsplit("/", 1)[-1] in earlier or entry.type.value != draft.type:
        return None
    return draft


async def retire_drafts(session: AsyncSession, step_id: uuid.UUID, kept: list[Artifact]) -> list[uuid.UUID]:
    """After a step's attempt harvested its deliverables, its earlier attempts' live deliverables that
    this attempt neither republished nor revised are superseded: the final attempt is the step's whole
    output. Only lineages born in this step: a revision of an earlier turn's or the parent's file stays,
    or that file would leave the shelf. The rows stay on the record (R52's role: never shown, mounted or
    verified)."""
    kept_ids = {a.id for a in kept}
    kept_lineages = {a.lineage_id for a in kept}
    rows = [row for row in await Artifact.for_step(session, step_id) if row.role == "deliverable"]
    born = {row.lineage_id for row in rows if row.lineage_id == row.id}
    stale = [
        row.id
        for row in rows
        if row.lineage_id in born and row.id not in kept_ids and row.lineage_id not in kept_lineages
    ]
    await Artifact.supersede(session, stale)
    return stale


def _check_hash(entry: ManifestEntry, data: bytes) -> None:
    """The frame hashed it; we hashed it again. If they disagree, the bytes changed between
    publish and harvest - a bug or an attack, and neither is a thing to shrug at. PolicyDenied, not
    a hard error: the ladder retries the STEP with the reason (a corrupt publish is a failed
    attempt, never a dead thread)."""
    if storage.sha256(data) != entry.sha256:
        raise PolicyDenied(
            f"{entry.path}: hash mismatch (frame said {entry.sha256[:12]}, bytes are "
            f"{storage.sha256(data)[:12]}) - republish the file"
        )


def normalize_for_harvest(entry: ManifestEntry, data: bytes) -> tuple[bytes, list[str]]:
    """The bytes to store and the repairs to record on the provenance.

    **A collection never fails the thread**: the normalizer makes a near-miss valid - stray select
    values become options, timestamps become dates, a board that lost its group falls back
    honestly - with every change recorded on the provenance. Unrepairable bytes ship as published;
    the reader's "can't be read" card is the floor."""
    if entry.type is not ArtifactType.COLLECTION:
        return data, []
    fixed = repair_collection(data)
    if fixed.ok and fixed.repairs:
        logfire.info(
            "collection {path} normalized at harvest: {n} repair(s)",
            path=entry.path,
            n=len(fixed.repairs),
        )
        return _repaired_bytes(fixed), fixed.repairs
    if not fixed.ok:
        return data, fixed.repairs  # the honest note; bytes stored exactly as published
    return data, []


def _top_up_sidecars(existing: Artifact, sidecars: _Sidecars) -> None:
    """A re-publish may bring the sidecars the first publish lacked; one already stored stays."""
    if existing.preview_sha256 is None and sidecars.preview_sha is not None:
        existing.preview_sha256 = sidecars.preview_sha
    if existing.rendition_sha256 is None and sidecars.rendition_sha is not None:
        existing.rendition_sha256 = sidecars.rendition_sha
        existing.rendition_media = sidecars.rendition_media


def _new_row(
    task: Task,
    step_id: uuid.UUID,
    entry: ManifestEntry,
    prior: Artifact | None,
    *,
    digest: str,
    size: int,
    sidecars: _Sidecars,
    role: str,
) -> Artifact:
    """The Artifact row for a fresh publish: the next revision of `prior`'s lineage, or a new one."""
    row_id = uuid.uuid4()
    return Artifact(
        id=row_id,
        task_id=task.id,
        step_id=step_id,
        lineage_id=prior.lineage_id if prior else row_id,
        revision=prior.revision + 1 if prior else 1,
        supersedes_id=prior.id if prior else None,
        type=entry.type.value,
        title=entry.title,
        sha256=digest,
        bytes=size,
        storage_key=storage.key_for(digest),
        preview_sha256=sidecars.preview_sha,
        rendition_sha256=sidecars.rendition_sha,
        rendition_media=sidecars.rendition_media,
        role=role,
        provenance=entry.model_dump(mode="json"),
    )


async def _announce(
    session: AsyncSession,
    task: Task,
    step_id: uuid.UUID,
    row: Artifact,
    entry: ManifestEntry,
    *,
    prior: Artifact | None,
) -> None:
    """Journal a fresh row: `artifact.produced`, or `artifact.revised` when it continues a lineage."""
    seq = await task.next_seq(session)
    project_id = ProjectId(task.project_id) if task.project_id else None
    filename = entry.path.rsplit("/", 1)[-1]
    if prior is not None:
        # The documented signal for "which id is now the lineage's face" - the web repoints
        # its shelf entry and feed card on it, exactly as it does for a human edit's revise.
        await TaskEvent.append(
            session,
            ArtifactRevised(
                seq=seq,
                task_id=TaskId(task.id),
                step_id=StepId(step_id),
                project_id=project_id,
                at=utcnow(),
                artifact_id=ArtifactId(row.id),
                supersedes_id=ArtifactId(prior.id),
                lineage_id=ArtifactId(row.lineage_id),
                revision=row.revision,
                artifact_type=entry.type,
                title=entry.title,
                filename=filename,
                sha256=row.sha256,
                bytes=entry.bytes,
                role=row.role,
            ),
        )
        return
    await TaskEvent.append(
        session,
        ArtifactProduced(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step_id),
            project_id=project_id,
            at=utcnow(),
            artifact_id=ArtifactId(row.id),
            artifact_type=entry.type,
            title=entry.title,
            filename=filename,
            sha256=row.sha256,
            bytes=entry.bytes,
            role=row.role,
        ),
    )


# The text-revise ceiling: generous for prose, far below anything the reader
# would render anyway; the collection path has its own structural caps (collection_repair).
MAX_TEXT_REVISION_BYTES = 5 * 1024 * 1024

# Which text files a human may edit in-app: exactly the document kind's registry
# text formats (one place per kind - the web's render-hint-gated Edit affordance derives
# from the same tuple, so the two can never drift). Code is a directory (tar), datasets are
# machine-shaped - restore covers them; .html is deliberately absent from text_extensions
# (viewable-not-editable, recorded in the ADR).
_TEXT_EDIT_SUFFIXES = ARTIFACT_TYPES[ArtifactType.DOCUMENT].text_extensions


def _repaired_bytes(fixed: CollectionRepair) -> bytes:
    """A repaired collection as the bytes to store. A repair that changed something always carries
    its content; one that does not is a bug in the repair pass, never the publisher's."""
    if fixed.content is None:
        raise PrismoidError("a collection repair reported changes but returned no content")
    return orjson.dumps(fixed.content, option=orjson.OPT_INDENT_2)


async def _require_edit_rights(session: AsyncSession, actor: Actor, task: Task) -> None:
    """The mutation-door rights check (revise / restore / label): the task's creator, or an
    editor+ of its project - the delete/rename norm."""
    if task.creator_actor_id != actor.id:
        if task.project_id is None:
            raise Forbidden("only the task's creator can modify a spaceless task's artifacts")
        await Project.get_for(session, task.project_id, actor, min_role=Role.EDITOR)


async def _editable_row(session: AsyncSession, actor: Actor, artifact_id: uuid.UUID) -> tuple[Artifact, Task]:
    """Fetch + authorize the mutation doors share (revise / restore / label): scope via the
    task, then the rights check above."""
    row = await Artifact.get_for(session, artifact_id, actor)
    task = await session.get(Task, row.task_id)
    if task is None:  # pragma: no cover - the FK makes this unreachable
        raise NotFound("no such task")
    await _require_edit_rights(session, actor, task)
    return row, task


async def revise(
    session: AsyncSession,
    actor: Actor,
    artifact_id: uuid.UUID,
    *,
    content: Mapping[str, object] | str,
    base_sha256: str,
) -> Artifact:
    """A human edit becomes the lineage's next immutable revision (text kinds
    are joined) - the second door.

    Nothing is mutated: the base row keeps its bytes and its signed verification; this mints a
    new content-addressed row one revision up, provenance `agent="user"`, and the durable
    `artifact.revised` event tells every surface which id is now the lineage's face. The base
    sha is optimistic concurrency: editing v2 while someone saved v3 is a 409 that NAMES the
    newer sha, so the client can rebase its staged edits instead of silently clobbering.

    The content's SHAPE pairs with the kind: a collection takes the full object (validated by
    the same pydantic model the verifier mirrors); a text document (.md/.txt) takes the full
    text - a patch format is a merge engine nobody asked for. Sidecars never carry: the
    content changed, so the old preview/rendition would lie.
    """
    row, task = await _editable_row(session, actor, artifact_id)

    is_collection = row.type == ArtifactType.COLLECTION.value
    is_text_doc = row.type == ArtifactType.DOCUMENT.value and (filename(row) or "").lower().endswith(
        _TEXT_EDIT_SUFFIXES
    )
    if not (is_collection or is_text_doc):
        raise Conflict(
            f"this artifact is not editable in-app ({row.type}, {filename(row) or 'directory'}) - "
            "collections and text documents (.md/.txt) are; every kind can be restored"
        )

    # The stale-head 409 comes BEFORE content validation: a stale save must always learn the
    # newer sha - the client's rebase flow keys on the 409 - even when its draft would also
    # trip a content rule (a 422 first would strand the client on the stale base forever).
    newest = (await Artifact.revisions_of(session, row.lineage_id, actor=None))[0]
    if newest.id != row.id or row.sha256 != base_sha256:
        if (await Artifact.revisions_of(session, row.lineage_id, actor=actor))[0].id != newest.id:
            # the head is on a task this person cannot see: refuse without naming it
            raise Conflict("a newer revision exists where you cannot see it - run a new turn instead")
        raise Conflict(
            f"a newer revision exists - v{newest.revision}, sha {newest.sha256}. "
            "Rebase your edits on it and save again."
        )

    title = row.title
    if is_collection:
        if isinstance(content, str):
            raise ValidationFailed("collection content is the full collection object - send JSON, not text")
        try:
            model = Collection.model_validate(content)
        except ValidationError as bad:
            first = bad.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "content"
            raise ValidationFailed(f"not a valid collection - {where}: {first.get('msg')}") from None
        data = orjson.dumps(model.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        title = model.title
    else:
        if not isinstance(content, str):
            raise ValidationFailed("document content is the full text - send a string")
        if not content.strip():
            raise ValidationFailed("an empty revision would erase the document - send the full text")
        data = content.encode("utf-8")
        if len(data) > MAX_TEXT_REVISION_BYTES:
            raise ValidationFailed(
                f"the revision is {len(data)} bytes - the in-app editing cap is "
                f"{MAX_TEXT_REVISION_BYTES} (download, edit locally, run a new turn instead)"
            )

    digest = storage.sha256(data)
    if digest == newest.sha256:
        # Byte-identical with the head: nothing changed, nothing to mint - restore's no-op
        # law (harvest's by-digest posture). The web disables Save on a clean draft; this
        # holds the same line for the API and CLI, where a replayed save must not grow the
        # lineage.
        return newest
    await storage.aput(data)  # MinIO off the event loop (harvest's law)
    base_entry = ManifestEntry.model_validate(row.provenance)
    entry = base_entry.model_copy(
        update={
            "title": title,
            "sha256": digest,
            "bytes": len(data),
            # The plan shaped the original; a human shaped this one. Skill pins do not carry.
            "agent": "user",
            "model": "",
            "skill_pins": [],
            "at": utcnow(),
        }
    )

    revised = Artifact(
        id=uuid.uuid4(),
        task_id=row.task_id,
        step_id=None,  # no step produced this - a person did (the provenance says so)
        lineage_id=row.lineage_id,
        revision=row.revision + 1,
        supersedes_id=row.id,
        type=row.type,
        title=title[:400],
        sha256=digest,
        bytes=len(data),
        storage_key=storage.key_for(digest),
        role=row.role,  # a revision never changes WHAT the row is - a working outline stays working
        provenance=entry.model_dump(mode="json"),
    )
    session.add(revised)
    try:
        await session.flush()

        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            ArtifactRevised(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                artifact_id=ArtifactId(revised.id),
                supersedes_id=ArtifactId(row.id),
                lineage_id=ArtifactId(row.lineage_id),
                revision=revised.revision,
                artifact_type=ArtifactType(row.type),
                title=revised.title,
                filename=filename(revised),
                sha256=digest,
                bytes=len(data),
                role=revised.role,
            ),
        )
        await session.commit()
    except IntegrityError:
        # Two same-base saves raced past the head check (the window spans the upload await);
        # the (lineage_id, revision) unique index names the loser - surface the designed
        # 409, never a raw 500. "a newer revision exists" keeps every rebase flow alive.
        await session.rollback()
        raise Conflict(
            "a newer revision exists - another save landed while yours uploaded. "
            "Rebase your edits on it and save again."
        ) from None
    return revised


async def revisions(session: AsyncSession, actor: Actor, artifact_id: uuid.UUID) -> list[Artifact]:
    """The lineage, newest first - scoped through any one of its rows, and only the rows on
    tasks the actor can see (a lineage can span a fork that moved out of their sight)."""
    row = await Artifact.get_for(session, artifact_id, actor)
    return await Artifact.revisions_of(session, row.lineage_id, actor=actor)


async def restore(
    session: AsyncSession,
    actor: Actor,
    artifact_id: uuid.UUID,
    *,
    base_sha256: str,
) -> Artifact:
    """Make an old revision the lineage's face again - by MINTING, never rewinding:
    a new head carrying the target's bytes, so nothing is overwritten and the history tells
    the whole story. Works for every kind including binaries - no content validation, because
    the bytes already lived on this lineage.

    The bytes are content-addressed and already stored, so nothing uploads; and because the
    bytes are IDENTICAL, the target's preview/rendition sidecars carry forward - the
    deliberate inverse of `revise`, whose changed content would make them lie. `base_sha256`
    is the CURRENT HEAD's sha (the optimistic-concurrency token, revise's 409 shape);
    restoring what is already the head is an idempotent no-op returning the head - a
    double-click mints nothing (harvest's by-digest posture).
    """
    target, task = await _editable_row(session, actor, artifact_id)

    newest = (await Artifact.revisions_of(session, target.lineage_id, actor=None))[0]
    if newest.task_id != target.task_id:
        # A lineage can span tasks: a fork's republish chains onto the parent's lineage with
        # the fork's own task_id (harvest). The restore lands where the lineage's FACE lives
        # - the HEAD's task - so restoring a parent-made revision from a fork keeps the
        # fork's shelf and record intact instead of planting rows and events on the settled
        # parent (whose shelf never asked). Rights re-prove against that task.
        task = await session.get(Task, newest.task_id)
        if task is None:  # pragma: no cover - the FK makes this unreachable
            raise NotFound("no such task")
        await _require_edit_rights(session, actor, task)
    if newest.sha256 != base_sha256:
        raise Conflict(
            f"the lineage moved - the head is v{newest.revision}, sha {newest.sha256}. "
            "Reload the history and try again."
        )
    if newest.sha256 == target.sha256:
        return newest  # the head already carries these bytes - nothing to mint

    entry = ManifestEntry.model_validate(target.provenance).model_copy(
        update={
            # A person chose this; the original maker stays on the RESTORED row's provenance.
            "agent": "user",
            "model": "",
            "skill_pins": [],
            "at": utcnow(),
        }
    )
    restored = Artifact(
        id=uuid.uuid4(),
        task_id=newest.task_id,  # the lineage's face stays where it lives (fork note above)
        step_id=None,
        lineage_id=target.lineage_id,
        revision=newest.revision + 1,
        supersedes_id=newest.id,  # it replaces the HEAD, not the row it copies
        type=target.type,
        title=target.title,
        sha256=target.sha256,
        bytes=target.bytes,
        storage_key=target.storage_key,
        preview_sha256=target.preview_sha256,
        rendition_sha256=target.rendition_sha256,
        rendition_media=target.rendition_media,
        role=target.role,
        provenance=entry.model_dump(mode="json"),
    )
    session.add(restored)
    try:
        await session.flush()

        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            ArtifactRevised(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                artifact_id=ArtifactId(restored.id),
                supersedes_id=ArtifactId(newest.id),
                lineage_id=ArtifactId(target.lineage_id),
                revision=restored.revision,
                artifact_type=ArtifactType(target.type),
                title=restored.title,
                filename=filename(restored),
                sha256=restored.sha256,
                bytes=restored.bytes,
                role=restored.role,
            ),
        )
        await session.commit()
    except IntegrityError:
        # Two restores (or a restore racing a save) both passed the head check; the
        # (lineage_id, revision) unique index names the loser - the docstring's promised
        # 409, never a raw 500.
        await session.rollback()
        raise Conflict("the lineage moved while restoring - reload the history and try again.") from None
    return restored


async def set_label(
    session: AsyncSession,
    actor: Actor,
    artifact_id: uuid.UUID,
    *,
    label: str | None,
) -> Artifact:
    """Name a revision - "final draft", "sent to client" - Google-Docs-style named
    versions. Presentation metadata on the row, like the sidecar pointers: the content stays
    immutable, no event fires (surfaces fetch the lineage fresh), null clears."""
    row, _ = await _editable_row(session, actor, artifact_id)
    row.label = (label.strip()[:80] or None) if label is not None else None
    await session.commit()
    return row


def rendition_url(artifact: Artifact) -> str | None:
    """A presigned link to the FULL-FIDELITY rendition - LibreOffice's faithful
    render, stored as its own CAS blob. None when no rendition exists; the caller 404s."""
    stem = str(artifact.provenance.get("path", artifact.title)).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if artifact.rendition_sha256 is not None:
        ext = "pdf" if (artifact.rendition_media or "") == "application/pdf" else "html"
        media = artifact.rendition_media or "application/pdf"
        return storage.presign(artifact.rendition_sha256, f"{stem}.{ext}", inline=True, media=media)
    if filename(artifact).lower().endswith(".pdf"):
        # A .pdf artifact IS its own rendition - same inline lane, its own bytes.
        return storage.presign(artifact.sha256, f"{stem}.pdf", inline=True, media="application/pdf")
    return None


async def preview(session: AsyncSession, actor: Actor, artifact_id: uuid.UUID) -> dict[str, object]:
    """The render sidecar's JSON - 404 when absent, which is most artifacts."""

    row = await Artifact.get_for(session, artifact_id, actor)
    if row.preview_sha256 is None:
        raise NotFound("this artifact has no preview")
    data = await storage.aget(row.preview_sha256)
    parsed = orjson.loads(data)
    if not isinstance(parsed, dict):  # pragma: no cover - the suite writes an object by contract
        raise NotFound("this artifact has no preview")
    return parsed


def view(artifact: Artifact, *, verdict: Verdict | None = None) -> ArtifactView:
    """A row → a value. Never an ORM object across the boundary.

    `verdict` is passed in rather than looked up, because this function has no session and should
    not grow one: the *caller* knows whether it wants the verification, and a view that quietly
    issues a second query is how a list endpoint becomes N+1 without anyone deciding to.
    """
    name = filename(artifact)
    return ArtifactView(
        id=ArtifactId(artifact.id),
        task_id=TaskId(artifact.task_id),
        type=ArtifactType(artifact.type),
        title=artifact.title,
        filename=name,
        render=render_for(ArtifactType(artifact.type), name),
        lineage_id=ArtifactId(artifact.lineage_id),
        revision=artifact.revision,
        label=artifact.label,
        has_preview=artifact.preview_sha256 is not None,
        has_rendition=artifact.rendition_sha256 is not None,
        rendition_media=artifact.rendition_media,
        sha256=artifact.sha256,
        bytes=artifact.bytes,
        role=artifact.role,
        provenance=ManifestEntry.model_validate(artifact.provenance),
        verdict=verdict,  # None ⇒ nobody has checked. `unverified` ⇒ we checked and proved nothing.
        created_at=artifact.created_at,
    )


def filename(artifact: Artifact) -> str:
    """The published file's basename - from provenance, the one place the path is recorded."""
    return str(artifact.provenance.get("path", artifact.title)).rsplit("/", 1)[-1]


def download_url(artifact: Artifact) -> str:
    """A presigned link, signed for the **public** endpoint."""
    return storage.presign(artifact.sha256, filename(artifact))


async def _who_made_it(session: AsyncSession, step_id: uuid.UUID) -> tuple[str, str, list[SkillPin]]:
    """The agent and skill pins from the plan; the model **from the ledger**.

    Rule 8 says every provider call emits a `UsageEvent` - which means the ledger is the one place
    that knows, for certain, which model produced this work. Asking the model to tell us would be
    asking it to report on itself; asking the plan would tell us who we *hoped* would serve it.
    The pins ARE the plan's to tell: they were resolved to exact versions at submit, and stamping
    them here is what makes `ManifestEntry.skill_pins` provenance instead of an always-empty field
    (both frame drivers honestly write `[]` - the frame does not know what shaped the work).
    """
    from prismoid.db.models.plan import PlanStep
    from prismoid.db.models.usage import UsageEvent

    step = await session.get(PlanStep, step_id)
    agent = str(step.spec.get("agent", "")) if step else ""
    pins = [SkillPin.model_validate(p) for p in (step.spec.get("skill_pins") or [])] if step else []

    rows = await UsageEvent.for_step(session, step_id)
    model = f"{rows[-1].provider}:{rows[-1].model}" if rows else ""
    return agent, model, pins
