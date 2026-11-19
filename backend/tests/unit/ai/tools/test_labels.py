# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ONE redaction policy (`ai/tools/labels.py`) - locators reach the wire, content never.

The parity pin (`test_the_live_label_is_the_journal_summary`) is the law made a test: the
journal's `summary` and the live `activity.label` derive from the same primitives, so the
two surfaces cannot drift.
"""

from __future__ import annotations

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame
from prismoid.ai.frames.sandbar_driver import SandbarFrame
from prismoid.ai.tools import catalog, labels
from prismoid.ai.tools.browser import BROWSER_TOOLS, TOOL_NAMES
from prismoid.ai.tools.frame_tools import frame_toolset
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.runners import run_recorded


def test_paths_and_commands_are_locators() -> None:
    assert labels.label_for("read_file", {"path": "/task/inputs/brief.md"}) == "/task/inputs/brief.md"
    assert labels.label_for("write_file", {"path": "/work/memo.md", "content": "SECRET"}) == "/work/memo.md"
    assert labels.label_for("shell", {"command": "python3 /work/build.py"}) == "python3 /work/build.py"
    assert labels.label_for("publish", {"type": "document", "path": "/work/memo.md"}) == (
        "document:/work/memo.md"
    )


def test_content_fields_never_leak() -> None:
    label = labels.label_for("write_file", {"path": "/w.md", "content": "the secret body"})
    assert "secret" not in label


def test_commands_clamp_at_eighty() -> None:
    assert len(labels.command_label("x" * 500)) == labels.COMMAND_MAX


def test_ask_user_labels_the_count_never_the_text() -> None:
    label = labels.label_for("ask_user", {"questions": [{"question": "internal codename?"}]})
    assert label == "1 question"
    assert "codename" not in label


def test_an_unknown_tool_shows_arg_keys_never_values() -> None:
    """A tool this file does not know - a connector's, usually - labels with its
    arg KEYS, the same keys-only format the connector journal writes, so the two surfaces
    cannot drift. Values never appear; a no-arg unknown gets nothing but its name."""
    assert catalog.journal_name("teleport_user") == "teleport_user"
    assert labels.label_for("teleport_user", {"query": "anything"}) == "args: query"
    assert "anything" not in labels.label_for("teleport_user", {"query": "anything"})
    assert labels.label_for("gmail_send_email", {"to": "a@b.c", "body": "SECRET"}) == "args: body, to"
    assert labels.label_for("teleport_user", {}) == ""


def test_the_web_tools_labels_carry_locators_never_content() -> None:
    assert catalog.journal_name("web_fetch") == "web.fetch"
    assert labels.fetch_label("https://ex.com/a/b?token=SECRET") == "ex.com/a/b"
    assert catalog.journal_name("web_search") == "web.search"
    assert labels.label_for("web_search", {"queries": ["SECRET plans"], "n": 5}) == "1 query, 5 per query"
    assert labels.label_for("web_search", {"queries": ["SECRET a", "SECRET b"]}) == "2 queries"
    assert "SECRET" not in labels.label_for("web_search", {"queries": ["SECRET plans"]})


def test_a_reason_carries_no_more_than_a_summary_would() -> None:
    """`denied_reason` is the OTHER field that reaches the journal, and it is written by an
    exception rather than by this module - so the redaction happens on the way out (the record
    calls this), by the same function that writes the summary."""
    assert (
        labels.scrub_reason(
            "HTTPStatusError: Client error '429' for url "
            "'https://api.search.brave.com/res/v1/web/search?q=SECRET+QUERY&count=5'"
        )
        == "HTTPStatusError: Client error '429' for url 'api.search.brave.com/res/v1/web/search'"
    )
    assert "SECRETTOKEN" not in labels.scrub_reason(
        "FetchDenied: example.com answered 404 for https://example.com/d?access_token=SECRETTOKEN"
    )
    assert "example.com/d" in labels.scrub_reason(
        "FetchDenied: example.com answered 404 for https://example.com/d?access_token=SECRETTOKEN"
    ), "host+path survives - a reason nobody can act on is not worth recording"
    assert labels.scrub_reason("not granted (has: ['frame.read_file'])") == (
        "not granted (has: ['frame.read_file'])"
    ), "a reason with no URL in it passes through untouched"
    assert labels.scrub_reason("") == ""
    assert len(labels.scrub_reason("x" * 5_000)) == labels.REASON_MAX


def test_the_journal_vocabulary_is_the_wire_vocabulary() -> None:
    assert catalog.journal_name("read_file") == "frame.read_file"
    assert catalog.journal_name("shell") == "frame.shell"
    assert catalog.journal_name("publish") == "frame.publish"
    assert catalog.journal_name("write_file") == "frame.write_file"


def test_the_live_label_is_the_journal_summary() -> None:
    """The parity pin: `MessageTap` builds live labels via `label_for`; `frame_tools`
    builds journal summaries from the primitives - same inputs, same string, so the two
    surfaces cannot drift."""
    assert labels.label_for("read_file", {"path": "/w/a.md"}) == labels.clamp("/w/a.md")
    assert labels.label_for("shell", {"command": "x" * 500}) == labels.command_label("x" * 500)
    assert labels.label_for("publish", {"type": "document", "path": "/w/a.md"}) == (
        labels.publish_label("document", "/w/a.md")
    )


def test_a_cloud_frame_keeps_the_models_words_byte_for_byte() -> None:
    """`SpellsVirtual` is the device's alone, so a cloud frame's labels are what they always were,
    a real-looking path and a long command included."""
    cloud: list[type] = [DockerFrame, SandbarFrame]
    for driver in cloud:
        assert not issubclass(driver, labels.SpellsVirtual)
    frame = FakeFrame("t1")
    path, command = "/Users/me/.ssh/" + "k" * 300, "cat /Users/me/.ssh/id " + "x" * 300
    assert labels.path_label(path, frame) == labels.clamp(path) == path[: labels.LABEL_MAX]
    assert labels.command_label(command, frame) == command[: labels.COMMAND_MAX]
    assert labels.publish_label("document", path, frame) == labels.clamp(f"document:{path}")
    assert labels.said_path(path, frame) == path
    for tool, args in (
        ("read_file", {"path": path}),
        ("write_file", {"path": path, "content": "x"}),
        ("shell", {"command": command}),
        ("publish", {"type": "document", "path": path}),
        ("generate_image", {"path": path, "n": 2}),
    ):
        assert labels.label_for(tool, args, frame) == labels.label_for(tool, args)


async def test_a_cloud_frames_tools_journal_the_models_words() -> None:
    rows: list[str] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append(summary)

    frame = FakeFrame("t1")
    await frame.create("t1", [])
    previews: list[str] = []

    async def preview(path: str, _content: str) -> None:
        previews.append(path)

    toolset = frame_toolset(
        ("frame.write_file", "frame.read_file", "frame.shell"), frame, journal, (), preview
    )
    calls = [
        ("write_file", {"path": "/work/../work/a.md", "content": "x"}),
        ("shell", {"command": "cat /work/a.md"}),
    ]
    await run_recorded(toolset, calls, journal, frame=frame)
    assert rows == ["/work/../work/a.md", "cat /work/a.md"]
    assert previews == ["/work/a.md"], "the preview names the file written, normalized"


def test_egress_labels_the_host_never_the_url() -> None:
    """The proxy's denial reaches the wire as `host=…` - the exact prefix the web reads
    back - and an unknown host stays honestly marked."""
    assert labels.egress_label("pypi.org") == "host=pypi.org"
    assert labels.egress_label("") == "host=?"


def test_a_browser_label_names_the_element_and_keeps_a_pressed_key() -> None:
    """With a focused element's fingerprint, "press Enter" and "press Escape" must not read
    alike in the journal, and an element the page left unnamed adds no empty quotes."""
    fp = {"role": "button", "name": "Save", "host": "a.com", "path": "/x"}
    assert labels.browser_label(fp, "click") == 'a.com/x · button "Save"'
    assert labels.browser_label(fp, "press Enter") == 'a.com/x · button "Save" · Enter'
    assert labels.browser_label({**fp, "role": "", "name": ""}, "press Escape") == "a.com/x · Escape"
    assert labels.browser_label(None, "press Enter") == "press Enter"


@pytest.mark.parametrize(
    ("tool", "named"),
    [
        ("read_file", "frame.read_file"),
        ("web_fetch", "web.fetch"),
        ("browser_click", "browser.click"),
        ("linear_create_issue", "linear_create_issue"),
    ],
)
def test_a_tool_journals_under_its_catalog_name(tool: str, named: str) -> None:
    assert catalog.journal_name(tool) == named


def test_every_browser_verb_has_its_row_in_the_names_table() -> None:
    assert set(BROWSER_TOOLS) <= {tool.journal for tool in catalog.TOOLS}
    assert {tool.name for tool in catalog.TOOLS if tool.journal in BROWSER_TOOLS} == TOOL_NAMES
