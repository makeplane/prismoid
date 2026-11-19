# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one redaction policy for tool labels: what the journal's `summary` and the live
`activity.label` may say about a tool call.

The rule: labels come from *locator-named* arguments only - a path, a clamped command, a
`type:path` pair, a count - **never a content field, never raw args**. `frame_tools.py`
builds its journal summaries from these primitives and `MessageTap` builds its live labels
from `label_for`, so the two surfaces cannot drift: one policy, one file, one test.

A frame on the user's own disk has its paths said for the record (`SpellsVirtual`), before the
clamp: the model is told real paths, and its words are the args. A cloud frame's are as they came.

A new tool is one `catalog.TOOLS` row and one `label_for` branch. A tool nobody registered (a
connector's, whose names are open vocabulary, or a hallucination) labels with its arg keys only
("args: to, subject"), the same keys-only summary the connector journal writes.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

from prismoid.ai.tools import catalog
from prismoid.schemas.egress import sites_in, strip_control

LABEL_MAX = 160
COMMAND_MAX = 80  # a command's label is clamped shorter than a path's

# The model-facing names `label_for` knows, checked against the catalog once.
_PATH_TOOLS: Final = catalog.known("write_file", "edit", "read_file")
_SHELL: Final = catalog.tool("shell").name
_PUBLISH: Final = catalog.tool("publish").name
_ASK_USER: Final = catalog.tool("ask_user").name
_GENERATE_IMAGE: Final = catalog.tool("generate_image").name
_WEB_FETCH: Final = catalog.tool("web_fetch")
_WEB_SEARCH: Final = catalog.tool("web_search").name


@runtime_checkable
class SpellsVirtual(Protocol):
    """A frame whose real paths are the user's own: it says how a path, and a command's text, reads
    on the record. Structural, because `tools` may not import `frames`. Only the device driver has
    one, so a cloud frame's labels are the model's words, byte for byte."""

    def shown(self, path: str) -> str: ...
    def virtual_text(self, text: str) -> str: ...


_C1_AND_SEPARATORS: Final = str.maketrans(
    {**{chr(c): None for c in range(0x80, 0xA0)}, "\u2028": " ", "\u2029": " "}
)


def terminal_line(text: str) -> str:
    """Page-controlled text made safe for a one-line surface a terminal may show (a gate preview, a
    title, a journal label): C0/DEL, C1 and the Unicode line/paragraph separators go, so an ESC or a
    `\r` cannot repaint the approver's line, and every whitespace run collapses to one space. Never
    applied to the fingerprint sent as `expect`, which must match the live name byte for byte."""
    return " ".join(strip_control(text).translate(_C1_AND_SEPARATORS).split())


def clamp(text: str, limit: int = LABEL_MAX) -> str:
    return text if len(text) <= limit else text[:limit]


def said_path(path: str, frame: object = None) -> str:
    """`path` as the record may hold it."""
    return frame.shown(path) if isinstance(frame, SpellsVirtual) else path


def path_label(path: str, frame: object = None) -> str:
    return clamp(said_path(path, frame))


def command_label(command: str, frame: object = None) -> str:
    return clamp(frame.virtual_text(command) if isinstance(frame, SpellsVirtual) else command, COMMAND_MAX)


def publish_label(type_: str, path: str, frame: object = None) -> str:
    return clamp(f"{type_}:{said_path(path, frame)}")


def egress_label(host: str) -> str:
    """The egress-denial summary: the HOST is the locator (`host=pypi.org`) - never a URL,
    never a payload. The web reads this format back (progress-copy strips the prefix), so
    it changes here or not at all."""
    return clamp(f"host={host or '?'}")


def fetch_label(url: str) -> str:
    """The web door's locator: host + path, NEVER the query string - query params
    are where tokens and exfiltration ride, and a journal is forever."""
    parts = urlsplit(str(url or ""))
    return clamp(f"{parts.hostname or '?'}{parts.path or ''}")


def browser_label(fp: dict[str, str] | None, verb: str) -> str:
    """The browser family's locator: host/path plus the element's role and
    accessible name - never a value the model typed, never a query string. A `press` keeps its
    key, so pressing Enter and Escape on the same element never read alike."""
    if not fp:
        return clamp(verb)
    name = fp.get("name", "")
    element = " ".join(part for part in (fp.get("role", ""), f'"{name}"' if name else "") if part)
    key = verb.removeprefix("press ") if verb.startswith("press ") else ""
    where = f"{fp.get('host', '?')}{fp.get('path', '')}"
    return clamp(" · ".join(part for part in (where, element, key) if part))


# The journal names whose `ok` call proves a page was read, the receipt's `links:resolve` proof.
# `web.fetch` and `browser.open` journal the page they asked for (`fetch_label`), a snapshot the
# page its tree came from (`where`), an act on an element the page the element was on
# (`browser_label`'s first word). Back, wait, scroll and a screenshot name no page; a search is
# no read (its results are never journaled, and a snippet is not the page).
READ_TOOLS: Final[frozenset[str]] = frozenset(
    catalog.journal_name(name)
    for name in catalog.known(
        "web_fetch",
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_fill",
        "browser_select",
        "browser_press",
    )
)


def page_read(tool: str, summary: str) -> str | None:
    """The page (host+path) an `ok` call of `tool` proves was read, or None: a fetch's whole label, a
    browser label's first word; a page with no host (`about:blank`) is none."""
    if tool not in READ_TOOLS:
        return None
    page = summary if tool == _WEB_FETCH.journal else summary.split(" ", 1)[0]
    return page if page[:1] not in ("", "/", "?") else None


REASON_MAX = 300  # a denial reason is a debugging line, not a transcript


def scrub_reason(text: str) -> str:
    """The redaction law applied to the other field that reaches the journal, `denied_reason`:
    an exception's message names the URL it failed on, query string and all. Every URL in it
    collapses to host+path (`fetch_label`), then the reason is clamped. The model still sees the
    full URL in the tool's result; only the record is redacted."""
    text = text or ""
    kept: list[str] = []
    at = 0
    for site in sites_in(text):
        if site.url is not None:
            kept += (text[at : site.start], fetch_label(site.url))
            at = site.end
    kept.append(text[at:])
    return clamp("".join(kept), REASON_MAX)


def label_for(tool_name: str, args: dict[str, Any], frame: object = None) -> str:
    """The live label for a tool call, from its (complete, validated-or-not) args. `frame` is the
    step's, for the two surfaces to say a path alike."""
    if tool_name in _PATH_TOOLS:
        return path_label(str(args.get("path", "")), frame)
    if tool_name == _SHELL:
        return command_label(str(args.get("command", "")), frame)
    if tool_name == _PUBLISH:
        return publish_label(str(args.get("type", "")), str(args.get("path", "")), frame)
    if tool_name == _ASK_USER:
        questions = args.get("questions")
        n = len(cast("list[object]", questions)) if isinstance(questions, list) else 0
        return f"{n} question{'' if n == 1 else 's'}"
    if tool_name == _GENERATE_IMAGE:
        # Locators only: the destination path, the shape, the count. The PROMPT is content and
        # never reaches a label or a journal - the keys-not-values law, applied to media.
        n = args.get("n", 1)
        return clamp(f"{said_path(str(args.get('path', '')), frame)} {args.get('size', 'square')} n={n}")
    if tool_name == _WEB_FETCH.name:
        return fetch_label(str(args.get("url", "")))
    if tool_name == _WEB_SEARCH:
        # The QUERIES are content and never reach a label (the generate_image prompt's law): their count does.
        queries = args.get("queries")
        k = len(cast("list[object]", queries)) if isinstance(queries, list) else 0
        n = args.get("n") or 0
        said = [f"{k} quer{'y' if k == 1 else 'ies'}"] if k else []
        return ", ".join([*said, *([f"{n} per query"] if n else [])]) or "web"
    if args:
        # A tool this file does not know - a connector's, usually. Arg KEYS are
        # locators enough for a live label ("args: to, subject"); values stay off the wire,
        # the same keys-only summary the connector journal writes.
        return clamp("args: " + ", ".join(sorted(str(k) for k in args)))
    return ""  # unknown tool with no args: the name is visible alone
