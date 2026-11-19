# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The frame's layout - four directories, and exactly one of them is harvestable.

    /task       the agent's own map: plan.md, criteria.json, answers.json
    /task/inputs  what the SYSTEM gave it: attachments, a parent's artifacts. Read-only.
    /work       the workshop: clones, notebooks, intermediates. Kept across the task's steps in
                its Machine, and checkpointed when the Machine sleeps.
    /artifacts  the publish target - the ONLY harvestable directory
    /skills     the loader target

The asymmetry is the whole design. An agent writes wherever it likes in `/work`; nothing there
becomes a deliverable. A file becomes an Artifact only by passing through `frame.publish()`, which
validates its type, hashes it, and writes it into `/artifacts` with a manifest entry. There is no
other door - which is what makes "nothing unverified ships" enforceable rather than aspirational.

`/task/inputs` is the mirror of that door on the way *in*: **everything an agent did not invent
came through here**, and the verifier knows it. A claim is groundable exactly when it traces back
to one of these files. One directory in, one directory out, and the provenance chain
runs between them.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

TASK: Final = "/task"  # the map
WORK: Final = "/work"  # the workshop
ARTIFACTS: Final = "/artifacts"  # the loading dock - the only one harvest reads
SKILLS: Final = "/skills"  # the library
VERIFY: Final = "/verify"  # the invigilator's desk - the suite, and never the agent's
BROWSER: Final = "/browser"  # the browser user's profile: cookies live here, never in the plain snapshot
# A device frame's own `out/`: Prismoid's folder beside the user's work,
# where the engine stages copies (`execution._recording_stage`). A root only where `FramePaths.out`
# is set - never on a cloud frame, whose paths are byte-identical without it.
OUT: Final = "/out"
SIDECAR_SOCK: Final = "/run/browser/sidecar.sock"  # 0600, owned by the browser user

# The two frame uids, pinned in the image (`useradd --uid 1000`/`--uid 1001` in the
# Dockerfiles) and shared by every container driver: `put_archive`/sandbar's `Extract` both stamp the uid
# a tar member carries, so these numbers decide whose files land as whose. One place per kind:
# a driver-local copy of either number is the thing that drifts.
AGENT_UID: Final = 1000
BROWSER_UID: Final = 1001

INPUTS: Final = "/task/inputs"  # attachments + a follow-up's parent artifacts, read-only
MANIFEST: Final = "/artifacts/manifest.json"

ALL: Final = (TASK, WORK, ARTIFACTS, SKILLS, VERIFY, INPUTS)


ANSWERS: Final = f"{TASK}/answers.json"
"""What the user said when asked.

In the sandbox, next to the sources, read-only. An answer that lives only in the database is an
answer the agent doing the work never sees - and then the question was an interruption the user paid
for and got nothing back from, which is the fastest way to teach them to always type `--clarify
never`."""


CONTEXT: Final = f"{TASK}/context.md"
"""What the system remembers (the Context Pack, frozen on the task row at planning).

Project instructions, user and project memories, skill notes - rendered by `ai/context/blocks.py`
and mounted read-only like `answers.json`: never a citable source; the instructions and skill notes
instruct, the memories are fenced data. A memory that lives only in a database row is a memory the agent doing the work
never sees."""


THREAD: Final = f"{TASK}/thread.md"
"""The conversation so far - the thread transcript, rendered by `ai/context/thread.py`.

Every earlier turn of this task (and, for a fork, its parent's whole thread): what was asked,
answered, steered and delivered. Mounted read-only like `answers.json`, same framing -
an **instruction**, never a citable source - and rendered to the planning boundary, so every
retry of a turn reads byte-identical history and never its own in-flight narrations. A
conversation that lives only in the event log is a conversation the agent doing the work never
had."""


INSTRUCTIONS: Final = (ANSWERS, CONTEXT, THREAD)
"""What the system mounts beside the sources as **instruction**, never a citable source - each
step's own, mounted again with `/task/inputs` and `/skills` at every step start (ADR-063)."""

SKILLS_RESET: Final = f"chmod -R u+w {SKILLS} 2>/dev/null; find {SKILLS} -mindepth 1 -delete"
"""How a step boundary empties `/skills`, as the agent whose directory it is (`reset_mounts`): a
read-only directory left there must not stop the delete, or every later step of the Machine fails."""


def input_path(filename: str) -> str:
    """Where an attachment lands. **`schemas` may not compute this** - it sits below `ai` in the
    layering law, so it carries the filename and this builds the path. The one definition."""
    return f"{INPUTS}/{filename}"


def input_paths(filenames: Iterable[str]) -> tuple[str, ...]:
    """The contract's `inputs`, and the default `sources` of anything published from them."""
    return tuple(input_path(name) for name in filenames)


# Touched (through `write_bytes(owner="system")`, with a real mtime) at every step start on a
# Machine, so a retry can be shown exactly what the failed attempt changed:
# `find /work -newer /task/.step-start`.
STEP_MARKER: Final = f"{TASK}/.step-start"


EPOCHS: Final = f"{TASK}/.browser-epochs"
"""The browser's snapshot-epoch ledger, where the browser outlives a session: the system's,
0444, in a root the agent reads and never writes; `reset_mounts` leaves it, so a later session starts
past every epoch an earlier one used."""


def is_publishable(path: str) -> bool:
    """A published path must land in /artifacts. Anything else is not a deliverable, by design."""
    return path.startswith(f"{ARTIFACTS}/")


class OutsideFrame(PermissionError, ValueError):
    """A path that is under none of a frame's roots - the host's `~/.ssh`, say. A refusal first
    (the tools hand it to the model, as they do the file door's), and a bad value as it always was."""


