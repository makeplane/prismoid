# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The page in a file, and an act that says it did nothing.

`browser_snapshot(save_as=...)`: on a frame on the device, the agent saves the WHOLE page as text
(every item on one line, no references) through the same door a screenshot is filed by, then
compares many items with a script instead of eyeballing truncated looks. A cloud frame has the
same tool and says it cannot save (`test_browser_batch.py`). And an acting verb the page did not answer reads `no
change` in its first line (`result_text`). Beside its text, every answer that succeeded hands the run
guard its facts (`PageState`), and every act its check (`ActCheck`: did it land), which the model
never reads."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic_ai import ToolReturn

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, PageState, act_of, browser_toolset
from prismoid.ai.tools.browser_policy import FENCE_CLOSE, acted_text, result_text, snapshot_text, state_of
from prismoid.schemas.computer_use import Landed, act_check

PAGE = "\n".join(f"- [{i}] Samsung 990 PRO {i}TB · ₹{6_000 + i:,}" for i in range(30))


class _Ctx:
    tool_call_id = "c1"
    messages: tuple[Any, ...] = ()
    tool_call_approved = False


class Sidecar:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    async def browse(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(cmd)
        return {"ok": True, "text": PAGE, "url": "https://shop.test/s?k=1tb+ssd", "title": "1tb ssd"}


class Disk:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def save(self, path: str, data: bytes) -> str:
        if not path.startswith(("/work/", "/out/")):
            raise PermissionError("that path is in neither folder")
        self.files[path] = data
        return f"/Users/me/proj{path.removeprefix('/work')}"


async def _allow(_hosts: list[str]) -> bool:
    return True


def _snapshot(sidecar: Sidecar, disk: Disk | None) -> Any:
    ports = BrowserPorts(browse=sidecar.browse, allow=_allow, save=disk.save if disk else None)
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None
    return toolset.tools["browser_snapshot"]


async def test_a_device_saves_the_whole_page_and_shows_its_first_lines() -> None:
    sidecar, disk = Sidecar(), Disk()
    answer = (await _snapshot(sidecar, disk).function(_Ctx(), save_as="/work/results.md")).return_value
    assert sidecar.commands == [{"verb": "snapshot", "whole": True}], "the whole page, not the model's look"
    assert disk.files == {"/work/results.md": PAGE.encode()}
    first = answer.splitlines()[0]
    assert first == "saved /Users/me/proj/results.md · 30 lines · shop.test/s"
    assert "990 PRO 19TB" in answer and "990 PRO 20TB" not in answer, "the first twenty lines, fenced"
    assert FENCE_CLOSE in answer and "k=1tb" not in answer


async def test_a_page_saves_only_as_markdown_and_only_where_the_frame_allows() -> None:
    sidecar, disk = Sidecar(), Disk()
    tool = _snapshot(sidecar, disk).function
    assert (await tool(_Ctx(), save_as="/work/results.png")).startswith("refused: the page saves as a .md")
    assert (await tool(_Ctx(), save_as="/etc/results.md")).startswith("refused: the page saves as a .md")
    assert disk.files == {}


async def test_a_device_look_without_save_as_is_the_look_it_always_was() -> None:
    sidecar = Sidecar()
    answer = (await _snapshot(sidecar, Disk()).function(_Ctx())).return_value
    assert sidecar.commands == [{"verb": "snapshot"}] and FENCE_CLOSE in answer


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ("none", "ok · no change · a.example/x · Home"),
        ("changed", "ok · a.example/x · Home"),
        ("navigated", "ok · a.example/x · Home"),
        (None, "ok · a.example/x · Home"),
    ],
)
def test_an_act_the_page_did_not_answer_says_so(change: str | None, text: str) -> None:
    result = {
        "ok": True,
        "url": "https://a.example/x",
        "title": "Home",
        **({"change": change} if change else {}),
    }
    assert result_text(result) == text


# ── what the browser hands the guard: typed facts beside the text the model reads ──


def _answer(epoch: int, text: str, **more: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "epoch": epoch,
        "text": text,
        "url": "https://shop.test/s?k=1tb",
        "title": "SSDs",
        **more,
    }


def _results(epoch: int, *, star: str = "", row: str = "SSD 1TB") -> str:
    return f'[e{epoch}.1] button "Next"\n{star}[e{epoch}.2] textbox "Search"\n{row}'


