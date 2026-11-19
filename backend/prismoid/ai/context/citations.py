# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Source-id citations: the marks a citing run writes (`[S3]`), the pages its brief lists by id, and the
published text with each mark linked to its page; an id the task never gave is refused."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from prismoid.ai.tools.labels import fetch_label
from prismoid.ai.tools.research import cited_ids, relink_citations, source_number


class UnknownSource(ValueError):
    """A text cites a source id no page this task read was given."""

    def __init__(self, ids: Sequence[str], sources: Mapping[str, str]) -> None:
        self.ids = tuple(ids)
        listed = ", ".join(sorted(sources, key=source_number)) or "none"
        super().__init__(
            f"{', '.join(f'[{i}]' for i in ids)} names no page this task read (its sources: {listed}). "
            "Cite only the ids you were given, or drop the claim."
        )


def sources_brief(sources: Mapping[str, str]) -> str:
    """The task's pages by id, for a citing step's brief, as host+path (never a query); empty when none."""
    if not sources:
        return ""
    listed = "\n".join(f"- [{sid}] {fetch_label(sources[sid])}" for sid in sorted(sources, key=source_number))
    return (
        f"\n\nPages this task has read, by source id. Cite a claim with its id, never with a URL:\n{listed}"
    )


def unknown_sources(text: str, sources: Mapping[str, str]) -> list[str]:
    """The ids `text` cites that `sources` does not hold, in first-cited order."""
    return [sid for sid in cited_ids(text) if sid not in sources]


def render_citations(text: str, sources: Mapping[str, str]) -> str:
    """`text` with each cited id as a numbered link to its page and the pages listed under Sources,
    numbered in first-cited order; unchanged when it cites nothing. Raises `UnknownSource`."""
    (body,), listed = render_parts([text], sources)
    return f"{body.rstrip()}\n\n{listed}" if listed else text


def render_parts(texts: Sequence[str], sources: Mapping[str, str]) -> tuple[list[str], str]:
    """Each of `texts` (one document's parts, in order) with its cited ids linked under ONE numbering, and
    that numbering's `## Sources` list (empty when nothing is cited). Raises `UnknownSource`."""
    if unknown := unknown_sources("\n".join(texts), sources):
        raise UnknownSource(unknown, sources)
    numbers: dict[str, int] = {}

    def linked(ids: list[str]) -> str:
        return "".join(f"[[{numbers.setdefault(sid, len(numbers) + 1)}]](<{sources[sid]}>)" for sid in ids)

    bodies = [relink_citations(text, linked) for text in texts]
    listed = "\n".join(f"{n}. <{sources[sid]}>" for sid, n in numbers.items())
    return bodies, (f"## Sources\n\n{listed}\n" if numbers else "")
