# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The proof check: the system looks for itself before a `done` counts -
one page read, one directory listing, the manifest. No model call, and it never raises."""

from __future__ import annotations

import shlex
from typing import Any, Literal

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STANDARD_PATHS, STEP_MARKER
from prismoid.ai.tools.browser_policy import PageState
from prismoid.operations.execution.proof import (
    NO_PROOF,
    PAGE_READ,
    URL_ONLY,
    check_proof,
    quotes_service,
    quotes_shell,
    unproven,
    unsaved_edit,
)
from prismoid.schemas.outputs import Outcome, Proof

SITE = "https://admin.example.com/users"


class _Frame:
    """A frame's files. `fresh`: the ones this step wrote (newer than the step marker); None is a
    frame with no marker (a throwaway frame, a device), where `find` cannot say."""

    paths = STANDARD_PATHS

    def __init__(self, *files: str, fresh: tuple[str, ...] | None = None) -> None:
        self.files, self.fresh = files, fresh
        self.ran: list[str] = []

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix) :].split("/")[0] for p in self.files if p.startswith(prefix)})

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        self.ran.append(command)
        if self.fresh is None:
            return ExecResult(1, "", "")
        words = shlex.split(command)
        target = words[words.index("find") + 1]
        hit = next((f for f in self.fresh if f == target or f.startswith(f"{target}/")), "")
        return ExecResult(0, f"{hit}\n" if hit else "", "")


def _page(
    text: str, url: str = SITE, ok: bool = True, *, reading: str = "", written: str | None = None
) -> Any:
    """The proof's read of a page: the tree `text`, its own lines as `written` (all of `text`
    unless said), and its words in `reading`, as the sidecar types them."""
    seen: list[dict[str, Any]] = []

    async def browse(command: dict[str, Any]) -> dict[str, Any]:
        seen.append(command)
        if not ok:
            return {"ok": False, "error": "no page"}
        lines = text if written is None else written
        return {"ok": True, "text": text, "written": lines, "reading": reading, "url": url, "title": "Users"}

    browse.seen = seen  # pyright: ignore[reportFunctionMemberAccess]
    return browse


def _done(**proof: str) -> Outcome:
    return Outcome(status="done", summary="Saved Mary's record.", proof=Proof(**proof) if proof else None)


def _shell(output: str) -> list[ModelMessage]:
    return [ModelRequest(parts=[ToolReturnPart("shell", output, tool_call_id="c1")])]


async def test_a_phrase_on_the_current_page_holds_whatever_its_case_and_spacing() -> None:
    browse = _page('heading "Users"\nStatus:\n   Open')
    assert (
        await check_proof(
            _done(url=SITE, page_text="status: open"), frame=_Frame(), browser=browse, published=()
        )
        is None
    )
    assert browse.seen == [{"verb": "snapshot", "cap": PAGE_READ}], (
        "one read of the whole page, through the frame's own verb"
    )


async def test_a_phrase_missing_from_the_page_fails_without_quoting_the_page() -> None:
    failed = await check_proof(
        _done(url=SITE, page_text="Record saved"),
        frame=_Frame(),
        browser=_page("secret token 123"),
        published=(),
    )
    assert failed == "'Record saved' is not on the page now"
    assert "secret" not in failed


async def test_the_browser_on_another_host_fails() -> None:
    failed = await check_proof(
        _done(url="https://plane.so/x", page_text="Users"),
        frame=_Frame(),
        browser=_page("Users"),
        published=(),
    )
    assert failed == "the browser is on admin.example.com, not plane.so"


async def test_a_page_that_cannot_be_read_is_a_failed_proof_never_a_crash() -> None:
    assert await check_proof(_done(page_text="xyz"), frame=_Frame(), browser=None, published=()) == (
        "this step has no browser to look at the page with"
    )
    unread = await check_proof(
        _done(page_text="xyz"), frame=_Frame(), browser=_page("", ok=False), published=()
    )
    assert unread == "the page could not be read (no page)"

    async def broken(command: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("sidecar gone")

    assert await check_proof(_done(page_text="xyz"), frame=_Frame(), browser=broken, published=()) == (
        "the page could not be read (RuntimeError)"
    )


async def test_a_work_path_must_exist_and_stay_under_work() -> None:
    frame = _Frame("/work/hello.txt", "/work/site/index.html")
    assert await check_proof(_done(path="/work/hello.txt"), frame=frame, browser=None, published=()) is None
    assert await check_proof(_done(path="/work/site"), frame=frame, browser=None, published=()) is None
    assert await check_proof(_done(path="/work/nope.txt"), frame=frame, browser=None, published=()) == (
        "/work/nope.txt does not exist"
    )
    assert await check_proof(_done(path="/work/../etc/passwd"), frame=frame, browser=None, published=()) == (
        "/work/../etc/passwd is not a path inside /work"
    )


async def test_an_artifact_must_be_in_the_manifest() -> None:
    published = ["/artifacts/stories.csv"]
    for named in ("/work/stories.csv", "stories.csv", "/artifacts/stories.csv"):
        assert (
            await check_proof(_done(artifact=named), frame=_Frame(), browser=None, published=published)
            is None
        )
    assert await check_proof(
        _done(artifact="other.csv"), frame=_Frame(), browser=None, published=published
    ) == ("other.csv was not published by this step")


@pytest.mark.parametrize(
    ("published", "messages", "holds"),
    [
        (["/artifacts/a.md"], [], True),
        ([], _shell("hello"), True),
        ([], _shell("(exit 0, no output)"), False),
        ([], _shell("ok"), False),
        ([], [], False),
    ],
)
async def test_a_done_without_proof_holds_only_on_a_publish_or_quoted_output(
    published: list[str], messages: list[ModelMessage], holds: bool
) -> None:
    outcome = Outcome(status="done", summary="The file says hello, ok.")
    failed = await check_proof(outcome, frame=_Frame(), browser=None, published=published, messages=messages)
    assert (failed is None) is holds
    if not holds:
        assert failed == NO_PROOF


def test_quoted_output_is_read_line_by_line() -> None:
    assert quotes_shell("It printed: Hello World", _shell("noise line\nhello   world"))
    assert not quotes_shell("It printed something", _shell("hello world"))


def _connector(
    content: object, *, tool: str = "notion_create_page", outcome: Literal["success", "retried"] = "success"
) -> list[ModelMessage]:
    return [ModelRequest(parts=[ToolReturnPart(tool, content, tool_call_id="c1", outcome=outcome)])]


PAGE = '{"id": "1f2e9a7c", "url": "https://notion.so/Connector-Test-1f2e9a7c", "object": "page"}'


async def test_a_done_on_a_connector_holds_when_it_quotes_what_the_service_returned() -> None:
    """A connector act has no page to read: the service's own id or link, quoted as its tool returned it, proves it."""
    said = Outcome(status="done", summary="Created Connector Test: https://notion.so/Connector-Test-1f2e9a7c")
    assert (
        await check_proof(said, frame=_Frame(), browser=None, published=(), messages=_connector(PAGE)) is None
    )
    vague = Outcome(status="done", summary="Created the page in Notion.")
    assert (
        await check_proof(vague, frame=_Frame(), browser=None, published=(), messages=_connector(PAGE))
        == NO_PROOF
    )


