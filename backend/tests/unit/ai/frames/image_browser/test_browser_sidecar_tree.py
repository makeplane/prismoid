# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Accessibility nodes -> the snapshot the model reads."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import Any, cast

from tests.support.paths import BACKEND, TESTS

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
# `Accessibility.getFullAXTree` from a real Chromium on `plane_like.html` (a header, a 10-link nav
# list, a 5-row table, a labelled form), stripped to the fields the renderer reads.
CHROME_AX = TESTS / "fixtures" / "ax" / "plane_like.json"
sys.path.insert(0, str(SIDECAR))

from sidecar.tree import render  # noqa: E402


def node(
    nid: str,
    role: str,
    name: str = "",
    children: Sequence[str] = (),
    backend: int = 0,
    value: str | None = None,
    props: Sequence[str] = (),
):
    n = {
        "nodeId": nid,
        "role": {"value": role},
        "name": {"value": name},
        "childIds": list(children),
        "backendDOMNodeId": backend,
        "properties": [{"name": p, "value": {"value": True}} for p in props],
    }
    if value is not None:
        n["value"] = {"value": value}
    return n


def page():
    return [
        node("1", "RootWebArea", "Orders", children=["2", "3", "4", "5"]),
        node("2", "heading", "Orders", backend=10),
        node("3", "textbox", "Search", backend=11, value="widg"),
        node("4", "button", "Save changes", backend=12),
        node("5", "link", "Delete all", backend=13),
    ]


def test_only_interactive_nodes_carry_references_in_document_order():
    snap = render(page(), epoch=3, cap=10_000, password_backend_ids=set())
    assert 'heading "Orders"' in snap.text
    assert '[e3.1] textbox "Search" value="widg"' in snap.text
    assert '[e3.2] button "Save changes"' in snap.text
    assert '[e3.3] link "Delete all"' in snap.text
    assert snap.refs == {"e3.1": 11, "e3.2": 12, "e3.3": 13}
    assert snap.truncated == 0


def test_meta_carries_role_and_name_per_ref():
    snap = render(page(), epoch=3, cap=10_000, password_backend_ids=set())
    assert snap.meta == {
        "e3.1": ("textbox", "Search"),
        "e3.2": ("button", "Save changes"),
        "e3.3": ("link", "Delete all"),
    }


def test_truncated_refs_are_dropped_from_meta_too():
    root = node("1", "RootWebArea", "Big", children=[str(i) for i in range(2, 5002)])
    nodes = [root] + [node(str(i), "button", f"row {i}", backend=i) for i in range(2, 5002)]
    snap = render(nodes, epoch=1, cap=12_000, password_backend_ids=set())
    assert set(snap.meta) == set(snap.refs)
    assert "e1.5001" not in snap.meta


def test_password_values_are_never_rendered():
    nodes = [*page(), node("6", "textbox", "Password", backend=14, value="hunter2")]
    child_ids = cast(list[str], nodes[0]["childIds"])
    child_ids.append("6")
    snap = render(nodes, epoch=1, cap=10_000, password_backend_ids={14})
    assert "hunter2" not in snap.text
    assert '[e1.4] textbox "Password"' in snap.text


def test_cap_truncates_with_a_truthful_marker_inside_the_cap():
    """The marker names the remedy that exists (`find`) - scrolling never brings a cut node back,
    since the tree does not depend on the viewport - and its own length counts against the cap."""
    root = node("1", "RootWebArea", "Big", children=[str(i) for i in range(2, 5002)])
    nodes = [root] + [node(str(i), "button", f"row {i}", backend=i) for i in range(2, 5002)]
    snap = render(nodes, epoch=1, cap=12_000, password_backend_ids=set())
    assert len(snap.text) <= 12_000
    assert (
        snap.text.splitlines()[-1]
        == f'[truncated: {snap.truncated} more nodes - snapshot with find="<text>" to reach one]'
    )
    assert snap.truncated > 4000
    assert "[truncated: " in snap.text
    # references are only minted for nodes that made it into the text
    assert all(f"[{r}]" in snap.text for r in snap.refs)
    assert "[e1.5001]" not in snap.text


def test_ignored_and_generic_nodes_collapse_but_children_survive():
    nodes = [
        node("1", "RootWebArea", "", children=["2"]),
        {**node("2", "generic", "", children=["3"]), "ignored": True},
        node("3", "button", "Go", backend=7),
    ]
    snap = render(nodes, epoch=2, cap=1_000, password_backend_ids=set())
    assert '[e2.1] button "Go"' in snap.text
    assert "generic" not in snap.text


def test_cycle_in_tree_guard_prevents_infinite_loops():
    nodes = [
        node("1", "RootWebArea", "", children=["2", "3"]),
        node("2", "button", "A", backend=10, children=["3"]),
        node("3", "button", "B", backend=11, children=["2"]),
    ]
    snap = render(nodes, epoch=1, cap=10_000, password_backend_ids=set())
    assert '[e1.1] button "A"' in snap.text
    assert '[e1.2] button "B"' in snap.text
    assert snap.text.count("[e1.1]") == 1
    assert snap.text.count("[e1.2]") == 1


def _chrome() -> list[dict[str, Any]]:
    return json.loads(CHROME_AX.read_text())


def _parent_and_line(text: str) -> list[tuple[str, str]]:
    """(nearest shallower line, line) for every line of a snapshot."""
    out: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        depth = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        out.append((stack[-1][1] if stack else "", line.strip()))
        stack.append((depth, line.strip()))
    return out


