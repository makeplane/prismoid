# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Accessibility tree -> snapshot text. One line per node, indented by depth. Only interactive
nodes carry a reference, `e{epoch}.{n}`, valid for exactly one epoch. The work renders before the
page's chrome, a long navigation folds, and the text is capped with a marker inside the cap; a
`find` narrows it to what matches.

The model's look is `lean`: what is on the screen first, then what is below it and above it,
each under a line that says so; a list item's, a row's or a small card's text on ONE line, so a
price sits beside its title; a heading as `## text`; a native select's choices inline; a `*` on
a control the previous look of the same document did not have, keyed by the browser's own node
ids, so references and approval fingerprints are untouched; and a cut that keeps the top four
fifths and the last fifth (a dialog's Done, a pager). The proof check's whole-page read is not
lean: it is the tree as written, which its matching reads (`render` with `lean=False`)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Final

INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
    }
)
SKIP_ROLES = frozenset({"generic", "none", "presentation", "InlineTextBox", "LineBreak", "RootWebArea"})
STATE_PROPS = ("required", "disabled", "checked", "expanded", "selected", "focused", "invalid")
# Where the work is, first. The tree is not visual order anyway, and in DOM order a long header
# or sidebar spent the cap before the form or table a task needs ever got a reference: an open
# dialog leads, the main content and forms follow, the page's chrome comes last.
_REGION_RANK: Final = {
    "dialog": 0,
    "alertdialog": 0,
    "main": 1,
    "form": 1,
    "navigation": 3,
    "banner": 3,
    "contentinfo": 3,
    "complementary": 3,
}
_UNMARKED_RANK: Final = 2
_DIALOGS: Final = frozenset({"dialog", "alertdialog"})
NAV_SHOWN: Final = 12  # links a navigation shows before the rest fold into one line
_REACH: Final = 'snapshot with find="<text>" to reach one'
# One line each when their text fits (`JOIN_MAX`): a list item, a table row or cell, a heading, a
# paragraph, and a card - the unnamed container a site builds a product tile from (`_Node.group`).
_ROWS: Final = frozenset({"row", "LayoutTableRow"})
_CELLS: Final = frozenset({"cell", "gridcell", "columnheader", "rowheader", "LayoutTableCell"})
BLOCK_ROLES: Final = frozenset({"listitem", "article", "paragraph", "LabelText", "heading", *_ROWS, *_CELLS})
_PREFIX: Final = {"listitem": "- ", "heading": "## "}
# What holds many items is never one line: its items are - a landmark, a table, and a list two
# of whose items hold a control (a menu, a row of links). A list of plain bullets inside a card
# (a hotel's amenities) is part of the card's line; a form is not a holder either (add-to-cart).
_HOLDERS: Final = frozenset(
    {*(region for region in _REGION_RANK if region != "form"), "table", "LayoutTable", "grid", "treegrid"}
)
_LISTS: Final = frozenset({"list", "tree", "menu", "menubar", "tablist", "listbox"})
_STRUCTURE: Final = BLOCK_ROLES | _HOLDERS | _LISTS  # kept as a group in the lean look, unnamed or not
JOIN_MAX: Final = (
    800  # a card's title, rating, price and delivery fit (measured on live shop pages); a section does not
)
OPTIONS_SHOWN: Final = 5
HEAD_SHARE: Final = 0.8  # over the cap, the top four fifths and the last fifth survive
_ON, _BELOW, _ABOVE = 0, 1, -1


@dataclass
class Snapshot:
    text: str
    refs: dict[str, int] = field(default_factory=dict[str, int])
    meta: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    truncated: int = 0
    # As written only, for the proof check: each kept node's own words in tree order with no markup
    # (`reading`), and its kept lines without the renderer's own (a fold, the truncation marker).
    reading: str = ""
    written: str = ""


@dataclass(frozen=True)
class Screen:
    """Where each laid-out node sits against the viewport, by its backend node id: `_ON`,
    `_BELOW` (or beside it, a carousel's hidden slide) or `_ABOVE`. A node with no box of its own
    (a text run's container, a node the snapshot has no layout for) sits where its parent does."""

    places: dict[int, int]