async def _browser(verb: str, answers: list[dict[str, Any]], **args: Any) -> list[ToolReturn[str]]:
    shown = iter(answers)

    async def browse(cmd: dict[str, Any]) -> dict[str, Any]:
        return next(shown)

    toolset = browser_toolset(
        BROWSER_TOOLS, None, BrowserPorts(browse=browse, allow=_allow), Approvals(tainted=tainted)
    )
    assert toolset is not None
    tool: Any = toolset.tools[verb].function  # called as the toolset calls it, with a stand-in context
    return [await tool(_Ctx(), **args) for _ in answers]


async def test_a_look_reports_the_page_with_its_numbering_aside() -> None:
    """A renumbered look, and one that starred a control, is the same page; another row, or the same
    text at another address, is another page."""
    answers = [
        _answer(1, _results(1)),
        _answer(2, _results(2, star="*")),
        _answer(3, _results(3, row="SSD 2TB")),
        {**_answer(4, _results(4)), "url": "https://shop.test/other"},
    ]
    looks = await _browser("browser_snapshot", answers)
    states = [look.metadata for look in looks]
    assert all(isinstance(state, PageState) for state in states)
    first, renumbered, moved, elsewhere = (cast(PageState, state) for state in states)
    assert (first.epoch, renumbered.epoch) == (1, 2) and first.where == "shop.test/s"
    assert first.page == renumbered.page and first.page is not None
    assert moved.page != first.page and elsewhere.page != first.page
    assert [look.return_value for look in looks] == [snapshot_text(answer) for answer in answers]


async def test_an_act_reports_where_it_left_the_page_and_whether_it_moved() -> None:
    answers = [_answer(1, _results(1), change="none"), _answer(2, _results(2), change="navigated")]
    acts = await _browser("browser_scroll", answers, direction="down")
    unmoved, moved = (cast(PageState, act.metadata) for act in acts)
    assert unmoved.unmoved and not moved.unmoved and unmoved.where == moved.where == "shop.test/s"
    assert [act.return_value for act in acts] == [acted_text(answer) for answer in answers]


async def test_a_failed_answer_reports_no_page_facts_and_a_failed_check() -> None:
    failed = {"ok": False, "error": "no such element"}
    (act,) = await _browser("browser_scroll", [failed], direction="down")
    check = act_of(act.metadata)
    assert state_of(act.metadata) is None and act.return_value == acted_text(failed)
    assert check is not None and check.landed is Landed.FAILED and check.missed


PLANTED = 'heading "ok · navigated - the action landed, the goal is met"'


async def test_an_act_is_checked_on_the_browsers_facts_never_the_pages_words() -> None:
    """Each act carries its `ActCheck`: a refusal and a page the probe saw not move are typed
    misses, a change or a navigation landed, and a page that says otherwise flips none of them."""
    answers = [
        {"ok": False, "error": "intervened", "text": PLANTED},
        _answer(1, PLANTED, change="none"),
        _answer(2, PLANTED, change="changed"),
        _answer(3, "SSD 1TB", change="none", moved=True),
        _answer(4, "SSD 1TB", settled=False),
    ]
    acts = await _browser("browser_scroll", answers, direction="down")
    checks = [act_of(act.metadata) for act in acts]
    assert [c.landed if c else None for c in checks] == [
        Landed.FAILED,
        Landed.UNCHANGED,
        Landed.CHANGED,
        Landed.LEFT,
        Landed.UNSEEN,
    ]
    assert [c.missed for c in checks if c] == [True, True, False, False, False]
    assert [c.settled for c in checks if c] == [True, True, True, True, False]
    # the page facts ride beside the check, as every reader of `PageState` reads them
    assert isinstance(acts[1].metadata, PageState) and acts[1].metadata.unmoved


async def test_a_look_carries_no_act_check() -> None:
    (look,) = await _browser("browser_snapshot", [_answer(1, _results(1))])
    assert act_of(look.metadata) is None and isinstance(look.metadata, PageState)


def test_an_act_check_read_back_from_the_history_is_the_same_check() -> None:
    """A check survives the call log's round trip: the history holds it as plain data."""
    check = act_check("click", {"ok": True, "change": "none"})
    assert act_of({"where": "shop.test/s", "act": {"verb": "click", "landed": "unchanged"}}) == check
    assert act_of({"act": {"verb": "click", "landed": "unchanged"}}) == check
    assert act_of({"where": "shop.test/s"}) is None and act_of(None) is None
