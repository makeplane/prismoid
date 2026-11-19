# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The frame's tools, as an agent toolset - **only the granted ones exist**.

The agent does not get a frame. It gets *these functions*, and every call lands on the flight
recorder as `tool.called` (`ToolRecorder`); a tool says its own row (`call_rows.say`) only when the
call failed and it answers the model with text instead of raising. That is the difference between
*"the agent may write files"* and *"the agent may write files, and you can see every one it wrote."*

**This module imports neither `frames` nor `deps` nor `agents`, and that is not an accident.**
`tools` and `frames` are *siblings* in the engine contract - mutually independent - and `deps`
and `agents` are *above*. So this file does what a sibling must: it **declares the shape it needs**
(`Frame` below) and is handed something that fits. `DockerFrame` satisfies it without either module
knowing the other exists, which is exactly the property that lets the frame driver be swapped for
gVisor or a remote `framesd` without touching a single tool.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final, Protocol, runtime_checkable

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import BinaryContent
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai import config
from prismoid.ai.tools import catalog, convert, labels
from prismoid.ai.tools.call_rows import JournalTool, Said, say
from prismoid.ai.tools.edits import Edited, Missed, apply
from prismoid.ai.tools.output import OUTPUT_CAP, capped, numbered, shell_text, spill_path
from prismoid.errors import PathRefused, PolicyDenied
from prismoid.schemas.outputs import ArtifactType, publish_types_doc, text_artifact_suffixes

_READ_FILE: Final = catalog.tool("read_file")
_WRITE_FILE: Final = catalog.tool("write_file")
_EDIT: Final = catalog.tool("edit")
_SHELL: Final = catalog.tool("shell")
_PUBLISH: Final = catalog.tool("publish")
# The name the model calls `frame.publish` by: the run guard lets a run told to finish still call it.
PUBLISH: Final = _PUBLISH.name

# The image types a model can see: `read_file` returns these as `BinaryContent`, the pixels
# themselves, because a vision call is the only honest read of a photograph.
IMAGE_MEDIA: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def image_media_for(path: str) -> str | None:
    return IMAGE_MEDIA.get(PurePosixPath(path).suffix.lower())


def refuse_mangled_binary(path: str, text: str) -> None:
    """A non-image binary read as text is context poison: `cat` turns every non-UTF-8 byte
    into U+FFFD (or worse, NULs), and a tool result full of them kills the very model call
    that asked. Refuse with the honest alternative instead of returning garbage."""
    head = text[:2048]
    if "\x00" in head or head.count("\ufffd") > 8:
        raise ModelRetry(
            f"{path} is a binary file - do not read it as text. Process it with `shell` "
            "(python + the pinned toolchain), or give an image its real extension to see it."
        )