async def test_a_page_phrase_on_a_step_with_no_browser_falls_back_to_the_services_read_back() -> None:
    """A connector step runs with no browser: a `page_text` it offers cannot be looked at, so the
    service's own value quoted in the summary still proves it (seen live)."""
    proof = Proof(url="https://notion.so/Connector-Test-1f2e9a7c", page_text="Connector Test")
    said = Outcome(status="done", summary="Created https://notion.so/Connector-Test-1f2e9a7c", proof=proof)
    assert (
        await check_proof(said, frame=_Frame(), browser=None, published=(), messages=_connector(PAGE)) is None
    )
    vague = Outcome(status="done", summary="Created the page in Notion.", proof=proof)
    assert await check_proof(
        vague, frame=_Frame(), browser=None, published=(), messages=_connector(PAGE)
    ) == ("this step has no browser to look at the page with")


def test_only_a_connector_call_that_ran_is_quoted() -> None:
    said = "Created page 1f2e9a7c"
    assert quotes_service(said, _connector(PAGE))
    assert quotes_service(said, _connector({"result": {"ids": ["1f2e9a7c"]}}))
    assert not quotes_service(said, _connector(PAGE, outcome="retried")), "a retried call never ran"
    assert not quotes_service(said, _connector(PAGE, tool="web_fetch")), "a built-in tool is not a service"
    assert not quotes_service("the page", _connector('{"object": "page"}')), "a short value proves nothing"