def _name(line: str) -> str:
    return line.split('"')[1] if '"' in line else ""


def test_chrome_text_children_never_repeat_what_their_line_already_says():
    """Chrome names a link or a button from its text, so the tree is `link "X" > StaticText "X"`:
    every name printed twice, 33-41% of a link-heavy snapshot. A text line that only repeats its
    parent's name, or the label of the field right after it, carries nothing; list bullets carry
    nothing either. Every interactive reference survives."""
    before_refs = 32  # the captured page's interactive elements, every one still referenced
    snap = render(_chrome(), epoch=1, cap=12_000, password_backend_ids=set())
    pairs = _parent_and_line(snap.text)
    assert "ListMarker" not in snap.text
    for parent, line in pairs:
        if line.startswith("StaticText"):
            assert _name(line) not in _name(parent), f"{line!r} repeats {parent!r}"
    lines = [line for _, line in pairs]
    for i, line in enumerate(lines[:-1]):
        if line.startswith("StaticText"):
            assert _name(line) not in _name(lines[i + 1]), f"{line!r} repeats the label of {lines[i + 1]!r}"
    assert len(snap.refs) == before_refs
    assert 'link "Project 0 - Sprint board"' in snap.text
    assert 'StaticText "Fix the login redirect loop number 0"' not in snap.text  # its cell says it
    assert len(snap.text) < 0.6 * 2931, "the old render of this page was 2,931 chars"


def test_text_that_adds_to_its_parent_is_kept():
    nodes = [
        node("1", "RootWebArea", "", children=["2", "4"]),
        node("2", "heading", "Orders", children=["3"]),
        node("3", "StaticText", "Orders"),
        node("4", "paragraph", "", children=["5", "6"]),
        node("5", "StaticText", "Three orders are overdue."),
        node("6", "link", "Review them", backend=9),
    ]
    snap = render(nodes, epoch=1, cap=1_000, password_backend_ids=set())
    assert snap.text.splitlines() == [
        'heading "Orders"',
        'StaticText "Three orders are overdue."',
        '[e1.1] link "Review them"',
    ]


def _saas_page(nav_links: int, fields: int, *, dialog: bool = False) -> list[dict[str, Any]]:
    """Chrome's shape for a SaaS page in DOM order: a header, a long sidebar, then the work."""
    nav = [str(100 + i) for i in range(nav_links)]
    form = [str(1000 + i) for i in range(fields)] + ["1999"]
    tops = ["2", "3", "4"] + (["5"] if dialog else [])
    return [
        node("1", "RootWebArea", "App", children=tops),
        node("2", "banner", "", children=["20"]),
        node("20", "button", "Notifications", backend=20),
        node("3", "navigation", "Main", children=nav),
        *(node(n, "link", f"Project {i}", backend=int(n)) for i, n in enumerate(nav)),
        node("4", "main", "", children=["40", "41"]),
        node("40", "heading", "New work item"),
        node("41", "form", "New work item", children=form),
        *(node(n, "textbox", f"Field {i}", backend=int(n)) for i, n in enumerate(form[:-1])),
        node("1999", "button", "Submit", backend=1999),
        *(
            [
                node("5", "dialog", "Unsaved changes", children=["50"]),
                node("50", "button", "Discard", backend=50),
            ]
            if dialog
            else []
        ),
    ]


def test_the_work_renders_before_the_chrome_and_a_long_sidebar_folds():
    """In DOM order a 200-link sidebar spent the cap before the form, so Submit got no reference
    (real Chromium: 323 nodes cut). The work renders first and a long navigation keeps its first
    links, folding the rest into one line that says how to reach them."""
    snap = render(_saas_page(200, 25), epoch=1, cap=12_000, password_backend_ids=set())
    lines = snap.text.splitlines()
    assert ("button", "Submit") in snap.meta.values()
    submit = next(i for i, line in enumerate(lines) if '"Submit"' in line)
    assert lines.index('heading "New work item"') == 0
    assert submit < lines.index('navigation "Main"')
    assert submit < next(i for i, line in enumerate(lines) if '"Notifications"' in line)
    links = [line for line in lines if " link " in line]
    assert len(links) == 12
    assert '  [188 more links in this navigation - snapshot with find="<text>" to reach one]' in lines
    assert snap.truncated == 0


def test_an_open_dialog_renders_first():
    snap = render(_saas_page(3, 2, dialog=True), epoch=1, cap=12_000, password_backend_ids=set())
    assert snap.text.splitlines()[:2] == ['dialog "Unsaved changes"', '  [e1.1] button "Discard"']


def test_find_narrows_the_snapshot_to_what_matches_and_what_holds_it():
    page = _saas_page(200, 25)
    submit = render(page, epoch=4, cap=12_000, password_backend_ids=set(), find="submit")
    assert submit.text.splitlines() == ['form "New work item"', '  [e4.1] button "Submit"']
    assert submit.refs == {"e4.1": 1999}
    deep = render(page, epoch=5, cap=12_000, password_backend_ids=set(), find="Project 150")
    assert '[e5.1] link "Project 150"' in deep.text, "a folded link is one find away"
    form = render(page, epoch=6, cap=12_000, password_backend_ids=set(), find="new work item")
    assert len(form.refs) == 26, "a match brings what is inside it"
    none = render(page, epoch=7, cap=12_000, password_backend_ids=set(), find="Invoices")
    assert none.text == '[nothing on this page has "Invoices" in its name]' and none.refs == {}