class ExecLike(Protocol):
    """What `shell` needs from an exec result - structural, so `frames.ExecResult` fits without an
    import (the sibling rule). Read-only properties, not bare attributes, because `ExecResult` is
    a frozen dataclass - a Protocol demanding writable fields would refuse it."""

    @property
    def exit_code(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...


class PathsLike(Protocol):
    """A frame's `FramePaths`, as far as a tool reads them: the model's words, spelled as the
    frame really has its roots. Structural for the same sibling reason as `Frame`."""

    @property
    def on_host(self) -> bool: ...

    def render(self, text: str) -> str: ...
    def virtual(self, path: str) -> str | None: ...
    def agent_writable(self, path: str) -> str | None: ...
    def agent_readable(self, path: str) -> str | None: ...


class Frame(Protocol):
    """What a tool needs from a sandbox - and nothing more. Structural, not imported."""

    @property
    def paths(self) -> PathsLike: ...

    async def write_file(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...  # media lands as bytes
    async def read_file(self, path: str) -> str: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def exec(self, command: str, timeout: float = ...) -> ExecLike: ...  # noqa: ASYNC109
    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ...) -> None: ...


@runtime_checkable
class AgentExec(Protocol):
    """A frame whose `exec` reads virtual roots in a command because the system writes them there.
    The model's text is not the system's, so `shell` takes this door where a frame has one.
    Structural for the same sibling reason as `Frame`. Only the device driver has one: a cloud
    frame's roots are the paths themselves, and nothing is rewritten."""

    async def agent_exec(self, command: str, timeout: float = ...) -> ExecLike: ...  # noqa: ASYNC109


# Text-like files stream their content; binaries stream *facts* and render on harvest.
# An `.xlsx` byte-stream is not a preview, it is noise with a progress bar. The artifact half of
# this list is DERIVED from the registry (`text_extensions`) so the streaming gate can
# never disagree with the door; the scratch half is the working files an agent composes en route.
_SCRATCH_SUFFIXES = (".json", ".yaml", ".yml", ".py", ".ts", ".js", ".html", ".sql")
TEXT_SUFFIXES = tuple(dict.fromkeys(text_artifact_suffixes() + _SCRATCH_SUFFIXES))


def _is_text(path: str) -> bool:
    return path.lower().endswith(TEXT_SUFFIXES)


# The engine emits an *intent* - a path and its content - and `operations` turns it into an
# ephemeral `artifact.delta` plus, every ~8 KB, a durable `artifact.snapshot`. Same shape as
# `journal_tool`: the engine cannot reach a database and does not know there is one.
PreviewTool = Callable[[str, str], Awaitable[None]]


async def writable_target(frame: Frame, path: str) -> str | None:
    """`path` as the virtual path an agent may write, resolved inside the frame - or None.

    `agent_writable` judges the text. A cloud driver then writes as root and follows every
    symlink on the way (Docker's `put_archive`, sandbar's file write), so an agent that made
    `/work/t -> /task` would carry `/work/t/context.md` into `/task`. So the target is resolved
    in the frame, as the agent's own shell sees it, and judged again, and the write goes to the
    resolved path: a link made before the call cannot redirect it. A target that does not
    resolve is refused. A frame on the user's own disk is left to its helper's file door, which
    opens every component without following a link, and a frame that holds no links at all (paper,
    `follows_links = False`: no shell to make one, no shell to resolve one) writes where it is told."""
    target = frame.paths.agent_writable(path)
    if target is None or frame.paths.on_host or not getattr(frame, "follows_links", True):
        return target
    resolved = await frame.exec(f"realpath -m -- {shlex.quote(target)}", 30.0)
    real = resolved.stdout.strip()
    if resolved.exit_code != 0 or not real.startswith("/"):
        return None
    return frame.paths.agent_writable(real)


def _confined_read_path(frame: Frame, path: str) -> str:
    """The normalised virtual path an agent may read (relative names under /work), else `PathRefused`
    naming the path as the record may hold it (`said_path`)."""
    target = frame.paths.agent_readable(path)
    if target is None:
        raise PathRefused(
            f"{labels.said_path(path, frame)} is outside the files you can read "
            "(/work, /artifacts, /task, /skills)"
        )
    return target


async def _read_text_confined(frame: Frame, path: str) -> str:
    return await frame.read_file(_confined_read_path(frame, path))


async def _read_bytes_confined(frame: Frame, path: str) -> bytes:
    return await frame.read_bytes(_confined_read_path(frame, path))


# A document is converted whole, once per content, and paged like text: a 600-page PDF fits.
_DOCUMENT_MAX_CHARS: Final = 4_000_000
_DOCUMENT_TIMEOUT_S: Final = 120.0


async def _read_document(frame: Frame, path: str, document: tuple[str, str], seen: dict[str, str]) -> str:
    """A document's text from the worker's conversion sidecar, converted once per content in a run."""
    data = bytes(await _read_bytes_confined(frame, path))
    key = hashlib.sha256(data).hexdigest()
    if key not in seen:
        media, extension = document
        page = await convert.convert(
            data,
            extension=extension,
            media_type=media,
            max_chars=_DOCUMENT_MAX_CHARS,
            timeout_s=_DOCUMENT_TIMEOUT_S,
        )
        seen[key] = page.markdown
    return seen[key]


async def _read_binary(
    frame: Frame,
    path: str,
    *,
    documents: dict[str, str],
    offset: int,
    limit: int | None,
    journal: JournalTool | None,
) -> str | BinaryContent | None:
    """An image as itself, a document as its numbered text (a failed conversion is an answer); None
    for a file to read as plain text."""
    media = image_media_for(path)
    if media is not None:
        return BinaryContent(data=bytes(await _read_bytes_confined(frame, path)), media_type=media)
    document = convert.document_of(path)
    if document is None:
        return None
    try:
        return numbered(await _read_document(frame, path, document, documents), offset, limit)
    except convert.ConvertFailed as failed:
        await say(journal, Said.failed(_READ_FILE.journal, failed))
        return frame.paths.render(f"{path}: {failed}. Do not retry this read.")


async def _write_confined(frame: Frame, path: str, content: str) -> str:
    """Write where an agent may (`/work`, `/artifacts`; a device frame's grants and `/out`) and
    return the virtual path written. Confined here, not left to the driver: a cloud driver writes
    as root, so the uid split cannot keep an agent out of the browser sidecar's code, `/task` or
    `/verify`."""
    target = await writable_target(frame, path)
    if target is None:
        raise PolicyDenied(
            f"{labels.said_path(path, frame)} is outside /work and /artifacts - write your files there"
        )
    await frame.write_file(target, content)
    return target


def _refused(frame: Frame, refused: OSError | ValueError | PolicyDenied) -> str:
    """A frame on the user's own disk refuses a path (a protected file, a folder off the frame, a
    grant revoked a moment ago, a folder read as a file, a file past its door's limit): the model's
    to read and go on from, and an ANSWER, not a retry, for the reason `read_file`'s missing file is
    (a `ModelRetry` is spent from `retries=3`, and a model that tries four keys in a row
    would kill the run). The driver spelled the text, so it holds no real path, and it is not
    rendered. A cloud frame's errors stay as they were, and a frame that did not answer refused
    nothing: that is the step's to wait out. A write outside the agent's folders (`PolicyDenied`)
    is refused on every frame, and so is a path a file door or the read fence refused
    (`PathRefused`), whose text already says the path as the record may hold it, so it is not
    rendered either."""
    said = f"Refused: {refused}. Do not retry this path."
    if isinstance(refused, PathRefused):
        return said
    if isinstance(refused, PolicyDenied):
        return frame.paths.render(said)
    if not frame.paths.on_host or isinstance(refused, ConnectionError):
        raise refused
    return said


@dataclass(frozen=True)
class _EditAsked:
    """One `edit` call's arguments, as the model sent them."""

    path: str
    old: str
    new: str
    replace_all: bool


async def _edit_confined(frame: Frame, asked: _EditAsked) -> tuple[str, Edited | Missed]:
    """The virtual path edited and the edit, written back only when it was made. Read as bytes and
    decoded strictly: a file that is not UTF-8 is refused, never written back with its bytes replaced."""
    target = await writable_target(frame, asked.path)
    if target is None:
        raise PolicyDenied(
            f"{labels.said_path(asked.path, frame)} is outside /work and /artifacts - edit your files there"
        )
    text = (await frame.read_bytes(target)).decode()
    made = apply(text, asked.old, asked.new, replace_all=asked.replace_all)
    if isinstance(made, Edited):
        await frame.write_file(target, made.text)
    return target, made


def _edit_refused(frame: Frame, path: str, refused: OSError | ValueError | PolicyDenied) -> str:
    """A missing file or one that is not text is the model's to read and go on from, on every frame."""
    said = labels.said_path(path, frame)
    if isinstance(refused, FileNotFoundError):
        return frame.paths.render(f"{said} does not exist. Create it with write_file.")
    if isinstance(refused, UnicodeDecodeError):
        return frame.paths.render(f"{said} is not UTF-8 text. Change it with shell.")
    return _refused(frame, refused)


async def _edit_said(
    frame: Frame, asked: _EditAsked, *, journal: JournalTool | None, preview: PreviewTool | None
) -> str:
    """Make the edit and say how it went: a refusal or a miss is an answer, never a retry."""
    try:
        target, made = await _edit_confined(frame, asked)
    except (OSError, ValueError, PolicyDenied) as refused:
        await say(journal, Said.failed(_EDIT.journal, refused))
        return _edit_refused(frame, asked.path, refused)
    if isinstance(made, Missed):
        await say(journal, Said(_EDIT.journal, False, denied_reason=made.reason))
        return made.said
    if preview is not None and _is_text(target):
        await preview(labels.said_path(target, frame), made.text)
    return _edited(frame, target, made)


def _edited(frame: Frame, target: str, made: Edited) -> str:
    """What a made edit tells the model: how many places changed and, for one, the lines as they read now."""
    where = frame.paths.render(target)
    if made.replaced > 1:
        return f"edited {where}: replaced {made.replaced} places."
    window = numbered(made.text, max(1, made.first_line - 1), min(40, made.lines + 2))
    return f"edited {where}. The lines around the change now read:\n{window}"


async def _publish_or_refuse(
    frame: Frame, path: str, type: str, title: str, sources: tuple[str, ...]
) -> None:
    # The recording is the engine's evidence, never an agent's: only the engine's own harvest
    # publishes one, so an agent holding `frame.publish` cannot forge it. Checked before the
    # driver's `publish` runs, so no bytes move and no manifest entry is written.
    if type == ArtifactType.RECORDING.value:
        raise PolicyDenied("the recording is published by the engine, never by an agent")
    await frame.publish(path, type, title, sources)


async def _within_cap(frame: Frame, said: str, spill: str) -> str:
    """`said` as the model reads it: whole when short; else its head and tail, the whole of it
    spilled to `spill` under /work and named in the result. A spill that fails costs only the
    file's name, never the result."""
    if len(said) <= OUTPUT_CAP:
        return said
    try:
        written = await _write_confined(frame, spill, said)
    except (OSError, ValueError, PolicyDenied):
        return capped(said)
    return capped(said, whole=frame.paths.render(written))


def _render_docs(frame: Frame, *tools: Callable[..., Any]) -> None:
    """The docstrings the model reads name the virtual roots; a frame on the user's own disk
    spells them as they really are (a cloud frame's `render` returns the text itself)."""
    for tool in tools:
        doc = tool.__doc__ or ""
        if (rendered := frame.paths.render(doc)) is not doc:
            tool.__doc__ = rendered


def frame_toolset(
    granted: tuple[str, ...],
    frame: Frame | None,
    journal: JournalTool | None = None,
    sources: tuple[str, ...] = (),
    preview: PreviewTool | None = None,
) -> FunctionToolset[Any]:
    """Build the toolset an agent is granted. **A tool it did not ask for does not exist for it.**

    `sources` is what the system put in `/task/inputs/`. Every publish carries it, whether the model
    thinks to mention it or not - see `publish` below.

    `preview` is the Lane-2 tap: the user watching this task sees the document appear as it is
    written, rather than a spinner for ninety seconds.
    """
    # `sequential=True` (every tool inherits it): one sandbox has one working directory, and two
    # parallel calls - a write and the build that reads it - raced each other. The browser's
    # toolset runs one at a time for the same reason.
    toolset: FunctionToolset[Any] = FunctionToolset(sequential=True, id="prismoid-frame")
    documents: dict[str, str] = {}  # a converted document's text by its content hash, for paging

    async def write_file(ctx: RunContext[Any], path: str, content: str) -> str:
        """Write a file inside the sandbox. `/work` is scratch; `/artifacts` is the loading dock.

        Write your document to `/work/<name>.md` first, then publish it.
        """
        assert frame is not None
        try:
            path = await _write_confined(frame, path, content)
        except (OSError, ValueError, PolicyDenied) as refused:
            await say(journal, Said.failed(_WRITE_FILE.journal, refused))
            return _refused(frame, refused)

        # **The preview**. The user is watching a blank screen while a model composes 300
        # words; this is the only thing that changes that. Text-like artifacts stream their content;
        # a binary one never streams bytes (an xlsx would be noise), which is why this is gated on
        # the *extension* and not on a flag somebody has to remember to set.
        if preview is not None and _is_text(path):
            await preview(labels.said_path(path, frame), content)

        return f"wrote {frame.paths.render(path)} ({len(content)} bytes)"

    async def edit(
        ctx: RunContext[Any], path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        """Change a file by replacing exact text in it. Prefer this to rewriting a file with write_file.

        Args:
            path: the file to change.
            old_string: the text to replace, copied from the file with its indentation, without the
                line numbers read_file shows. It must match once: add lines around it until it does.
            new_string: the text to put in its place.
            replace_all: replace every match of old_string instead of exactly one.
        """
        assert frame is not None
        asked = _EditAsked(path, old_string, new_string, replace_all)
        return await _edit_said(frame, asked, journal=journal, preview=preview)

    async def read_file(
        ctx: RunContext[Any], path: str, offset: int = 1, limit: int | None = None
    ) -> str | BinaryContent:
        """Read a file from inside the sandbox. An image comes back as the image itself - the model
        sees pixels; a PDF, Word, PowerPoint, Excel or EPUB file comes back as its text; text comes
        back numbered like `cat -n`, as many lines as fit, and a long file ends with the offset to
        read on from.

        Args:
            path: the file to read.
            offset: the line to start from (1 = the top).
            limit: the most lines to return (none: as many as fit).
        """
        assert frame is not None
        try:
            shown = await _read_binary(
                frame, path, documents=documents, offset=offset, limit=limit, journal=journal
            )
            if shown is not None:
                return shown
            text = await _read_text_confined(frame, path)
        except (OSError, ValueError, PathRefused) as refused:
            await say(journal, Said.failed(_READ_FILE.journal, refused))
            if not isinstance(refused, FileNotFoundError):
                return _refused(frame, refused)
            # A missing file is an answer, not a retry: a `ModelRetry` spends the run's retries,
            # while a result costs nothing and lists what exists.
            mounted = ", ".join(sources) if sources else "none - this task has no mounted files"
            hint = f" Create it with {_WRITE_FILE.name} first." if _WRITE_FILE.journal in granted else ""
            return frame.paths.render(
                f"{path} does not exist. Do not retry this read. "
                f"Files that exist for you: {mounted}.{hint} "
                "If you were probing for sources: there are none beyond those listed - proceed "
                "from your own knowledge and the goal."
            )
        refuse_mangled_binary(path, text)
        return numbered(text, offset, limit)

    async def shell(ctx: RunContext[Any], command: str) -> str:
        """Run a shell command inside the sandbox to build an artifact with the toolchain.

        The sandbox has `openpyxl`, `python-docx`, `python-pptx`, `reportlab`, `pypdf`, `pandas`,
        `matplotlib` and `pyyaml` pinned - write a small script to `/work` and run it, e.g.
        `python3 /work/build.py`, to produce an `.xlsx`, `.pptx`, `.docx`, `.pdf` or a program. There is **no network** except this task's allowlist, so this
        cannot exfiltrate or fetch. The exit code (when it is not 0), stderr and stdout come back so
        you can see what happened and fix it; a long output keeps its start and its end, and the
        whole of it is saved to a file the result names.
        Produce the file in `/work`, then `publish` it - `publish` is still the only door to a
        deliverable.
        """
        assert frame is not None
        settings = config.current()

        run = frame.agent_exec if isinstance(frame, AgentExec) else frame.exec
        # The wall clock is conf: a long-form assembly build is minutes of real
        # work; the driver's old hardcoded 120 s called that a hang.
        result: ExecLike = await run(command, timeout=float(settings.worker.shell_timeout_s))
        await say(journal, Said(_SHELL.journal, result.exit_code == 0))  # a non-zero exit is not ok
        # A non-zero exit is the model's to fix, so it is a normal result, never a `ModelRetry`
        # spent from the run's retries.
        said = shell_text(result.exit_code, result.stdout or "", result.stderr or "")
        return await _within_cap(frame, said, spill_path(_SHELL.name, ctx.tool_call_id or ""))

    async def publish(ctx: RunContext[Any], path: str, type: str, title: str) -> str:
        # The docstring the model reads is composed below from the type registry, so the tool
        # never advertises a type list the door disagrees with.
        assert frame is not None
        try:
            # `sources` is never the model's to fill in: they are exactly the files the system
            # wrote into `/task/inputs/`, and the grounding checks re-derive from this list.
            await _publish_or_refuse(frame, path, type, title, sources)
        except PolicyDenied as denied:
            # A refused publish (a wrong type, an unwritten file) is the model's to fix in one turn:
            # the error message is the fix, spelled in the frame's own paths so it works in `shell`.
            raise ModelRetry(frame.paths.render(str(denied))) from None
        return f"published {path} as {type}"

    publish.__doc__ = (
        "**The only way a file becomes a deliverable.** Publishes /work → /artifacts.\n\n"
        f"`type` must be one of: {publish_types_doc(on_host=frame is not None and frame.paths.on_host)}.\n\n"
        "Publish a file you have already written with `write_file`. Prose is a `document`."
    )

    if frame is None:
        return toolset  # no sandbox on this step - it gets no frame tools, rather than broken ones

    _render_docs(frame, write_file, edit, shell, publish)

    # Each mount is its own typed call: `add_function` cannot type-check a union of tool signatures.
    mounts: dict[str, Callable[[], object]] = {
        _WRITE_FILE.journal: lambda: toolset.add_function(write_file, name=_WRITE_FILE.name),
        _EDIT.journal: lambda: toolset.add_function(edit, name=_EDIT.name),
        _READ_FILE.journal: lambda: toolset.add_function(read_file, name=_READ_FILE.name),
        _SHELL.journal: lambda: toolset.add_function(shell, name=_SHELL.name),
        _PUBLISH.journal: lambda: toolset.add_function(publish, name=PUBLISH),
    }
    for grant, mount in mounts.items():
        if grant in granted:
            mount()
    return toolset