def screen_of(dom: dict[str, Any], metrics: dict[str, Any]) -> Screen | None:
    """`DOMSnapshot.captureSnapshot`'s boxes (document coordinates) against
    `Page.getLayoutMetrics`'s viewport; None when either says nothing. Only the main document:
    a frame's boxes are its own document's, and its nodes are not in the page's tree anyway."""
    view: dict[str, float] = metrics.get("cssVisualViewport") or {}
    docs: list[dict[str, Any]] = dom.get("documents") or []
    if not view or not docs:
        return None
    left, top = float(view.get("pageX", 0)), float(view.get("pageY", 0))
    right, bottom = left + float(view.get("clientWidth", 0)), top + float(view.get("clientHeight", 0))
    ids: list[int] = docs[0].get("nodes", {}).get("backendNodeId", [])
    layout: dict[str, list[Any]] = docs[0].get("layout", {})
    boxes: list[tuple[int, list[float]]] = list(
        zip(layout.get("nodeIndex", []), layout.get("bounds", []), strict=False)
    )
    places: dict[int, int] = {}
    for index, (x, y, w, h) in boxes:
        if w <= 0 or h <= 0 or index >= len(ids):
            continue
        place = _ABOVE if y + h <= top else _BELOW if y >= bottom or x >= right or x + w <= left else _ON
        if places.get(ids[index]) != _ON:  # a node with a box on the screen is on it
            places[ids[index]] = place
    return Screen(places)


def interactive_ids(nodes: list[dict[str, Any]]) -> set[int]:
    """Every control of a tree, by backend node id: what the next look marks `*` against."""
    return {
        bid for n in nodes if _val(n.get("role")) in INTERACTIVE_ROLES and (bid := _backend_id(n)) is not None
    }


def _val(d: dict[str, Any] | None) -> str:
    if not d:
        return ""
    v = d.get("value")
    return "" if v is None else str(v)


def _backend_id(n: dict[str, Any]) -> int | None:
    """Extract backendDOMNodeId, treating 0 as a real id (not a missing sentinel)."""
    bid = n.get("backendDOMNodeId")
    if bid is not None:
        return int(bid)
    return None


def _line(n: dict[str, Any], ref: str | None, hide_value: bool) -> str:
    role = _val(n.get("role"))
    name = _val(n.get("name"))
    parts = [f"[{ref}] {role}" if ref else role]
    if name:
        parts.append(f'"{name}"')
    value = _val(n.get("value"))
    if value and not hide_value:
        parts.append(f'value="{value}"')
    props: list[dict[str, Any]] = n.get("properties", []) or []
    for p in props:
        pname = p.get("name", "")
        if pname in STATE_PROPS and p.get("value", {}).get("value") not in (False, None, "false"):
            parts.append(pname)
    return " ".join(parts)


@dataclass
class _Node:
    """A node that makes it into the snapshot; the nodes the renderer skips hand their children
    up to the nearest one that does. `region` is the nearest landmark around it, itself included.
    A `note` is the renderer's own line (a folded navigation), never a page node. A `group` is a
    skipped container the lean look keeps (a card's unnamed div, an unnamed list or row: the tree
    as written has none); `many`, that it holds many items, so it is never one line (`_many`);
    `options`, a native select's choices."""

    ax: dict[str, Any]
    role: str
    name: str
    region: str = ""
    kids: list[_Node] = field(default_factory=lambda: [])
    note: str = ""
    group: bool = False
    options: tuple[str, ...] = ()
    many: bool = False


def _redundant(node: _Node, parent_name: str, next_sibling: _Node | None) -> bool:
    """Text that says nothing its own line's neighbours do not: Chrome names a link or a button
    from its text (`link "X" > StaticText "X"`), and a label's text is the name of the field
    right after it. A list bullet says nothing at all."""
    if node.role == "ListMarker":
        return True
    if node.role != "StaticText":
        return False
    return node.name in parent_name or (next_sibling is not None and node.name in next_sibling.name)


def _rank(node: _Node) -> int:
    """A group ranks as the dialog or the main content inside it, so a page's wrapper still puts
    its main first - never as a form inside it: a product card's add-to-cart form would lift the
    card's price above its title."""
    own = _REGION_RANK.get(node.region, _UNMARKED_RANK)
    if not node.group:
        return own
    return min([own, *(r for k in node.kids if k.region != "form" and (r := _rank(k)) < _UNMARKED_RANK)])


def _ordered(kids: list[_Node], parent_name: str) -> list[_Node]:
    kept = [
        kid
        for i, kid in enumerate(kids)
        if not _redundant(kid, parent_name, kids[i + 1] if i + 1 < len(kids) else None)
    ]
    return sorted(kept, key=_rank)


