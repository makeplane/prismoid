#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The page's pre-publish gate - run me: python3 check_page.py page.html

Stdlib only, network-free. Since ADR-033 the page ships as ONE `.html` document - this is
the author's own gate before `publish` (the retired webapp shape carried it as `test.sh`
into a verify sandbox; the document suite reads the published page itself now). It enforces
the two laws a shipped page must obey - it is a real page (doctype, title, one h1, lang,
viewport) and it is SELF-CONTAINED (no external resource loads; the reader may be offline).
Exit 0 = clean; exit 1 lists problems.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

# Matches the reader's inline-preview cage (MAX_PAGE_HTML, ai/frames/preview.py): a page
# past it still downloads and opens fine, but silently loses its in-app preview - the gate
# must fail where the product degrades, not 1.1 MB later. (And a "single page" past ~900 KB
# is a bundle wearing a costume anyway.)
SIZE_BUDGET = 900_000  # bytes

# Resource-loading attributes per element. <a href> is deliberately absent: linking OUT is
# content; LOADING from outside is the violation.
RESOURCE_ATTRS = {
    "script": ("src",),
    "link": ("href",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
}
EXTERNAL = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
CSS_EXTERNAL = re.compile(r"(?:@import\s+['\"]?|url\(\s*['\"]?)\s*(?:https?:)?//", re.IGNORECASE)


class PageScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = 0
        self.lang = ""
        self.viewport = False
        self.external: list[str] = []
        self._in_title = False
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        got = dict(attrs)
        if tag == "html":
            self.lang = (got.get("lang") or "").strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "style":
            self._in_style = True
        elif tag == "meta" and (got.get("name") or "").lower() == "viewport":
            self.viewport = True
        for attr in RESOURCE_ATTRS.get(tag, ()):
            value = (got.get(attr) or "").strip()
            if value and EXTERNAL.match(value):
                self.external.append(f"<{tag} {attr}={value[:80]}>")
        style_attr = got.get("style") or ""
        if CSS_EXTERNAL.search(style_attr):
            self.external.append(f"<{tag} style=…url(//…)>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_style and CSS_EXTERNAL.search(data):
            self.external.append("<style> pulls a remote url(...)/@import")


def check(path: str) -> list[str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    problems: list[str] = []
    if len(raw) > SIZE_BUDGET:
        problems.append(f"{len(raw):,} bytes is over the {SIZE_BUDGET:,} budget for one page")
    text = raw.decode("utf-8")
    if not text.lstrip()[:15].lower().startswith("<!doctype"):
        problems.append("missing <!doctype html> as the first thing in the file")

    scan = PageScan()
    scan.feed(text)
    scan.close()
    if not scan.title.strip():
        problems.append("no <title> - the tab and the reader both need the page's name")
    if scan.h1 != 1:
        problems.append(f"expected exactly one <h1>, found {scan.h1}")
    if not scan.lang:
        problems.append('<html> has no lang attribute (use <html lang="en">)')
    if not scan.viewport:
        problems.append('missing <meta name="viewport" …> - the page will not scale on phones')
    for ref in scan.external:
        problems.append(f"external resource load (self-contained law): {ref}")
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 check_page.py index.html")
        return 2
    try:
        problems = check(sys.argv[1])
    except FileNotFoundError:
        print(f"NOT CLEAN - {sys.argv[1]} does not exist")
        return 1
    except UnicodeDecodeError:
        print("NOT CLEAN - the file is not valid UTF-8")
        return 1
    if problems:
        print(f"NOT CLEAN - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean: real page structure, fully self-contained")
    return 0


if __name__ == "__main__":
    sys.exit(main())
