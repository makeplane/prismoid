# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The proof check (ADR-019): before an action Step's
`done` counts, the system looks for itself. No model call: one page read through the frame's own
browser verb, one directory listing, and the manifest the step already holds.

Pure-ish: it reads the frame through what it is handed and returns the reason a proof failed - a
sentence the model is shown, so it never quotes the page back - or None when the proof holds. It
never raises: a check that cannot look is a failed proof, said plainly. Documents keep the
answer-first order (deliver, then verify); this is for steps whose deliverable is a change."""

from __future__ import annotations

import contextlib
import json
import posixpath
import shlex
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STEP_MARKER, FramePaths, OutsideFrame, to_virtual
from prismoid.ai.tools import catalog
from prismoid.ai.tools.browser import TOOL_NAMES
from prismoid.ai.tools.browser_policy import state_of
from prismoid.ai.tools.call_rows import never_ran, ran
from prismoid.schemas.outputs import DONE_TOOL, Outcome

Browse = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

NO_PROOF: Final = (
    "nothing shows it: there is no proof, nothing was published, and neither a command's output nor a "
    "service's own id, title or link is quoted - give the page and a short phrase on it (what changed, or "
    "the answer you read there), or quote what the service's tool returned"
)
URL_ONLY: Final = (
    "a url alone shows only that a page loaded - add `page_text`, a short phrase on that page now "
    "(for something you looked up, a line or figure you read there)"
)
_NO_BROWSER: Final = "this step has no browser to look at the page with"
_SHELL_TOOL: Final = "shell"  # `frame.shell`, as the model calls it
_NO_OUTPUT: Final = "(exit 0, no output)"
_MIN_QUOTE: Final = 3  # a quoted line shorter than this proves nothing ("ok", "1")
_MIN_VALUE: Final = 6  # a service's value shorter than this proves nothing ("page", "true", "200")
_SHOWN: Final = 120  # how much of a model-given value a reason repeats
# Advisory, never an order to send: a task can say "fill it in but don't submit it", and the person's
# words outrank the proof. Left unsent on purpose, the step finishes `partial` and says so.
UNSAVED: Final = (
    "you changed a field and nothing saved it after that - confirm it is saved where the task wants "
    "it and quote what the page shows; if the task says not to save or send it, leave it as it is "
    "and finish with `partial`, saying it is filled in and not sent"
)
# The browser calls that change a field. Every other browser call but a look counts as a save:
# a click, a key, a hand-off, leaving the page - the rule only fires when nothing at all followed.
_EDITS: Final = frozenset({"browser_type", "browser_fill", "browser_select"})
_LOOKS: Final = frozenset({"browser_snapshot", "browser_scroll", "browser_wait", "browser_screenshot"})
# The proof check reads the whole page (the sidecar's `SNAPSHOT_CHARS_MAX`, one cap over the read's
# fields), not the model's window:
# a quote the model read with `find`, or further down a long article, is on the page all the same.
PAGE_READ: Final = 400_000
_FOLD: Final = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "}
)


class Lists(Protocol):
    paths: FramePaths

    async def list_dir(self, path: str) -> list[str]: ...

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult: ...  # noqa: ASYNC109


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _host(url: str) -> str:
    host = (urlsplit(url if "//" in url else f"//{url}").hostname or "").casefold()
    return host.removeprefix("www.")


def quotes_shell(summary: str, messages: Sequence[ModelMessage]) -> bool:
    """Does the summary quote a line a `shell` call printed (`cat /work/hello.txt` → "hello")?"""
    said = _flat(summary)
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            # A retried call never ran: its text is the refusal, not anything the shell printed.
            if not ran(part) or part.tool_name != _SHELL_TOOL:
                continue
            content = str(part.content)
            if content == _NO_OUTPUT:
                continue
            for raw in content.splitlines():
                line = _flat(raw.removeprefix("[stderr]"))
                if len(line) >= _MIN_QUOTE and line in said:
                    return True
    return False


def quotes_service(summary: str, messages: Sequence[ModelMessage]) -> bool:
    """Does the summary quote a value a connector call returned (an id, a title, a link), as the service wrote it?"""
    said = _flat(summary)
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not ran(part) or never_ran(part) or not _connector(part.tool_name):
                continue
            if any(len(value) >= _MIN_VALUE and value in said for value in _values(part.content)):
                return True
    return False


def _connector(name: str) -> bool:
    """A connector's tool: one the tool catalog does not hold, and not the `done` the run ends with."""
    try:
        catalog.tool(name)
    except KeyError:
        return name != DONE_TOOL
    return False


def _values(content: object) -> Iterator[str]:
    """The scalar values in a tool's return, flattened: a JSON text is read as its structure."""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            yield from (_flat(line) for line in content.splitlines())
            return
    if isinstance(content, Mapping):
        for value in content.values():
            yield from _values(value)
    elif isinstance(content, list | tuple):
        for value in content:
            yield from _values(value)
    elif isinstance(content, str | int | float) and not isinstance(content, bool):
        yield _flat(str(content))


def unsaved_edit(messages: Sequence[ModelMessage]) -> bool:
    """Did the run's last field change go unfollowed by anything that could have saved it? The page alone
    cannot tell; the run's calls can."""
    landed = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if ran(part) and (state := state_of(part.metadata)) is not None and state.ok
    }
    pending = False
    for part in _browser_calls(messages):
        if part.tool_name in _EDITS:
            if part.tool_call_id in landed:
                pending = part.args_as_dict().get("submit") is not True
        elif part.tool_name not in _LOOKS:
            pending = False
    return pending