def test_an_unproven_done_settles_partial_with_the_reason() -> None:
    settled = unproven(Outcome(status="done", summary="x" * 2000), "'Saved' is not on the page now")
    assert settled.status == "partial"
    assert settled.summary.endswith("Not confirmed: 'Saved' is not on the page now.")
    assert len(settled.summary) <= 2000


async def test_blank_proof_fields_are_absent_not_vacuous() -> None:
    for blank in (
        _done(page_text=" "),
        _done(url="  "),
        _done(url=" ", page_text="  ", path=" ", artifact=" "),
    ):
        assert await check_proof(blank, frame=_Frame(), browser=_page("anything"), published=()) == NO_PROOF


async def test_a_url_alone_is_not_proof() -> None:
    """...and the reason says what would be: a lookup's `done` that names only the page it read is
    told to quote a line from it, so its closing turn can prove the read instead of settling
    `partial` on a reason written for a change."""
    browse = _page("Users")
    assert await check_proof(_done(url=SITE), frame=_Frame(), browser=browse, published=()) == URL_ONLY
    assert "page_text" in URL_ONLY
    assert browse.seen == []
    held = await check_proof(
        _done(url="https://x.io", path="/work/a"), frame=_Frame("/work/a"), browser=None, published=()
    )
    assert held is None


async def test_a_phrase_too_short_to_prove_anything_fails() -> None:
    assert await check_proof(
        _done(url=SITE, page_text="a"), frame=_Frame(), browser=_page("a b c"), published=()
    ) == ("the phrase is too short to prove anything")


def test_the_reason_for_no_proof_fits_a_read_as_well_as_a_change() -> None:
    """A step that only looked something up has nothing that "took effect": the reason it is shown
    names the proof a read can give."""
    assert "read" in NO_PROOF and "took effect" not in NO_PROOF


async def test_a_path_this_step_never_touched_is_not_proof() -> None:
    """A /work file an earlier step (or turn) left on the Machine proves nothing about this one."""
    frame = _Frame("/work/report.md", fresh=())
    failed = await check_proof(_done(path="/work/report.md"), frame=frame, browser=None, published=())
    assert failed is not None
    assert "/work/report.md was already there" in failed


async def test_a_path_this_step_wrote_or_wrote_inside_holds() -> None:
    frame = _Frame(
        "/work/report.md", "/work/site/index.html", fresh=("/work/report.md", "/work/site/index.html")
    )
    assert await check_proof(_done(path="/work/report.md"), frame=frame, browser=None, published=()) is None
    assert await check_proof(_done(path="/work/site"), frame=frame, browser=None, published=()) is None
    assert f"-cnewer {STEP_MARKER}" in frame.ran[0], "ctime: a copy that kept its old mtime is still new"


async def test_an_untouched_path_falls_back_to_what_a_done_without_proof_stands_on() -> None:
    """A step that only READ a file it was asked about (`cat /work/notes.txt` -> "gamma") is honest:
    the quoted output holds it, as it would with no proof at all - and so does a publish."""
    frame = _Frame("/work/notes.txt", fresh=())
    read = Outcome(status="done", summary="The last line is gamma.", proof=Proof(path="/work/notes.txt"))
    assert (
        await check_proof(read, frame=frame, browser=None, published=(), messages=_shell("alpha\ngamma"))
        is None
    )
    assert (
        await check_proof(
            _done(path="/work/notes.txt"), frame=frame, browser=None, published=["/artifacts/a.md"]
        )
        is None
    )
    held = await check_proof(
        _done(url=SITE, page_text="Status: Open", path="/work/notes.txt"),
        frame=frame,
        browser=_page("Status: Open"),
        published=(),
    )
    assert held is None, "a phrase that holds is proof enough; the untouched path is simply not counted"


async def test_a_frame_that_cannot_say_keeps_the_existence_check() -> None:
    """No marker (a throwaway frame, a device frame) or no answer: never a rejection on that alone."""
    frame = _Frame("/work/hello.txt")
    assert await check_proof(_done(path="/work/hello.txt"), frame=frame, browser=None, published=()) is None


