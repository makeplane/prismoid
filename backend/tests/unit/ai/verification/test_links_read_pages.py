# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`links:resolve` counts the pages a task actually read, not only the frame's egress allowlist.

`web.fetch` and `web.search` run in the worker and never cross the frame's proxy (the web door), and
a browser host a person let through is a grant, not a contract domain. So a page the task read that
way was never on the list `links:resolve` held a link to, and a correct, web-sourced memo signed
FAIL. The journal already holds the proof: every `ok=true` `web.fetch` and every page the browser
loaded, by host+path (`labels.fetch_label`, `browser_label`, the snapshot's `where`). These tests
pin how a link meets that proof: the page, not the host, and never a page only a search returned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prismoid.ai.tools import browser, fetch, labels
from prismoid.ai.verification.suites import document
from prismoid.ai.verification.suites.document import check_links, run
from tests.support.paths import TESTS

FIXTURES = TESTS / "fixtures" / "go_memo"


def _status(memo: str, read: list[str], allow: list[str] | None = None) -> str:
    return str(check_links(memo, [], allow or [], read)["status"])


# ── the page a task read ──────────────────────────────────────────────────────────────────────


def test_a_fetched_page_resolves() -> None:
    memo = "Go 1.27.4 shipped ([release history](https://go.dev/doc/devel/release))."
    assert _status(memo, ["go.dev/doc/devel/release"]) == "pass"


def test_a_fetched_host_with_an_unfetched_path_still_fails() -> None:
    """The page, never the host: reading the release history proves nothing about a blog post the
    agent never opened on the same site."""
    memo = "See [the announcement](https://go.dev/blog/go1.27)."
    result = check_links(memo, [], [], ["go.dev/doc/devel/release"])
    assert result["status"] == "fail"
    assert "https://go.dev/blog/go1.27" in str(result["actual"])


@pytest.mark.parametrize(
    "link",
    [
        "https://pkg.go.dev/vuln/list?q=net%2Fhttp",  # the journal never holds a query string
        "https://pkg.go.dev/vuln/list#GO-2026-6859",  # a fragment is a place on the same page
        "https://pkg.go.dev/vuln/list/",  # a trailing slash is the same page
        "http://PKG.go.dev/vuln/list",  # the scheme is not journaled, and a host has no case
    ],
)
def test_a_read_page_matches_on_host_and_path_alone(link: str) -> None:
    assert _status(f"See [the list]({link}).", ["pkg.go.dev/vuln/list"]) == "pass"


def test_a_read_page_with_parentheses_in_its_path_resolves() -> None:
    """A markdown link keeps one level of balanced parentheses in its target (CommonMark): cut at
    the first `)`, the page a task read would read as one it never did."""
    url = "https://en.wikipedia.org/wiki/Go_(programming_language)"
    assert _status(f"See [Go]({url}).", [labels.fetch_label(url)]) == "pass"
    assert _status(f"See [Go](<{url}>).", [labels.fetch_label(url)]) == "pass"


def test_a_read_page_journaled_with_a_trailing_slash_matches_without_one() -> None:
    assert _status("See [downloads](https://go.dev/dl).", ["go.dev/dl/"]) == "pass"


def test_a_page_whose_label_hit_the_clamp_matches_by_prefix() -> None:
    """A label is clamped at `LABEL_MAX`, so a long path is journaled cut: the cut spelling is a
    prefix of the page that was read, and the link must still resolve."""
    url = "https://groups.google.com/g/golang-announce/c/" + "x" * 200
    journaled = labels.fetch_label(url)
    assert len(journaled) == labels.LABEL_MAX
    assert _status(f"See [the thread]({url}).", [journaled]) == "pass"


def test_a_short_read_page_is_never_a_prefix() -> None:
    """Only a spelling the clamp cut is a prefix: `go.dev/doc` read does not admit `go.dev/doc/x`."""
    assert _status("See [this](https://go.dev/doc/go1.28).", ["go.dev/doc"]) == "fail"


def test_the_suite_clamp_is_the_journal_clamp() -> None:
    """Two spellings of one number, because the suite runs alone in a frame and imports nothing:
    this pins them together."""
    assert document.READ_CLAMP == labels.LABEL_MAX


def test_a_page_the_browser_loaded_resolves() -> None:
    page = labels.page_read("browser.click", 'go.dev/dl · link "go1.27.4.src.tar.gz"')
    assert page == "go.dev/dl"
    assert _status("See [downloads](https://go.dev/dl/).", [page]) == "pass"


def test_a_fabricated_link_still_fails_beside_read_pages() -> None:
    """A URL seen only in search results is not a read (search results are not journaled, and a
    snippet is not a page), and neither is one the model made up."""
    memo = (
        "See [the notes](https://go.dev/doc/devel/release) and [the 1.28 notes](https://go.dev/doc/go1.28)."
    )
    result = check_links(memo, [], [], ["go.dev/doc/devel/release"])
    assert result["status"] == "fail"
    assert result["detail"] == "1 of 2 resolve"


def test_the_receipt_names_a_read_page_as_a_way_to_resolve() -> None:
    result = check_links("See [x](https://nowhere.example/x).", [], [], [])
    assert "read" in str(result["expected"])
    assert str(result["actual"]).startswith("never read")


def test_the_egress_allowlist_still_resolves_a_link() -> None:
    assert _status("See [the docs](https://docs.example.com/guide).", [], ["example.com"]) == "pass"


def test_the_suite_reads_read_pages_from_its_job(tmp_path: Path) -> None:
    memo = tmp_path / "memo.md"
    memo.write_text("Go 1.27.4 shipped ([release history](https://go.dev/doc/devel/release)).\n")
    job: dict[str, object] = {
        "artifact_path": str(memo),
        "artifact_sha256": "0" * 64,
        "sources": [],
        "allow_domains": [],
        "read_pages": ["go.dev/doc/devel/release"],
    }
    checks = {c["name"]: c for c in run(job)}
    assert checks["links:resolve"]["status"] == "pass"


# ── which journaled calls prove a page was read ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "summary", "page"),
    [
        ("web.fetch", "go.dev/doc/devel/release", "go.dev/doc/devel/release"),
        ("browser.open", "go.dev/dl", "go.dev/dl"),
        ("browser.snapshot", "go.dev/blog/go1.27", "go.dev/blog/go1.27"),
        ("browser.click", 'go.dev/dl · link "Archived versions"', "go.dev/dl"),
        ("browser.type", 'pkg.go.dev/search · searchbox "Search"', "pkg.go.dev/search"),
        ("browser.press", 'pkg.go.dev/search · searchbox "Search" · Enter', "pkg.go.dev/search"),
        ("browser.fill", 'example.com/form · textbox "Name"', "example.com/form"),
        ("browser.select", 'example.com/form · combobox "Size"', "example.com/form"),
    ],
)
def test_each_page_proving_call_yields_its_page(tool: str, summary: str, page: str) -> None:
    assert labels.page_read(tool, summary) == page