def _browser_calls(messages: Sequence[ModelMessage]) -> Iterator[ToolCallPart]:
    """The run's browser calls, in the order the model made them."""
    for message in messages:
        if isinstance(message, ModelResponse):
            yield from (
                part
                for part in message.parts
                if isinstance(part, ToolCallPart) and part.tool_name in TOOL_NAMES
            )


def _said(text: str) -> str:
    """Text as two readers would compare it: quotes and dashes folded, double quotes and every space gone,
    so a sentence the tree split at a link reads whole."""
    return "".join(text.translate(_FOLD).replace('"', " ").split()).casefold()


def _on_page(phrase: str, page: Mapping[str, Any]) -> bool:
    """Is `phrase` contiguous on the page, in its lines as written or in its reading text (a sentence split
    at links reads whole)? The renderer's own lines are in neither."""
    written, reading = str(page.get("written") or ""), str(page.get("reading") or "")
    return _flat(phrase) in _flat(written) or _said(phrase) in _said(reading)


async def _page(url: str, phrase: str, browser: Browse) -> str | None:
    try:
        page = await browser({"verb": "snapshot", "cap": PAGE_READ})
    except Exception as exc:
        return f"the page could not be read ({type(exc).__name__})"
    if not page.get("ok"):
        return f"the page could not be read ({str(page.get('error') or 'failed')[:_SHOWN]})"
    if url:
        now = _host(str(page.get("url") or ""))
        if now != _host(url):
            return f"the browser is on {now or 'no page'}, not {_host(url)}"
    if not _on_page(phrase, page):
        return f"{phrase[:_SHOWN]!r} is not on the page now"
    return None


def _clean(path: str, frame: Lists) -> str:
    clean = posixpath.normpath(path)
    with contextlib.suppress(
        OutsideFrame
    ):  # a real path on a frame on the user's disk reads as its virtual one
        clean = to_virtual(clean, frame.paths)
    return clean


def _outside(path: str, frame: Lists) -> str | None:
    """Why `path` cannot prove anything at all: it is not inside /work (`/work` itself always
    exists, and nothing else is the agent's to show)."""
    return None if _clean(path, frame).startswith("/work/") else f"{path[:_SHOWN]} is not a path inside /work"


async def _untouched(path: str, frame: Lists) -> str | None:
    """Why `path` proves nothing about THIS step: it was there before the step began and nothing at
    or under it changed since (`find -cnewer` the step marker - ctime, so a copy that kept an old
    mtime still counts as new). None when it changed, or when the frame cannot say (no marker: a
    throwaway frame, a device frame; a missing path; a failed `find`) - never a rejection on that."""
    clean = _clean(path, frame)
    command = f"test -e {STEP_MARKER} && find {shlex.quote(clean)} -cnewer {STEP_MARKER} -print -quit"
    try:
        found = await frame.exec(command, timeout=30)
    except Exception:
        return None
    if not found.ok or found.stdout.strip():
        return None
    return (
        f"{clean[:_SHOWN]} was already there when this step began and nothing in it changed - for a "
        "file you only read, quote in the summary a line a command printed from it"
    )


async def _path(path: str, frame: Lists) -> str | None:
    clean = _clean(path, frame)
    parent, name = posixpath.split(clean)
    try:
        names = await frame.list_dir(parent)
    except Exception as exc:
        return f"{clean[:_SHOWN]} could not be checked ({type(exc).__name__})"
    return None if name in names or f"{name}/" in names else f"{clean[:_SHOWN]} does not exist"


def _artifact(path: str, published: Sequence[str]) -> str | None:
    name = posixpath.basename(path.rstrip("/"))
    if name and name in {posixpath.basename(p.rstrip("/")) for p in published}:
        return None
    return f"{path[:_SHOWN]} was not published by this step"


async def check_proof(
    outcome: Outcome,
    *,
    frame: Lists,
    browser: Browse | None,
    published: Sequence[str],
    messages: Sequence[ModelMessage] = (),
) -> str | None:
    """None when `outcome.proof` holds now, else the first reason it does not; a path that cannot prove
    anything is not counted."""
    proof = outcome.proof
    # a blank is absent; a url alone only shows a page loaded, so it is not proof without a phrase
    url = (proof.url or "").strip() if proof else ""
    phrase = (proof.page_text or "").strip() if proof else ""
    path = (proof.path or "").strip() if proof else ""
    artifact = (proof.artifact or "").strip() if proof else ""
    dropped = (_outside(path, frame) or await _untouched(path, frame)) if path else None
    if dropped:
        path = ""
    if phrase and browser is None:
        # a step with no browser cannot show a page; its other evidence (a service's read-back) still counts
        dropped, phrase = dropped or _NO_BROWSER, ""
    if not (phrase or path or artifact):
        if published or quotes_shell(outcome.summary, messages) or quotes_service(outcome.summary, messages):
            return None
        return dropped or (URL_ONLY if url else NO_PROOF)
    if phrase:
        if len(_flat(phrase)) < _MIN_QUOTE:
            return "the phrase is too short to prove anything"
        if not (path or artifact) and unsaved_edit(messages):
            return UNSAVED
        if browser is not None and (failed := await _page(url, phrase, browser)):
            return failed
    if path and (failed := await _path(path, frame)):
        return failed
    if artifact and (failed := _artifact(artifact, published)):
        return failed
    return None


def unproven(outcome: Outcome, reason: str) -> Outcome:
    """The system's word beside a `done` it could not confirm, its nudge spent: `partial`, saying why.
    The model's own Outcome is never rewritten; this is a copy the step settles on."""
    note = f"\n\nNot confirmed: {reason[: _SHOWN * 2]}."
    return outcome.model_copy(
        update={"status": "partial", "summary": outcome.summary[: 2000 - len(note)] + note}
    )