def _glyph(text: str) -> bool:
    """An icon font's character (a star, a tick): private-use code points, nothing to read."""
    return all(c.isspace() or 0xE000 <= ord(c) <= 0xF8FF for c in text)


def _many(node: _Node) -> _Node:
    """Marked on the whole tree, before the screen splits it: a grid with one card left on the
    screen still holds many."""
    listed = node.role in _LISTS and sum(map(_controls, node.kids)) > 1
    node.many = node.role in _HOLDERS or listed or _repeats(node.kids) or any(k.many for k in node.kids)
    return node


def _modal(n: dict[str, Any]) -> bool:
    """A dialog that holds the page: a site's filter sidebar marked `dialog` (non-modal, as on
    amazon.in) is page content, and leading with it would spend the cap before the results."""
    props: list[dict[str, Any]] = n.get("properties", []) or []
    return any(p.get("name") == "modal" and p.get("value", {}).get("value") is True for p in props)


def _options(
    nid: str, by_id: dict[str, dict[str, Any]], children_of: dict[str, list[str]]
) -> tuple[str, ...]:
    """A native select's choices: Chromium hangs them under its `MenuListPopup` child, some
    behind an ignored wrapper."""
    popups = [c for c in children_of.get(nid, []) if _val(by_id.get(c, {}).get("role")) == "MenuListPopup"]
    out: list[str] = []
    todo = list(reversed(popups))
    while todo:
        n = by_id.get(todo.pop(), {})
        if _val(n.get("role")) == "option":
            out.append(_val(n.get("name")))
        else:
            todo += reversed(n.get("childIds", []) or [])
    return tuple(out)


def _tree(nodes: list[dict[str, Any]], *, groups: bool = False) -> list[_Node]:
    by_id = {n["nodeId"]: n for n in nodes}
    children_of: dict[str, list[str]] = {n["nodeId"]: n.get("childIds", []) or [] for n in nodes}
    referenced = {c for kids in children_of.values() for c in kids}
    visited: set[str] = set()

    def collect(nid: str, region: str, said: str) -> list[_Node]:
        """`said`: the name of the nearest control around (lean only): Chromium names a control
        from its text, so that text says nothing twice; a cell or a heading named from its text
        keeps it, and its name is the one left out (`_covered`)."""
        if nid in visited:
            return []
        visited.add(nid)
        n = by_id.get(nid)
        if n is None:
            return []
        role = _val(n.get("role"))
        name = _val(n.get("name"))
        if role in _REGION_RANK and not (groups and role in _DIALOGS and not _modal(n)):
            region = role  # in the lean look a dialog leads only when it is modal (`_modal`)
        skip = (
            n.get("ignored")
            or role in SKIP_ROLES
            or (role == "StaticText" and not (name and (not groups or not _glyph(name))))
            or (not name and not _val(n.get("value")) and role not in INTERACTIVE_ROLES)
        )
        if role in INTERACTIVE_ROLES and not skip:
            said = name
        kids = [k for c in children_of.get(nid, []) for k in collect(c, region, said)]
        if not groups:
            return kids if skip else [_Node(n, role, name, region, _ordered(kids, name))]
        if skip and kids and (len(kids) > 1 or role in _STRUCTURE):
            kept = role if role in _STRUCTURE else ""  # an unnamed list or row is still one
            return [_many(_Node(n, kept, "", region, _ordered(kids, said), group=True))]
        if skip:
            return kids
        if role not in INTERACTIVE_ROLES and name and name in said and not kids:
            return []  # an image or a heading inside a link that says what the link says
        options = _options(nid, by_id, children_of) if role == "combobox" else ()
        return [_many(_Node(n, role, name, region, _ordered(kids, said), options=options))]

    roots = [n["nodeId"] for n in nodes if n["nodeId"] not in referenced]
    return _ordered([k for r in roots for k in collect(r, "", "")], "")


def _found(nodes: list[_Node], needle: str) -> list[_Node]:
    """The nodes whose name holds `needle`, each with everything inside it, and the lines that
    hold them for context - nothing else."""
    out: list[_Node] = []
    for n in nodes:
        if needle in n.name.casefold():
            out.append(n)
        elif inside := _found(n.kids, needle):
            out.append(dataclasses.replace(n, kids=inside))
    return out