@pytest.mark.parametrize(
    ("tool", "summary"),
    [
        ("browser.back", "back"),  # names no page
        ("browser.scroll", "scroll down"),
        ("browser.wait", "wait"),
        ("browser.screenshot", "screenshot"),
        ("web.search", "5 results"),  # a search is not a read
        ("frame.read_file", "/work/notes.md"),
        ("browser.snapshot", "?blank"),  # about:blank: no host
        ("browser.click", '/local · button "Go"'),  # no host, never a page a link can name
        ("web.fetch", ""),
    ],
)
def test_a_call_that_names_no_page_yields_none(tool: str, summary: str) -> None:
    assert labels.page_read(tool, summary) is None


def test_an_acting_label_cut_inside_its_page_is_the_cut_page() -> None:
    """An element's label clamped inside its page has no separator left: the whole label is
    the (cut) page, and the suite matches it by prefix."""
    label = labels.browser_label(
        {"host": "go.dev", "path": "/" + "p" * 200, "role": "link", "name": "x"}, "click"
    )
    assert labels.page_read("browser.click", label) == label


def test_every_page_proving_tool_is_a_tool_that_exists() -> None:
    """A renamed browser verb would otherwise drop out of the proof in silence."""
    assert {*browser.BROWSER_TOOLS, fetch.TOOL} >= labels.READ_TOOLS


# ── the regression: the Go release-notes memo ─────────────────────────────────────────────────


def test_the_go_memo_resolves_against_its_journal() -> None:
    """Found live: `PRISMOID_SEARCH_ENGINES=anthropic`, one writer, 4 searches and 13 fetches, a
    correct memo on Go 1.27.4 and 1.26.11 - and `links:resolve` signed "0 of 8 resolve" because
    the allowlist was empty. Against the pages its journal says it read, all 8 resolve. The
    fixture is rebuilt from that run's record (the tools, counts and hosts it journaled; its stack
    is gone): its links carry a fragment, a query and trailing slashes, as a writer's do."""
    memo = (FIXTURES / "memo.md").read_text()
    journal = json.loads((FIXTURES / "journal.json").read_text())
    read = sorted(
        {p for row in journal if row["ok"] and (p := labels.page_read(row["tool"], row["summary"]))}
    )

    before = check_links(memo, [], [])
    after = check_links(memo, [], [], read)

    assert (before["status"], before["detail"]) == ("fail", "0 of 8 resolve")
    assert (after["status"], after["detail"]) == ("pass", "all 8 link(s) resolve")