async def test_the_path_reaches_find_quoted() -> None:
    frame = _Frame("/work/a b; rm -rf x", fresh=("/work/a b; rm -rf x",))
    assert (
        await check_proof(_done(path="/work/a b; rm -rf x"), frame=frame, browser=None, published=()) is None
    )
    assert "'/work/a b; rm -rf x'" in frame.ran[0]


def _acts(*calls: tuple[str, dict[str, Any] | str, str]) -> list[ModelMessage]:
    """The run's browser calls, in order: (tool, args, what it returned). A call that answered `OK`
    carries the facts of one that landed; any other answer carries none, as a refusal does."""
    out: list[ModelMessage] = []
    for i, (tool, args, returned) in enumerate(calls):
        facts = LANDED if returned is OK else None
        out.append(ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id=f"a{i}")]))
        out.append(ModelRequest(parts=[ToolReturnPart(tool, returned, tool_call_id=f"a{i}", metadata=facts)]))
    return out


RECORDS = 'combobox "Status for Grace Hopper" value="closed"\nbutton "Save"'
OK = "ok · admin.example.com/records.html · Records"
LANDED = PageState(where="admin.example.com/records.html")
SELECT = ("browser_select", {"ref": "e1.2", "value": "closed"}, OK)
SAVE = ("browser_click", {"ref": "e1.3"}, OK)
CLOSED = {"url": SITE, "page_text": 'Status for Grace Hopper" value="closed'}


async def test_a_dropdown_changed_and_never_saved_is_not_proof() -> None:
    """Pick "closed", never click Save - the page shows the field's value either way."""
    failed = await check_proof(
        _done(**CLOSED), frame=_Frame(), browser=_page(RECORDS), published=(), messages=_acts(SELECT)
    )
    assert failed is not None
    assert "nothing saved it" in failed


async def test_a_field_saved_after_its_last_change_holds() -> None:
    """close-a-record's honest path, and every way a change gets submitted."""
    for acts in (
        _acts(SELECT, SAVE),
        _acts(("browser_type", {"ref": "e1.1", "text": "closed", "submit": True}, OK)),
        _acts(("browser_fill", '{"fields": [{"ref": "e1.1", "value": "closed"}], "submit": true}', OK)),
        _acts(SELECT, ("browser_press", {"key": "Enter"}, OK)),
        _acts(SELECT, ("browser_handoff", {"reason": "a person saves it"}, "handed back")),
    ):
        assert (
            await check_proof(
                _done(**CLOSED), frame=_Frame(), browser=_page(RECORDS), published=(), messages=acts
            )
            is None
        ), acts


async def test_a_change_the_rule_cannot_see_is_never_a_rejection() -> None:
    """A lookup (no field changed), a change that failed, a form left for another page, and a done
    that rests on another proof kind are all judged as before."""
    for acts in (
        _acts(("browser_open", {"url": SITE}, OK), ("browser_click", {"ref": "e1.4"}, OK)),
        _acts(("browser_select", {"ref": "e1.2", "value": "closed"}, "failed: no such option")),
        _acts(SELECT, ("browser_open", {"url": SITE}, OK)),
        _acts(SELECT, ("browser_snapshot", {}, "page"), SAVE, ("browser_scroll", {}, OK)),
    ):
        assert (
            await check_proof(
                _done(**CLOSED), frame=_Frame(), browser=_page(RECORDS), published=(), messages=acts
            )
            is None
        ), acts
    with_file = _done(**CLOSED, artifact="records.csv")
    assert (
        await check_proof(
            with_file,
            frame=_Frame(),
            browser=_page(RECORDS),
            published=["/artifacts/records.csv"],
            messages=_acts(SELECT),
        )
        is None
    )


@pytest.mark.parametrize("named", ["/work", "/work/", "/etc/hostname", "/work/../etc/passwd"])
async def test_a_path_outside_work_is_no_proof_and_sinks_nothing(named: str) -> None:
    """Computer book `run-a-version-check`: an agent that runs `python3 --version`, quotes
    `Python 3.12.16` and names `/work` itself as its path must not settle partial on "/work is not
    under /work". A path that cannot prove anything is not counted; the quoted output still holds."""
    outcome = Outcome(status="done", summary="It prints Python 3.12.16.", proof=Proof(path=named))
    frame = _Frame("/work/x", fresh=())
    held = await check_proof(
        outcome, frame=frame, browser=None, published=(), messages=_shell("Python 3.12.16")
    )
    assert held is None
    failed = await check_proof(outcome, frame=frame, browser=None, published=())
    assert failed == f"{named} is not a path inside /work"


