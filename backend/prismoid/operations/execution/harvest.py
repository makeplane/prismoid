# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's last stage: what a step delivered. `harvest` reads the frame's manifest - never a
directory listing - and hands the published bytes to `operations.artifacts`; `collect` checks the
contract and lets the narration and the browser's recording ride along. The recording and the
narration are evidence, never authority: their helpers never fail the step.
"""

from __future__ import annotations

import contextlib
import secrets
import shlex
import time
import uuid as uuid_mod
from dataclasses import dataclass
from typing import Protocol, cast

import logfire
import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.driver import FileTooLarge, FrameDriver, StagesCopies, run_python
from prismoid.ai.frames.layout import ARTIFACTS
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.frames.preview import ARGS_PATH, PREVIEW_RUNNER, RUNNER_PATH
from prismoid.ai.frames.preview import preview_args as build_args
from prismoid.ai.tools.call_rows import journal_egress_denials
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import PathRefused, PolicyDenied
from prismoid.operations import artifacts as artifact_ops
from prismoid.operations import live
from prismoid.operations.execution.device import stage_root
from prismoid.operations.execution.mounts import has_dependents
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.outputs import ArtifactType, Outcome, slug_title
from prismoid.schemas.plan import StepSpec


async def record_evidence(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    frame: object,
    deps: object,
    *,
    success: bool,
    own_output: bool,
    fact: live.ActivityFact,
) -> list[Artifact]:
    """Publish the recording and harvest exactly that one entry, under the role
    `publish_recording` decided. NEVER raises an `Exception` (evidence is not authority,
    ADR-019): the harvest rides its own SAVEPOINT, so a flush error unwinds before it is
    suppressed and cannot poison the session for whatever commits next."""
    with contextlib.suppress(Exception):
        published = await publish_recording(frame, deps, success=success, own_output=own_output, fact=fact)
        if published is not None:
            path, role = published
            async with session.begin_nested():
                return await harvest(session, task, step, frame, role=role, only=frozenset({path}))
    return []


async def publish_narration(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    frame: object,
    deps: object,
    *,
    spec: StepSpec,
    narration: str,
    fact: live.ActivityFact,
) -> list[Artifact]:
    """A no-publish step's narration as a `working` markdown file through the one door, for the
    steps that depend on it. Best-effort like the recording: the change in the service already
    happened, and a retry that drove the site again over a failed note would cost far more than
    the note - its dependents fall back to the recording."""

    path = f"{stage_root(frame)}/{slug_title(spec.title)}-narration.md"
    with contextlib.suppress(Exception):
        await frame.write_bytes(path, narration.encode())  # type: ignore[attr-defined]
        await frame.publish(  # type: ignore[attr-defined]
            path,
            ArtifactType.DOCUMENT.value,
            f"{spec.title} - what was done",
            sources=list(getattr(deps, "inputs", ()) or ()),
        )
        await fact("end", "Published the narration for the next step")
        async with session.begin_nested():
            only = frozenset({f"{ARTIFACTS}/{path.rsplit('/', 1)[-1]}"})
            return await harvest(session, task, step, frame, role="working", only=only)
    return []


async def quiet_fact(*_: object, **__: object) -> None:
    """An `ActivityFact` for a path with no live wire (a cancelled park's evidence)."""


async def publish_recording(
    frame: object, deps: object, *, success: bool, own_output: bool, fact: live.ActivityFact
) -> tuple[str, str] | None:
    """The computer's evidence: finalize the sidecar's recording into a
    self-contained folder and publish it through the ONE door - never fails the step, because
    evidence is not authority (the answer is primary). `success`/`own_output` decide
    nothing here directly; they are the same two facts the caller uses to pick the harvest
    role (`deliverable` iff `success and not own_output`, else `working` - a step that published its OWN
    output (a CSV the user asked for) has that as the deliverable, the recording rides beside it as working evidence; a failed
    step has nothing to deliver, so its recording is working evidence too).

    Returns the manifest PATH the recording was published under (`{ARTIFACTS}/{basename(copy)}`
    - both drivers' publish runner writes the manifest entry there, never at the source path)
    and the harvest ROLE, so the caller can harvest exactly that one entry - or `None` when
    nothing was published (a recording with no rows at all is never published). The role is `deliverable` only for a successful step that published nothing of its own
    whose recording holds at least one ACT (`finalize`'s `acting`: open/click/type/... rows,
    failed-capture placeholders included; a screenshot is not an act) - a computer step that did
    nothing has no trail worth delivering, so its recording is `working` evidence.

    **`frame.publish` cannot take `frame.recording_dir` directly** - both drivers' publish
    runner tars/copies TO `{ARTIFACTS}/{basename(path)}`, keyed on the SOURCE path's basename
    alone. A copy that merely moves the folder to `/work` under the SAME basename
    (`recording-{attempt}`) still collides: its target is `{ARTIFACTS}/recording-{attempt}`,
    which IS `frame.recording_dir` - the tar write hits the directory it is reading from and
    dies with `IsADirectoryError`, silently swallowed below as "not published".
    A random nonce in the COPY's own basename is what actually avoids the collision;
    `rm -rf` first is defence in depth against a retried attempt leaving a stale copy behind,
    and the copy's exit code is checked - a partial or failed `cp` must not be handed to
    `publish` as if it succeeded. The copy is removed once `publish` has taken it (or refused
    it): `/work` is the Machine's checkpoint and a fork's seed, and logged-in screenshots there
    would ride into every sleep and every fork, which must never carry a login. On a device the copy
    is staged in the task's own `out/` instead (`recording_stage`): its `/work` is the user's folder."""
    done = await frame.browser({"verb": "finalize"})  # type: ignore[attr-defined]
    if not done.get("ok"):
        await fact("end", f"No recording ({done.get('error', 'finalize failed')})")
        return None
    if not int(done.get("actions", 0) or 0):
        # Not one row (a shell-only step): the page would say "no actions were recorded", and an
        # empty file in the task's list is noise, never evidence.
        await fact("end", "No recording (no browser actions)")
        return None
    acted = int(done.get("acting", 0) or 0) > 0
    role = "deliverable" if success and not own_output and acted else "working"

    name = f"recording-pub-{frame.attempt}-{secrets.token_hex(4)}"  # type: ignore[attr-defined]
    staged, copy, remove = recording_stage(frame, name)
    published_path = f"{ARTIFACTS}/{name}"
    # The copy AND the publish share one try: `frame.exec` is a real
    # transport call, not a "every error is a result" verb like `frame.browser` above - a
    # docker/sandbar connection error there is exactly as possible, and exactly as
    # non-authoritative, as a `frame.publish` refusal. The docstring's "never fails the step"
    # must hold for BOTH calls, or a transport hiccup here would escape as a raised exception
    # instead of a logged fact.
    try:
        copied = await frame.exec(copy)  # type: ignore[attr-defined]
        if not copied.ok:
            await fact("end", f"Recording not published: copy failed (exit {copied.exit_code})")
            return None
        await frame.publish(  # type: ignore[attr-defined]
            staged,
            ArtifactType.RECORDING.value,
            f"Browser recording, attempt {frame.attempt}",  # type: ignore[attr-defined]
            sources=list(getattr(deps, "inputs", ()) or ()),
        )
    except Exception as failed:  # logged, never raised: the answer is primary
        await fact("end", f"Recording not published: {type(failed).__name__}")
        return None
    finally:
        with contextlib.suppress(Exception):  # best effort: a copy left behind costs disk, not the step
            await frame.exec(remove)  # type: ignore[attr-defined]
    await fact("end", f"Recording published ({done.get('actions', 0)} action(s)) · {role}")
    return published_path, role


def recording_stage(frame: object, name: str) -> tuple[str, str, str]:
    """Where `publish_recording` stages its copy, and the two commands that make and remove it:
    (copy path, copy command, remove command). A cloud frame stages in `/work`, as it always has.
    A frame on the user's own computer names a `staging_dir` instead (`/out`):
    its `/work` is the user's real folder, and a crash between the copy and its removal
    must never leave logged-in screenshots in their repo. Its roots are real folders that may hold
    a space, so the commands spell them really and quoted (a cut-off command's head is mapped back
    to virtual roots, `/out` among them, before anything records it); the copy also clears any
    `recording-pub-*` an earlier crash left there - none is this publish's, which has not begun."""
    staging = frame.staging_dir if isinstance(frame, StagesCopies) else None
    if not staging:
        copy = f"/work/{name}"
        return copy, f"rm -rf {copy} && cp -r {frame.recording_dir} {copy}", f"rm -rf {copy}"  # type: ignore[attr-defined]
    root = str(staging).rstrip("/")
    copy = f"{root}/{name}"

    def real(path: str) -> str:
        return shlex.quote(frame.paths.render(path))  # type: ignore[attr-defined]

    clear = f"find {real(root)} -maxdepth 1 -name 'recording-pub-*' -exec rm -rf {{}} +"
    return copy, f"{clear} ; cp -r {real(frame.recording_dir)} {real(copy)}", f"rm -rf {real(copy)}"  # type: ignore[attr-defined]


# What one harvest reads into the shared worker: so many entries, so many bytes together. The
# manifest is a file in the frame, so whoever ran the task chose both; a collection or a directory
# artifact carries many files as one entry.
HARVEST_MAX_ENTRIES = 100
HARVEST_MAX_BYTES = 256 * 1024 * 1024


class _Reads(Protocol):
    async def read_bytes(self, path: str) -> bytes: ...


async def read_published(frame: _Reads, manifest: Manifest) -> dict[str, bytes]:
    """The bytes of every entry the manifest's head names, read under the caps. Each refusal is a
    failed attempt whose message is the fix: too many entries (before any read), too many bytes
    together (at the read that passes the cap), one file past a read's cap, or a file missing.

    **Read bytes, not text.** `read_file` is `cat` - it decodes as UTF-8 and mangles a binary
    workbook, so harvest would hash a corrupted copy and reject the artifact it just published.
    Every artifact, text or binary, comes out through the byte-exact path."""
    latest = manifest.latest()
    if len(latest) > HARVEST_MAX_ENTRIES:
        raise PolicyDenied(
            f"the manifest names {len(latest)} artifacts, more than the {HARVEST_MAX_ENTRIES} one step "
            "publishes - publish fewer (a collection or a directory artifact holds many files)"
        )
    payloads: dict[str, bytes] = {}
    total = 0
    for entry in latest.values():
        try:
            data = await frame.read_bytes(entry.path)
        except FileNotFoundError:
            raise PolicyDenied(
                f"{entry.path}: the manifest names it but the file is missing from /artifacts - "
                "publish it again"
            ) from None
        except PathRefused:
            # The file door refused the entry (a link into the browser profile, a file the agent's
            # uid cannot read). Answered like a missing one so the model is told and can fix it.
            raise PolicyDenied(
                f"{entry.path}: the file door refused it - publish a regular file under /work or "
                "/artifacts again"
            ) from None
        except FileTooLarge as big:
            raise PolicyDenied(f"{entry.path}: {big}; publish a smaller file") from None
        total += len(data)
        if total > HARVEST_MAX_BYTES:
            raise PolicyDenied(
                f"the step's artifacts pass {HARVEST_MAX_BYTES:,} bytes together - publish less"
            )
        payloads[entry.path] = data
    return payloads


async def harvest(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    frame: object,
    *,
    role: str = "deliverable",
    only: frozenset[str] | None = None,
) -> list[Artifact]:
    """Pull the published bytes out of the frame and hand them to `operations.artifacts.harvest`.

    This function is the seam: the **engine** has a sandbox and no database; the
    **operation** has a database and no sandbox. `operations` is where the two meet, and it is the
    only place they do. `role` comes from the step SPEC - the model cannot forge it.

    `only`, when given, scopes the harvest to just these published PATHS (a browser recording,
    published and harvested in a SECOND call after the step's ordinary one) - without it, every
    call re-reads and re-preview-extracts the WHOLE manifest, including entries the first call
    already harvested this same attempt (`Artifact.by_digest`'s idempotency
    means they come back unchanged, but re-fetching bytes and re-rendering their previews for
    nothing is real, avoidable work, and the caller's `produced` list would otherwise carry
    every one of them TWICE).
    """
    from pydantic import ValidationError

    raw = await frame.manifest()  # type: ignore[attr-defined]
    try:
        manifest = Manifest.model_validate(raw)  # JSON stops being JSON here, at the edge
    except ValidationError:
        # A mangled manifest is a failed ATTEMPT, never a dead thread: the ladder
        # retries with the reason, exactly like any other produce failure.
        raise PolicyDenied(
            "the manifest is unreadable - write the file and publish(type=..., path=...) again"
        ) from None
    if not manifest.entries:
        return []
    if only is not None:
        latest = manifest.latest()
        manifest = Manifest(entries=[latest[p] for p in only if p in latest])
        if not manifest.entries:
            return []
    else:
        # `ArtifactType.RECORDING` is the ENGINE's own evidence - published through
        # `frame.publish` only by `record_evidence`'s `only={path}` call above, never by an
        # agent's turn. `frame_tools.publish` already refuses the type at the door, but the
        # door is a courtesy to a well-behaved model, not the boundary: a forged
        # manifest entry (a stale frame, a bypassed toolset) must not be harvestable through
        # the ordinary path either. Skip it here; it is only ever real when named explicitly.
        kept = [e for e in manifest.entries if e.type is not ArtifactType.RECORDING]
        if len(kept) != len(manifest.entries):
            logfire.warn(
                "harvest: ignored {n} agent-published recording entr{sfx} on step {s}",
                n=len(manifest.entries) - len(kept),
                sfx="y" if len(manifest.entries) - len(kept) == 1 else "ies",
                s=str(step.id),
            )
        manifest = Manifest(entries=kept)
        if not manifest.entries:
            return []

    payloads = await read_published(cast(_Reads, frame), manifest)
    previews, renditions = await extract_previews(frame, manifest)
    return await artifact_ops.harvest(
        session, task.id, step.id, manifest, payloads, previews=previews, renditions=renditions, role=role
    )


async def extract_previews(
    frame: object, manifest: object
) -> tuple[dict[str, bytes], dict[str, tuple[bytes, str]]]:
    """Render sidecars AND full-fidelity renditions, built in the STILL-LIVE work frame
    (ADR-023).

    The reader shows the work the moment `artifact.produced` lands: the row is born with its
    preview (the instant extraction) and its rendition (LibreOffice's faithful render -
    docx/pptx as PDF, xlsx as recalculated styled HTML). Same hand-off as publish - a static
    runner, args as a JSON file, a fixed exec line - and strictly best-effort: whatever cannot
    be built is absent, never a failed harvest. The verify-time spreadsheet sidecar later
    refreshes the preview slot.
    """

    previews: dict[str, bytes] = {}
    renditions: dict[str, tuple[bytes, str]] = {}
    if isinstance(frame, PaperFrame):
        return previews, renditions  # it holds text the system wrote: nothing a renderer reads
    entries = [
        {"path": e.path, "type": e.type.value}
        for e in manifest.latest().values()  # type: ignore[attr-defined]
    ]
    nonce = uuid_mod.uuid4().hex
    paths = cast(FrameDriver, frame).paths
    args = build_args(entries, nonce, paths=paths, office=settings.local.has_office)
    try:
        await frame.write_bytes(RUNNER_PATH, PREVIEW_RUNNER.encode(), mode=0o444, owner="system")  # type: ignore[attr-defined]
        await frame.write_bytes(ARGS_PATH, orjson.dumps(args), mode=0o444, owner="system")  # type: ignore[attr-defined]
        # 300s comfortably clears the inner soffice budget (SOFFICE_TIMEOUT=240s):
        # at 150s a large rendition was killed by the OUTER exec before the renderer's own
        # deadline could hand back the partial. The outer bound must exceed the inner one.
        result = await run_python(cast(FrameDriver, frame), RUNNER_PATH, ARGS_PATH, timeout=300.0)
        if result.exit_code != 0:
            # Not a return: the runner writes OUT after every entry, so a deadline-killed batch
            # still hands over whatever it finished - take the partial, honestly.
            logfire.warn(
                "preview runner exited {code}: {err}", code=result.exit_code, err=result.stderr[:300]
            )
    except Exception as never_fatal:  # presentation, never attestation
        logfire.warn("preview runner exec failed: {err}", err=str(never_fatal)[:300])
    try:
        raw = await frame.read_bytes(str(args["out"]))  # type: ignore[attr-defined]
        parsed: dict[str, object] = orjson.loads(raw)
        if parsed.get("nonce") != nonce:
            # A previous harvest's leftover in a long-lived frame - a stale sidecar attached to
            # re-published bytes would be a preview of the WRONG version. Absence is honest.
            logfire.warn("preview OUT is stale (nonce mismatch) - discarded")
            return previews, renditions
        by_path = parsed.get("made")
        if not isinstance(by_path, dict):
            return previews, renditions
        for spelled, made in by_path.items():
            if not isinstance(made, dict):
                continue
            path = paths.virtual(spelled) or spelled  # a device's runner reads the real one
            preview = made.get("preview")
            if preview is not None:
                previews[path] = orjson.dumps(preview)
            rendition = await _rendition(frame, made.get("rendition"), path)
            if rendition is not None:
                renditions[path] = rendition
    except Exception as never_fatal:  # same law: no OUT, no sidecars, never a failed harvest
        logfire.warn("preview extraction skipped: {err}", err=str(never_fatal)[:300])
    return previews, renditions


async def _rendition(frame: object, rendition: object, path: str) -> tuple[bytes, str] | None:
    """One entry's full-fidelity rendition, read out of the frame: None when the runner made
    none, or the frame lost it (an absent rendition, never a failed harvest)."""
    if not (isinstance(rendition, dict) and rendition.get("file")):
        return None
    try:
        data = await frame.read_bytes(str(rendition["file"]))  # type: ignore[attr-defined]
    except Exception as gone:  # a rendition the frame lost is an absent rendition
        logfire.warn("rendition read failed for {p}: {err}", p=path, err=str(gone)[:200])
        return None
    return data, str(rendition.get("media") or "application/pdf")


@dataclass(frozen=True)
class Yield:
    """What the run ended with, for the harvest to collect."""

    narration: str
    outline: object
    publishes: bool  # the agent's definition publishes its own output
    delivers_outcome: bool  # the agent's definition delivers a typed Outcome
    profile: str


async def collect(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    frame: FrameDriver,
    deps: TaskDeps,
    run: Yield,
    fact: live.ActivityFact,
) -> list[Artifact]:
    """The attempt's last stage: what the step delivered, read off the manifest, checked against
    its contract, with the narration and the recording riding along where they are owed."""
    # Any egress the proxy refused happened in another process, on another network. Put it on
    # the record, or the flight recorder shows a curl that failed for no stated reason.
    await journal_egress_denials(deps.journal_tool, await frame.denials())

    # HARVEST. Read the manifest - **not a directory listing**. A file that arrived in
    # /artifacts without going through publish has no entry, no declared type, no sources and
    # no provenance, so it is not a deliverable. That makes publish the only door, mechanically.
    harvest_started = time.monotonic()
    await fact("start", "Collecting the deliverable")
    produced = await harvest(session, task, step, frame, role=spec.artifact_role)
    if produced and spec.artifact_role == "deliverable":
        # The final attempt is the step's output: an earlier attempt's draft it did not republish
        # or revise leaves the shelf, so a retry never shows two competing results.
        await artifact_ops.retire_drafts(session, step.id, produced)
    await fact(
        "end",
        f"Collected {len(produced)} deliverable{'' if len(produced) == 1 else 's'}"
        if produced
        else "Collected nothing",
        started=harvest_started,
    )

    # **The contract declared an output. Did it deliver one?**
    #
    # The agent wrote a perfectly good memo and simply never called `publish`. The task went
    # `delivered` with nothing to download - the worst possible outcome, because it is a
    # *silent* one: the user is told it worked. A contract that nothing checks is not a
    # contract, it is a hope with a schema.
    output = spec.contract.output if spec.contract else None
    if output is not None and not produced:
        raise PolicyDenied(
            f"the contract required a {output.type.value} and nothing was "
            "published - write the file to /work and call publish(type=...)"
        )

    own_output = bool(produced)  # the step's own publishes, before narration and recording ride along
    # A step whose agent publishes nothing (the operator) or delivers its outcome (the computer)
    # still owes the steps that depend on it what it found, and only published artifacts flow
    # downstream (`mounts.upstream_artifacts`): its narration rides to them as a working file.
    if (
        run.narration
        and (not run.publishes or run.delivers_outcome)
        and await has_dependents(session, task, step)
    ):
        produced += await publish_narration(
            session, task, step, frame, deps, spec=spec, narration=run.narration, fact=fact
        )

    # The recording: evidence, never the deliverable the contract asked about -
    # published and harvested AFTER the check above, or a browser step whose contract wanted a
    # document and produced only a trail would pass it. Best-effort end to end (a recording
    # failure must never fail a step that otherwise succeeded): `publish_recording` never raises
    # on its own, and the harvest that follows it is wrapped too - a flush error there is real,
    # and without the SAVEPOINT it would poison the session for the commit that follows.
    if run.profile == "browser":
        outline = run.outline
        recorded = await record_evidence(
            session,
            task,
            step,
            frame,
            deps,
            # A blocked Outcome fails the step once this returns (the ladder's typed ending): its
            # trail is evidence of the wall, never the deliverable.
            success=not isinstance(outline, Outcome) or outline.status in ("done", "partial"),
            own_output=own_output,
            fact=fact,
        )
        seen = {a.id for a in produced}
        produced += [a for a in recorded if a.id not in seen]
    return produced
