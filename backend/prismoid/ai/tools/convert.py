# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversion sidecar - markitdown behind a process boundary (ADR-046).

Fetched binaries are untrusted input, and markitdown's parsers have recorded sharp edges (an
open XXE report in the DOCX math path, no decompression caps on its in-memory ZIP recursion).
So the fetch tool never converts in-process: the parent (`ai/tools/fetch.py`) runs
`python -m prismoid.ai.tools.convert` with the bytes on stdin and a wall-clock kill; this child
sets its OWN resource limits - address space, CPU seconds, file-size - *before* importing
markitdown, converts, and prints exactly one JSON object on stdout:

    {"ok": true, "title": "...", "markdown": "...", "chars": N}
    {"ok": false, "error": "ExceptionName: reason"}

A conversion failure is `ok: false` with exit 0 - a readable answer, not a stack trace; a
resource-limit death is a killed process the parent reads as one. The same doctrine as the
publish hand-off (the static-runner law): untrusted bytes meet a parser only inside
something the system can kill.

`convert` is the parent's side, the one way the worker runs the child: the web door and the Lead's
reading of a turn's attachments both call it.

The frame's `read_file` calls it too, on a document's bytes read out of the sandbox, so an agent
without a shell (a scatter seat) reads a PDF, a spreadsheet or a deck as text.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import tempfile
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

_ADDRESS_SPACE_BYTES = 2_000_000_000  # a decompression bomb dies here, not in the worker's RSS
_CPU_SECONDS = 120  # a busy-loop parse dies here even if the parent's wall clock misfires
_FILE_SIZE_BYTES = 10_000_000  # conversion is in-memory; a converter writing files is a bug

# How many children one worker runs at once. Each may take its full address-space limit, and every
# tenant's tasks share the worker, so a stranger firing parallel reads of crafted files waits for a
# slot instead of multiplying the memory.
CONVERT_SLOTS: Final = 2
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()

# All a child needs from the worker's environment: where executables are and the locale.
_CHILD_ENV: Final = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")

# Binary media types the sidecar converts, -> the extension hint markitdown routes on.
CONVERTS: Final[dict[str, str]] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/epub+zip": ".epub",
}


def document_of(path: str) -> tuple[str, str] | None:
    """The (media type, extension) of a document the sidecar converts, by `path`'s extension; else None."""
    suffix = PurePosixPath(path).suffix.lower()
    return next(((media, ext) for media, ext in CONVERTS.items() if ext == suffix), None)


@dataclass(frozen=True, slots=True)
class Converted:
    title: str
    markdown: str  # cut at the caller's `max_chars`


class ConvertFailed(Exception):
    """The sidecar timed out, died or could not read the bytes; the message says which."""


def converter_env(environ: Mapping[str, str], *, home: str) -> dict[str, str]:
    """The child's whole environment. It parses a stranger's file, so it gets none of the worker's
    model keys, database URL, cipher or storage credentials: a parser exploit there finds nothing
    to steal. HOME and TMPDIR point at the conversion's own empty directory."""
    kept = {name: environ[name] for name in _CHILD_ENV if name in environ}
    return {**kept, "HOME": home, "TMPDIR": home}


def _sidecar_slots() -> asyncio.Semaphore:
    """One semaphore per event loop (a semaphore binds to the loop it first waits on)."""
    loop = asyncio.get_running_loop()
    slots = _slots.get(loop)
    if slots is None:
        slots = _slots[loop] = asyncio.Semaphore(CONVERT_SLOTS)
    return slots


async def _answer_of(
    proc: asyncio.subprocess.Process, data: bytes, *, extension: str, timeout_s: float
) -> tuple[bytes, bytes]:
    """The child's stdout and stderr, or `ConvertFailed` when it runs past `timeout_s`. However the
    wait ends (a timeout, a task cancel, the step's own deadline), a child still running is killed
    and reaped before the caller hands its slot on, or more children than the slots would run."""
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout=timeout_s)
    except TimeoutError:
        raise ConvertFailed(
            f"converting the {extension} timed out after {int(timeout_s)}s - "
            "the file may be malformed or enormous"
        ) from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def convert(
    data: bytes, *, extension: str, media_type: str, max_chars: int, timeout_s: float
) -> Converted:
    """Run the child on `data` under a wall-clock kill; its markdown, or `ConvertFailed`. The child
    is isolated: `-I` ignores PYTHON* variables and the working directory, the environment carries
    no secret, the working directory is empty, and at most `CONVERT_SLOTS` run at once."""
    async with _sidecar_slots():
        with tempfile.TemporaryDirectory(prefix="prismoid-convert-") as home:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "prismoid.ai.tools.convert",
                "--extension",
                extension,
                "--media-type",
                media_type,
                "--max-chars",
                str(max_chars),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=home,
                env=converter_env(os.environ, home=home),
            )
            out, err = await _answer_of(proc, data, extension=extension, timeout_s=timeout_s)
    try:
        payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    if not payload.get("ok"):
        # A chatty parser can buffer megabytes of stderr; only 200 chars are ever shown.
        reason = str(payload.get("error") or "") or err[:2000].decode("utf-8", "replace")[:200].strip()
        raise ConvertFailed(
            f"could not read the {extension} - {reason or f'converter exited {proc.returncode}'}"
        )
    return Converted(str(payload.get("title") or ""), str(payload.get("markdown") or ""))


def _limit() -> None:
    """Self-imposed rlimits, set before markitdown (and its onnxruntime) are ever imported.
    Best-effort by platform: RLIMIT_AS is decorative on macOS (dev) and real on Linux (prod);
    CPU and FSIZE hold on both."""
    import contextlib
    import resource

    for name, ceiling in (
        ("RLIMIT_AS", _ADDRESS_SPACE_BYTES),
        ("RLIMIT_CPU", _CPU_SECONDS),
        ("RLIMIT_FSIZE", _FILE_SIZE_BYTES),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        with contextlib.suppress(ValueError, OSError):  # a stricter parent limit already applies
            resource.setrlimit(limit, (ceiling, ceiling))


def main() -> None:
    parser = argparse.ArgumentParser(description="stdin bytes → markdown JSON, resource-limited")
    parser.add_argument("--extension", default="", help="filename-extension hint, e.g. .pdf")
    parser.add_argument("--media-type", default="", help="content-type hint from the wire")
    parser.add_argument("--max-chars", type=int, default=400_000)
    args = parser.parse_args()

    _limit()
    data = sys.stdin.buffer.read()

    try:
        from markitdown import MarkItDown, StreamInfo

        converter = MarkItDown(enable_plugins=False)  # plugins are somebody else's code; none here
        result = converter.convert_stream(
            io.BytesIO(data),
            stream_info=StreamInfo(
                extension=args.extension or None,
                mimetype=args.media_type or None,
            ),
        )
        markdown = result.markdown or ""
        payload = {
            "ok": True,
            "title": result.title or "",
            "markdown": markdown[: args.max_chars],
            "chars": len(markdown),
        }
    except Exception as exc:  # one readable line beats a five-frame traceback on stderr
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