def _folded(nodes: list[_Node]) -> list[_Node]:
    """A navigation with more than NAV_SHOWN links keeps its first NAV_SHOWN and says how many
    more there are; `find` reaches any of them."""
    out: list[_Node] = []
    for n in nodes:
        if n.role == "navigation":
            budget = [NAV_SHOWN]
            kids = _take(n.kids, budget)
            hidden = _links(n.kids) - _links(kids)
            if hidden:
                kids.append(
                    _Node({}, "", "", n.region, note=f"[{hidden} more links in this navigation - {_REACH}]")
                )
            out.append(dataclasses.replace(n, kids=kids))
        else:
            out.append(dataclasses.replace(n, kids=_folded(n.kids)))
    return out


def _take(nodes: list[_Node], budget: list[int]) -> list[_Node]:
    out: list[_Node] = []
    for n in nodes:
        if budget[0] <= 0:
            break
        if n.role in INTERACTIVE_ROLES:
            budget[0] -= 1
        out.append(dataclasses.replace(n, kids=_take(n.kids, budget)))
    return out


def _links(nodes: list[_Node]) -> int:
    return sum((n.role in INTERACTIVE_ROLES) + _links(n.kids) for n in nodes)


def _marker(truncated: int) -> str:
    return f"[truncated: {truncated} more nodes - {_REACH}]"


def _parted(nodes: list[_Node], screen: Screen, inherited: int) -> tuple[list[_Node], ...]:
    """The forest three ways: on the screen, below it, above it. A node is in the part where it
    sits, and also, holding only those children, in each other part one of its children is in -
    the context a line needs (the list a far item is in)."""
    out: tuple[list[_Node], ...] = ([], [], [])
    for n in nodes:
        bid = _backend_id(n.ax)
        place = screen.places.get(bid, inherited) if bid is not None else inherited
        parts = _parted(n.kids, screen, place)
        for part, forest, kids in zip((_ON, _BELOW, _ABOVE), out, parts, strict=True):
            if part == place or kids:
                forest.append(dataclasses.replace(n, kids=kids))
    return out


_Line = tuple[str, tuple[str, ...]]  # its text, the references on it


@dataclass
class _Writer:
    """The lean lines. A control keeps one reference however many lines show it (a list that
    starts on the screen and goes on below it)."""

    epoch: int | None  # None: no references at all (a page saved to a file)
    passwords: set[int]
    previous: set[int] | None  # the controls of the last look at this document; None: no marks
    lines: list[_Line] = field(default_factory=lambda: [])
    refs: dict[str, int] = field(default_factory=dict[str, int])
    meta: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    minted: dict[int, str] = field(default_factory=dict[int, str])

    def control(self, node: _Node) -> tuple[str, str | None]:
        bid = _backend_id(node.ax)
        ref = self.minted.get(bid) if bid is not None else None
        if ref is None and self.epoch is not None and node.role in INTERACTIVE_ROLES and bid is not None:
            ref = self.minted[bid] = f"e{self.epoch}.{len(self.refs) + 1}"
            self.refs[ref], self.meta[ref] = bid, (node.role, node.name)
        text = _line(node.ax, ref, hide_value=bid in self.passwords)
        if self.previous is not None and bid is not None and bid not in self.previous:
            text = "*" + text
        if node.options:
            more = len(node.options) - OPTIONS_SHOWN
            text += (
                " options: "
                + " | ".join(node.options[:OPTIONS_SHOWN])
                + (f" | … {more} more" if more > 0 else "")
            )
        return text, ref

    def own(self, node: _Node) -> tuple[str, str | None]:
        """A node's text on a line of its own."""
        if node.note:
            return node.note, None
        if node.role == "StaticText":
            return node.name, None
        if node.role == "heading":
            return f"## {node.name}", None
        if node.role in INTERACTIVE_ROLES:
            return self.control(node)
        if _covered(node):
            return node.role, None  # its text follows, one line each
        return _line(node.ax, None, hide_value=False), None

    def walk(self, nodes: list[_Node], depth: int) -> None:
        """Each node's line, a run of text on one line; a card, a row, an item on one line when
        it fits; a container that says nothing its parent does not, its children in its place."""
        text: list[str] = []
        for n in nodes:
            if n.role == "StaticText" and not n.kids:
                text.append(n.name)
                continue
            self._text(text, depth)
            if (one := self.joined(n)) is not None:
                self.lines.append(("  " * depth + one[0], one[1]))
            elif n.group:
                self.walk(n.kids, depth)
            else:
                line, ref = self.own(n)
                self.lines.append(("  " * depth + line, (ref,) if ref else ()))
                if not n.options:
                    self.walk(n.kids, depth + 1)
        self._text(text, depth)

    def _text(self, text: list[str], depth: int) -> None:
        if text:
            self.lines.append(("  " * depth + _joined_text(text), ()))
            text.clear()

    def joined(self, node: _Node) -> _Line | None:
        """A block or a card on one line, when it is one: nothing in it holds many items, it says
        two things or more (a block, one), and it fits `JOIN_MAX`. Measured before a reference
        is minted."""
        if not (node.group or node.role in BLOCK_ROLES) or node.many or node.role in _LISTS:
            return None
        cells = node.role in _ROWS and all(k.role in _CELLS for k in node.kids)
        groups = [[k] for k in node.kids] if cells else [node.kids]
        pieces = [_pieces(g) for g in groups]
        if node.role == "heading" and not pieces[0]:
            return None
        if sum(map(len, pieces)) < (2 if node.group else 1) or _length(pieces) > JOIN_MAX:
            return None
        refs: list[str] = []
        texts = [_joined_text([self._piece(p, refs) for p in part]) for part in pieces]
        line = " | ".join(t for t in texts if t) if cells else texts[0]
        heads = node.group and node.kids[0].role == "heading"  # a section's heading and its edit link
        return _PREFIX.get("heading" if heads else node.role, "") + line, tuple(refs)

    def _piece(self, piece: _Node | str, refs: list[str]) -> str:
        if isinstance(piece, str):
            return piece
        text, ref = self.control(piece)
        if ref:
            refs.append(ref)
        return text


