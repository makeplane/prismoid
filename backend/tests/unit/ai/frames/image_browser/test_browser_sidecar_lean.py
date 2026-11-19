# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The model's lean look at a page (`render(lean=True)`): visible first, a card on one line so a
price sits beside its title, headings as `## text`, a native select's choices inline, a `*` on a
control the last look did not have, and a cut that keeps the top four fifths and the last fifth.
The tree as written (`lean=False`, the proof check's read) is `test_browser_sidecar_tree.py`'s."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.tree import Screen, interactive_ids, render, screen_of  # noqa: E402


def node(
    nid: str,
    role: str,
    name: str = "",
    children: Sequence[str] = (),
    backend: int | None = None,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    n: dict[str, Any] = {
        "nodeId": nid,
        "role": {"value": role},
        "name": {"value": name},
        "childIds": list(children),
        "properties": [{"name": k, "value": {"value": v}} for k, v in (props or {}).items()],
    }
    if backend is not None:
        n["backendDOMNodeId"] = backend
    return n


def card(i: int, title: str, price: str) -> list[dict[str, Any]]:
    """A product tile the way shops build one: unnamed divs around a title link, a rating, a
    price in pieces and an add-to-cart button - no list, no landmark."""
    p = f"c{i}"
    return [
        node(p, "generic", children=[f"{p}t", f"{p}r", f"{p}p"], backend=1000 + i * 10),
        node(f"{p}t", "link", title, children=[f"{p}h"], backend=1001 + i * 10),
        node(f"{p}h", "heading", title, children=[f"{p}hs"]),
        node(f"{p}hs", "StaticText", title),
        node(f"{p}r", "generic", children=[f"{p}r1", f"{p}r2"], backend=1002 + i * 10),
        node(f"{p}r1", "StaticText", "4.5 out of 5 stars"),
        node(f"{p}r2", "StaticText", "2,345 ratings"),
        node(f"{p}p", "generic", children=[f"{p}p1", f"{p}p2", f"{p}b"], backend=1003 + i * 10),
        node(f"{p}p1", "StaticText", "₹"),
        node(f"{p}p2", "StaticText", price),
        node(f"{p}b", "button", "Add to cart", backend=1004 + i * 10),
    ]


def shop(n: int = 3) -> list[dict[str, Any]]:
    cards = [x for i in range(n) for x in card(i, f"Samsung 990 PRO {i}TB NVMe SSD", f"{6_499 + i:,}")]
    return [
        node("1", "RootWebArea", "Results", children=["h", "grid"]),
        node("h", "heading", "Results", children=["hs"], backend=2),
        node("hs", "StaticText", "Results"),
        node("grid", "generic", children=[f"c{i}" for i in range(n)], backend=3),
        *cards,
    ]


def lean(nodes: list[dict[str, Any]], **kw: Any) -> str:
    kw.setdefault("cap", 12_000)
    return render(nodes, epoch=4, password_backend_ids=set(), lean=True, **kw).text


def test_a_card_is_one_line_so_its_price_sits_beside_its_title() -> None:
    text = lean(shop())
    lines = text.splitlines()
    assert lines[0] == "## Results"
    tile = next(line for line in lines if "990 PRO 1TB" in line)
    assert '[e4.3] link "Samsung 990 PRO 1TB NVMe SSD"' in tile and "₹ 6,500" in tile
    assert "4.5 out of 5 stars 2,345 ratings" in tile and '[e4.4] button "Add to cart"' in tile
    assert sum("990 PRO" in line for line in lines) == 3, "three cards, three lines - never one"


def test_the_tree_as_written_is_unchanged() -> None:
    """The proof check matches against the tree as written: one line per node, as before."""
    text = render(shop(), epoch=4, cap=12_000, password_backend_ids=set()).text
    assert 'StaticText "6,499"' in text and 'heading "Results"' in text


def test_a_list_item_and_a_row_are_one_line_each() -> None:
    nodes = [
        node("1", "RootWebArea", children=["l", "t"]),
        node("l", "list", children=["i1"]),
        node("i1", "listitem", children=["a", "s"]),
        node("a", "link", "Docs", backend=5),
        node("s", "StaticText", "- the manual"),
        node("t", "table", children=["r"]),
        node("r", "row", children=["c1", "c2"]),
        node("c1", "cell", "Alice", children=["c1s"]),
        node("c1s", "StaticText", "Alice"),
        node("c2", "cell", "42", children=["c2s"]),
        node("c2s", "StaticText", "42"),
    ]
    text = lean(nodes)
    assert '- [e4.1] link "Docs" - the manual' in text.splitlines()
    assert "Alice | 42" in text.splitlines()


def test_a_native_selects_choices_ride_on_its_line() -> None:
    options = [node(f"o{i}", "option", f"Choice {i}", backend=50 + i) for i in range(8)]
    nodes = [
        node("1", "RootWebArea", children=["sel"]),
        node("sel", "combobox", "Sort by", children=["pop"], backend=40),
        node("pop", "MenuListPopup", children=["wrap"]),
        node("wrap", "none", children=[o["nodeId"] for o in options]),
        *options,
    ]
    snap = render(nodes, epoch=4, cap=12_000, password_backend_ids=set(), lean=True)
    assert snap.text == (
        '[e4.1] combobox "Sort by" options: Choice 0 | Choice 1 | Choice 2 | Choice 3 | Choice 4 | … 3 more'
    )
    assert snap.refs == {"e4.1": 40}, "the select takes a value: its options need no references"


def test_only_a_modal_dialog_leads() -> None:
    def page(modal: bool) -> list[dict[str, Any]]:
        return [
            node("1", "RootWebArea", children=["t", "d"]),
            node("t", "StaticText", "Results"),
            node("d", "dialog", "Filters", children=["x"], props={"modal": modal}),
            node("x", "button", "Close", backend=9),
        ]

    assert lean(page(modal=True)).startswith('dialog "Filters"')
    assert lean(page(modal=False)).startswith("Results"), "a sidebar marked dialog is page content"


def test_what_is_on_the_screen_comes_first_and_the_rest_says_where_it_is() -> None:
    # Card 0 is below the screen, card 1 on it, card 2 above it (the page was scrolled).
    places = {1000: 1, 1010: 0, 1020: -1, 2: 0, 3: 0}
    lines = lean(shop(), screen=Screen(places)).splitlines()
    assert lines[0] == "## Results" and "1TB" in lines[1]
    assert lines[2] == "[1 more lines below the screen]" and "0TB" in lines[3]
    assert lines[4] == "[1 more lines above the screen]" and "2TB" in lines[5]


def test_the_screen_is_read_from_the_layout_boxes_against_the_viewport() -> None:
    dom = {
        "documents": [
            {
                "nodes": {"backendNodeId": [10, 11, 12, 13]},
                "layout": {
                    "nodeIndex": [0, 1, 2, 3],
                    "bounds": [[0, 100, 50, 20], [0, 1200, 50, 20], [0, 300, 50, 20], [2000, 600, 50, 20]],
                },
            }
        ]
    }
    metrics = {"cssVisualViewport": {"pageX": 0, "pageY": 400, "clientWidth": 1280, "clientHeight": 757}}
    screen = screen_of(dom, metrics)
    assert screen is not None
    assert screen.places == {10: -1, 11: 1, 12: -1, 13: 1}, "beside the screen counts as below it"
    assert screen_of({}, metrics) is None and screen_of(dom, {}) is None


def test_a_control_the_last_look_did_not_have_is_starred_and_its_reference_is_as_ever() -> None:
    first = shop(2)
    seen = interactive_ids(first)
    nodes = [*first, node("new", "option", "Goa, India", backend=77)]
    nodes[0]["childIds"].append("new")
    text = lean(nodes, previous=seen)
    assert '*[e4.5] option "Goa, India"' in text
    assert text.count("*") == 1, "only what is new is marked"
    assert "*" not in lean(nodes), "no previous look of this document, no marks"


def test_a_long_page_keeps_its_top_and_its_last_lines() -> None:
    texts = [
        node(str(i), "paragraph", f"paragraph {i} " + "x" * 80, children=[f"{i}s"]) for i in range(2, 202)
    ]
    nodes = [
        node("1", "RootWebArea", children=[*(str(i) for i in range(2, 202)), "go"]),
        *texts,
        *(node(f"{i}s", "StaticText", f"paragraph {i} " + "x" * 80) for i in range(2, 202)),
        node("go", "button", "Submit", backend=99),
    ]
    snap = render(nodes, epoch=4, cap=4_000, password_backend_ids=set(), lean=True)
    lines = snap.text.splitlines()
    assert len(snap.text) <= 4_000
    assert lines[0].startswith("paragraph 2 ") and lines[-1] == '[e4.1] button "Submit"'
    assert any(line.startswith("[truncated: ") for line in lines)
    assert snap.refs == {"e4.1": 99}


def test_a_page_saved_to_a_file_carries_no_references() -> None:
    snap = render(shop(), epoch=None, cap=400_000, password_backend_ids=set(), lean=True)
    assert "[e" not in snap.text and snap.refs == {}
    assert 'link "Samsung 990 PRO 0TB NVMe SSD"' in snap.text


def test_a_nameless_progressbar_with_only_a_value_is_kept() -> None:
    """An empty name is in every name, so a value-only progressbar, meter or status read as one
    "inside a link that says what the link says" and was dropped."""
    bar = node("bar", "progressbar", backend=9)
    bar["value"] = {"value": "40"}
    text = lean([node("1", "RootWebArea", "Upload", children=["bar"]), bar])
    assert "progressbar" in text and "40" in text