# Wikipedia's tree puts each link on its own line, so a sentence a reader sees
# whole is split across `StaticText` and `link` lines, quoted and prefixed with markup.
ROVER = """heading "History"
paragraph
  StaticText "The most famous of the rear-drive safeties introduced in 1885 was the"
  [e3.41] link "Rover"
  StaticText ", produced in"
  [e3.42] link "Coventry"
  StaticText "by"
  [e3.43] link "John Kemp Starley"
  StaticText ". Starley’s design — chain-driven — sold well."
[truncated: 812 more nodes - snapshot with find="<text>" to reach one]"""
WIKI = "https://en.wikipedia.org/wiki/Safety_bicycle"
ROVER_WRITTEN = ROVER.rpartition("\n")[0]
ROVER_READING = (
    "History The most famous of the rear-drive safeties introduced in 1885 was the Rover "
    ", produced in Coventry by John Kemp Starley . Starley’s design — chain-driven — sold well."
)


async def test_a_quote_split_by_links_matches_as_a_reader_sees_it() -> None:
    quote = "The most famous of the rear-drive safeties introduced in 1885 was the Rover, produced in Coventry by John Kemp Starley."
    for phrase in (quote, "Starley's design - chain-driven - sold well", "“Rover, produced in Coventry”"):
        held = await check_proof(
            _done(url=WIKI, page_text=phrase),
            frame=_Frame(),
            browser=_page(ROVER, WIKI, reading=ROVER_READING, written=ROVER_WRITTEN),
            published=(),
        )
        assert held is None, phrase


async def test_a_phrase_must_still_be_contiguous_in_reading_order() -> None:
    for phrase in (
        "Rover, produced by John Kemp Starley",
        "Coventry produced in",
        "History The most famous was",
    ):
        failed = await check_proof(
            _done(url=WIKI, page_text=phrase),
            frame=_Frame(),
            browser=_page(ROVER, WIKI, reading=ROVER_READING, written=ROVER_WRITTEN),
            published=(),
        )
        assert failed == f"{phrase!r} is not on the page now", phrase
    marker = "snapshot with find"
    assert (
        await check_proof(
            _done(url=WIKI, page_text=marker),
            frame=_Frame(),
            browser=_page(ROVER, WIKI, reading=ROVER_READING, written=ROVER_WRITTEN),
            published=(),
        )
        is not None
    )


async def test_the_renderer_s_own_lines_and_markup_are_never_the_page() -> None:
    """Only the read's typed page counts: the tree's text holds the truncation marker and a fold the
    renderer wrote, which no page said, so quoting them proves nothing."""
    tree = 'heading "Users"\n[3 more items]\n[truncated: 9 more nodes]'
    browse = _page(tree, written='heading "Users"', reading="Users")
    for phrase in ("3 more items", "truncated: 9 more nodes"):
        failed = await check_proof(
            _done(url=SITE, page_text=phrase), frame=_Frame(), browser=browse, published=()
        )
        assert failed == f"{phrase!r} is not on the page now", phrase
    held = await check_proof(
        _done(url=SITE, page_text='heading "Users"'), frame=_Frame(), browser=browse, published=()
    )
    assert held is None


def test_an_edit_lands_on_its_facts_never_its_words() -> None:
    """Whether a field change landed is the fact its answer carried: an answer that only SAYS `ok`
    (no facts: a refusal, an older history) changed nothing, and one that landed counts whatever
    its words."""
    select = ("browser_select", {"ref": "e1.2", "value": "closed"})
    said_only = [
        ModelResponse(parts=[ToolCallPart(*select, tool_call_id="s")]),
        ModelRequest(parts=[ToolReturnPart("browser_select", OK, tool_call_id="s")]),
    ]
    assert not unsaved_edit(said_only)
    landed = [
        ModelResponse(parts=[ToolCallPart(*select, tool_call_id="s")]),
        ModelRequest(
            parts=[ToolReturnPart("browser_select", "failed: x", tool_call_id="s", metadata=LANDED)]
        ),
    ]
    assert unsaved_edit(landed)