def _shape(node: _Node) -> tuple[Any, ...]:
    return (node.role, tuple(_shape(k) for k in node.kids if k.role != "StaticText"))


def _controls(node: _Node) -> bool:
    return node.role in INTERACTIVE_ROLES or any(map(_controls, node.kids))


def _repeats(nodes: list[_Node]) -> bool:
    """Two children of one shape with a control in it: cards in a grid the page did not mark as a
    list, each its own line."""
    shapes = [_shape(n) for n in nodes if n.kids and _controls(n)]
    return len(shapes) != len(set(shapes))


def _said(nodes: list[_Node]) -> str:
    return " ".join(
        n.note or n.name if n.role in INTERACTIVE_ROLES or not n.kids else _said(n.kids) for n in nodes
    )


def _covered(node: _Node) -> bool:
    """A name its children's text already says (a cell, a heading named from its text)."""
    return bool(node.kids) and "".join(node.name.split()) in "".join(_said(node.kids).split())


def _pieces(nodes: list[_Node]) -> list[_Node | str]:
    """A block's contents in reading order: text as text, a control as itself (its line is made
    when the block is written, so its reference is minted only then)."""
    out: list[_Node | str] = []
    for n in nodes:
        if n.role in INTERACTIVE_ROLES:
            out.append(n)
        elif n.note or (n.name and not _covered(n)):
            out.append(n.note or n.name)
        if not n.options:
            out += _pieces(n.kids)
    return out


def _length(pieces: list[list[_Node | str]]) -> int:
    """The joined line's length, a control's reference counted at its widest."""
    return sum(
        len(p) + 1 if isinstance(p, str) else len(_line(p.ax, "e000.000", False)) + 2
        for part in pieces
        for p in part
    )


def _joined_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


def _cut(lines: list[_Line], cap: int) -> tuple[list[_Line], int]:
    """The lines that fit `cap` with the marker among them: the top four fifths and the last
    fifth, so a dialog's last button and a list's pager survive a long page."""
    if sum(len(text) + 1 for text, _ in lines) - 1 <= cap:
        return lines, 0
    room = cap - len(_marker(len(lines))) - 1
    head = _fit(lines, int(room * HEAD_SHARE))
    used = sum(len(text) + 1 for text, _ in lines[:head])
    tail = _fit(lines[head:][::-1], room - used)
    cut = len(lines) - head - tail
    return [*lines[:head], (_marker(cut), ()), *lines[len(lines) - tail :]], cut


def _fit(lines: list[_Line], room: int) -> int:
    used = 0
    for i, (text, _) in enumerate(lines):
        used += len(text) + 1
        if used > room:
            return i
    return len(lines)