@dataclass(frozen=True)
class FramePaths:
    """Where a frame's virtual roots really are. Cloud frames keep the standard roots (the
    path IS the virtual path); a frame on the user's own disk maps each root into a folder
    it owns and sets `on_host`. `out` is the folder deliverables are copied to, when one is set,
    and then the `/out` root. `grants` are a device frame's other granted folders, each under its
    own virtual root (`/grants/<name>`, `schemas.device.grant_roots`) - empty on a cloud frame."""

    task: str = TASK
    work: str = WORK
    artifacts: str = ARTIFACTS
    skills: str = SKILLS
    verify: str = VERIFY
    inputs: str = INPUTS
    browser: str = BROWSER
    out: str | None = None
    on_host: bool = False
    grants: tuple[tuple[str, str], ...] = ()

    def pairs(self) -> list[tuple[str, str]]:
        """(virtual, real) for every root, longest virtual first so `/task/inputs` beats `/task`.
        `/out` and the grants are roots only where set (a device frame): a cloud frame has none."""
        roots = [
            (TASK, self.task),
            (WORK, self.work),
            (ARTIFACTS, self.artifacts),
            (SKILLS, self.skills),
            (VERIFY, self.verify),
            (INPUTS, self.inputs),
            (BROWSER, self.browser),
        ]
        if self.out:
            roots.append((OUT, self.out))
        roots.extend(self.grants)
        return sorted(roots, key=lambda r: len(r[0]), reverse=True)

    def also_granted(self) -> str:
        """The line a system prompt adds for a device frame's other folders, by their real paths:
        nothing else reaches one from a shell. Empty where there are none."""
        folders = ", ".join(f"`{real}`" for _virtual, real in self.grants)
        return f"\n\nThe user also granted this task: {folders}." if folders else ""

    def render(self, text: str) -> str:
        """`render(text, self)` - a method so the tools, which may not import `frames`, can call it."""
        return render(text, self)

    def virtual(self, path: str) -> str | None:
        """`to_virtual(path, self)`, or None when the path is under none of the roots."""
        try:
            return to_virtual(path, self)
        except OutsideFrame:
            return None

    def agent_writable(self, path: str) -> str | None:
        """`path` as the virtual path an agent may write, or None (`_agent_path`). Writable:
        `/work` and `/artifacts`, and on the user's own disk also `/out` and the granted folders
        (the device helper's file door still decides which grant takes a write). Never `/task`,
        `/verify`, `/skills` or the browser's profile and code: a cloud driver writes as root, so
        the uid split cannot be what refuses those."""
        return self._agent_path(path, (WORK, ARTIFACTS))

    def agent_readable(self, path: str) -> str | None:
        """`path` as the virtual path an agent may read, or None (`_agent_path`; an empty path or
        one holding a NUL is None). Readable: `/work`, `/artifacts`, `/task` (its inputs included)
        and `/skills`, and on the user's own disk also `/out` and the granted folders. Never the
        browser's profile, `/verify` or `/run`: a cloud driver reads as the sandbox's own uid, and
        which of its folders are the agent's is the layout's to know, not the daemon's."""
        if not path or "\x00" in path:
            return None
        return self._agent_path(path, (WORK, ARTIFACTS, TASK, SKILLS))

    def _agent_path(self, path: str, roots: tuple[str, ...]) -> str | None:
        """`path` as a virtual path under one of `roots` (and on the user's own disk the granted
        folders and `/out`), or None. Normalized first, a relative path read under `/work` and a
        real path judged as its virtual one, so a `..` cannot climb out of the root it is tested
        against; the driver is handed the clean path, never the model's spelling."""
        clean = posixpath.normpath(path if path.startswith("/") else f"{WORK}/{path}")
        clean = "/" + clean.lstrip("/")  # normpath keeps a leading `//`, which no root is under
        virtual = clean if any(_under(clean, v) for v, _real in self.pairs()) else self.virtual(clean)
        allowed = list(roots)
        if self.on_host:
            allowed += [v for v, _real in self.grants] + ([OUT] if self.out else [])
        return virtual if virtual is not None and any(_under(virtual, r) for r in allowed) else None


STANDARD_PATHS: Final = FramePaths()

# The boundary navigate.py uses for `/work`: not the tail of a URL, a relative path or a word.
_ROOT_BOUNDARY: Final = r"(?<![\w.:/])"


def render(text: str, paths: FramePaths) -> str:
    """`text` with its virtual roots spelled as `paths` really has them. A virtual frame returns
    `text` itself - cloud behaviour is byte-identical."""
    if paths is STANDARD_PATHS or not paths.on_host:
        return text
    pairs = paths.pairs()
    real = dict(pairs)
    pattern = re.compile(_ROOT_BOUNDARY + "(" + "|".join(re.escape(v) for v, _ in pairs) + r")(?=/|\b)")
    return pattern.sub(lambda m: real[m.group(1)], text)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def to_real(path: str, paths: FramePaths) -> str:
    """A virtual path as the frame really has it. Raises `OutsideFrame` under no virtual root."""
    for virtual, real in paths.pairs():
        if _under(path, virtual):
            return real.rstrip("/") + path[len(virtual) :]
    raise OutsideFrame(path)


def to_virtual(path: str, paths: FramePaths) -> str:
    """A real path as the agent knows it. Raises `OutsideFrame` under none of the frame's roots."""
    for virtual, real in sorted(paths.pairs(), key=lambda r: len(r[1]), reverse=True):
        if _under(path, real):
            return virtual + path[len(real.rstrip("/")) :]
    raise OutsideFrame(path)