def _lean(roots: list[_Node], writer: _Writer, screen: Screen | None, cap: int) -> Snapshot:
    if screen is None:
        writer.walk(roots, 0)
    else:
        on, below, above = _parted(roots, screen, _ON)
        writer.walk(on, 0)
        for forest, where in ((below, "below"), (above, "above")):
            start = len(writer.lines)
            writer.walk(forest, 0)
            if (n := len(writer.lines) - start) > 0:
                writer.lines.insert(start, (f"[{n} more lines {where} the screen]", ()))
    kept, truncated = _cut(writer.lines, cap)
    live = {ref for _, refs in kept for ref in refs}
    refs = {ref: bid for ref, bid in writer.refs.items() if ref in live}
    meta = {ref: m for ref, m in writer.meta.items() if ref in live}
    return Snapshot("\n".join(text for text, _ in kept), refs, meta, truncated)


def proof_read(snap: Snapshot, cap: int) -> dict[str, str]:
    """The proof check's read, all three fields within the one `cap`: the typed `reading` (at most
    half) and `written` first, since the proof decides on them, and the tree's `text` with what is left."""
    reading = snap.reading[: cap // 2]
    written = snap.written[: cap - len(reading)]
    return {"reading": reading, "written": written, "text": snap.text[: cap - len(reading) - len(written)]}


def render(
    nodes: list[dict[str, Any]],
    *,
    epoch: int | None,
    cap: int,
    password_backend_ids: set[int],
    find: str | None = None,
    lean: bool = False,
    screen: Screen | None = None,
    previous: set[int] | None = None,
) -> Snapshot:
    """The page as text. `lean` is the model's look (the module docstring), split by `screen`
    when there is one and marked against `previous`; without it, the tree as written, cut to
    its longest prefix. `epoch` None mints no references (lean only)."""
    roots = _tree(nodes, groups=lean)
    if find:
        roots = _found(roots, find.casefold())
        if not roots:
            return Snapshot(text=f'[nothing on this page has "{find}" in its name]')
    else:
        roots = _folded(roots)
    if lean:
        writer = _Writer(epoch, password_backend_ids, previous)
        return _lean(roots, writer, None if find else screen, cap)
    assert epoch is not None
    return _as_written(roots, epoch, cap, password_backend_ids)


def _as_written(roots: list[_Node], epoch: int, cap: int, password_backend_ids: set[int]) -> Snapshot:
    lines: list[tuple[str, str | None]] = []  # (text, its reference)
    words: list[str] = []  # each line's own words (`Snapshot.reading`); "" for the renderer's own
    owned: list[bool] = []  # whether each line is a node's own (`Snapshot.written`)
    refs: dict[str, int] = {}
    meta: dict[str, tuple[str, str]] = {}

    def walk(node: _Node, depth: int) -> None:
        if node.note:
            lines.append(("  " * depth + node.note, None))
            words.append("")
            owned.append(False)
            return
        ref = None
        bid = _backend_id(node.ax)
        if node.role in INTERACTIVE_ROLES and bid is not None:
            ref = f"e{epoch}.{len(refs) + 1}"
            refs[ref] = bid
            meta[ref] = (node.role, node.name)
        hide = bid in password_backend_ids if bid is not None else False
        lines.append(("  " * depth + _line(node.ax, ref, hide_value=hide), ref))
        words.append(" ".join(w for w in (node.name, "" if hide else _val(node.ax.get("value"))) if w))
        owned.append(True)
        for kid in node.kids:
            walk(kid, depth + 1)

    for root in roots:
        walk(root, 0)

    # The cap holds the marker too: keep the longest prefix that fits beside it, and drop the
    # references of every line that did not make it.
    kept, used = len(lines), sum(len(text) + 1 for text, _ in lines) - 1
    if used > cap:
        room = cap - len(_marker(len(lines))) - 1
        kept, used = 0, 0
        while kept < len(lines) and used + len(lines[kept][0]) + 1 <= room:
            used += len(lines[kept][0]) + 1
            kept += 1
        for _, ref in lines[kept:]:
            if ref is not None:
                refs.pop(ref)
                meta.pop(ref)
    truncated = len(lines) - kept
    text = [t for t, _ in lines[:kept]] + ([_marker(truncated)] if truncated else [])
    reading = " ".join(w for w in words[:kept] if w)
    written = "\n".join(t for (t, _), own in zip(lines[:kept], owned[:kept], strict=True) if own)
    return Snapshot(
        text="\n".join(text), refs=refs, meta=meta, truncated=truncated, reading=reading, written=written
    )
