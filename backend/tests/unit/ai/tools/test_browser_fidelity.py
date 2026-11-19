# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Browser fidelity: some sites (makemytrip.com) reset the stream on the sandbox's headless
Chromium (net::ERR_HTTP2_PROTOCOL_ERROR) on www. and on the bare host, and the person would be
asked twice - once for www.makemytrip.com, once for makemytrip.com. The browser
names itself as the same Chromium with a window when the operator turns that on (off by default),
and a bare host opens at its approved www. twin.
The wall's own ending lives with `test_unreachable_host_is_final`, the pairing's pure rules with
`test_browser_policy`."""

from __future__ import annotations

import sys

import pytest
from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, browser_toolset
from prismoid.conf import WorkerSettings
from tests.support.fakes.browser_fakes import FakeSidecar, RefusingSidecar, agent, journal, ports, returns
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar import browser as sidecar_browser  # noqa: E402

# ── the user agent: the headed Chromium's own, never `HeadlessChrome` ─────────────────────────

CHROME_156_LINUX = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)


def test_the_user_agent_is_what_the_same_chromium_sends_with_a_window() -> None:
    """An Akamai-fronted site (makemytrip.com) resets the stream on the
    `HeadlessChrome` token (net::ERR_HTTP2_PROTOCOL_ERROR) and serves the same Chromium the page
    under `Chrome`. The UA is the one this Chromium sends headed - its version (UA-reduced), its
    OS, the client-hint brands untouched - so it names the browser it is, minus the missing window."""
    assert (
        sidecar_browser.user_agent("Chromium 156.0.8078.79 built on Debian GNU/Linux 12") == CHROME_156_LINUX
    )
    assert sidecar_browser.user_agent("") is None  # no version read: Chromium's own UA stands
    assert sidecar_browser.user_agent("chromium: not found") is None


def test_the_launch_carries_that_user_agent_only_when_the_operator_turned_it_on() -> None:
    """The headed UA passes a site's headless filter, so it is the operator's switch
    (`PRISMOID_WORKER_BROWSER_HEADED_UA`, the owner's call), off by default - Chromium's own UA stands
    and such a site ends honestly. On, it is the same Chromium's headed UA and no other disguise."""
    version = "Chromium 156.0.8078.79 built on Debian"
    assert sidecar_browser.launch_flags(version) == sidecar_browser.CHROMIUM_FLAGS
    assert not any(f.startswith("--user-agent") for f in sidecar_browser.launch_flags(version))
    flags = sidecar_browser.launch_flags(version, headed_ua=True)
    assert f"--user-agent={CHROME_156_LINUX}" in flags
    assert not any("AutomationControlled" in f for f in flags)  # navigator.webdriver is not ours to hide
    assert sidecar_browser.launch_flags("", headed_ua=True) == sidecar_browser.CHROMIUM_FLAGS


def test_the_switch_is_off_by_default_and_reaches_the_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    assert WorkerSettings.model_fields["browser_headed_ua"].default is False
    monkeypatch.setenv("PRISMOID_WORKER_BROWSER_HEADED_UA", "true")
    assert WorkerSettings().browser_headed_ua is True
    assert sidecar_browser.Browser(profile_dir="/p", proxy=None).headed_ua is False


# ── a bare host and its www. twin, through the toolset ───────────────────────────────────────


async def test_a_bare_host_opens_at_its_allowed_www_twin_and_a_bounce_back_asks_next_time() -> None:
    """The person allowed www., so the bare host must not ask again. It
    opens at the twin, unasked and unwidened; a twin that sends the browser back to the bare host
    (the proxy refuses it) un-pairs it, so the model's next open asks instead of looping."""
    sidecar, rows, widened = RefusingSidecar(), [], []
    toolset = browser_toolset(
        BROWSER_TOOLS, journal(rows), ports(sidecar, {"www.example.org"}, widened), Approvals(tainted=tainted)
    )
    opens = [
        ToolCallPart(tool_name="browser_open", args={"url": "https://example.org/a"}, tool_call_id=f"o{i}")
        for i in (1, 2)
    ]

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = len(returns(messages))
        return ModelResponse(parts=[opens[seen]]) if seen < 2 else ModelResponse(parts=[TextPart("x")])

    out = await agent(toolset, script).run("go")
    assert [c["url"] for c in sidecar.calls] == ["https://www.example.org/a"]
    assert widened == []
    assert isinstance(out.output, DeferredToolRequests)
    assert out.output.metadata[out.output.approvals[0].tool_call_id]["host"] == "example.org"
    first = returns(out.all_messages())[0].content
    assert isinstance(first, str) and "open example.org again" in first


async def test_the_approved_bare_host_opens_itself_after_the_resume_and_never_loops() -> None:
    """The person said yes once to the bare host; the resumed run (a new toolset) paired it to
    the twin again, the twin sent the browser back, and the person was asked again, for ever. The approved open goes to the host approved, widening for it."""
    sidecar, rows, widened = RefusingSidecar(), [], []
    toolset = browser_toolset(
        BROWSER_TOOLS, journal(rows), ports(sidecar, {"www.example.org"}, widened), Approvals(tainted=tainted)
    )
    opens = [
        ToolCallPart(tool_name="browser_open", args={"url": "https://example.org/a"}, tool_call_id=f"o{i}")
        for i in (1, 2)
    ]

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = len(returns(messages))
        return ModelResponse(parts=[opens[seen]]) if seen < 2 else ModelResponse(parts=[TextPart("x")])

    parked = await agent(toolset, script).run("go")
    assert isinstance(parked.output, DeferredToolRequests)
    resumed_sidecar, resumed_widened = FakeSidecar(), []
    resumed = browser_toolset(
        BROWSER_TOOLS,
        journal(rows),
        ports(resumed_sidecar, {"www.example.org"}, resumed_widened),
        Approvals(tainted=tainted),
    )
    out = await agent(resumed, script).run(
        message_history=parked.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={"o2": True}),
    )
    assert [c["url"] for c in resumed_sidecar.calls if c["verb"] == "open"] == ["https://example.org/a"]
    assert resumed_widened == [["example.org", "www.example.org"]]
    assert out.output == "x"


async def test_a_resumed_run_still_asks_for_the_bare_host_its_twin_sent_back() -> None:
    """The bounce lives in the history, not in the toolset that saw it: a resumed run's
    unapproved open of the bare host asks for it instead of pairing it again."""
    first = await agent(
        browser_toolset(
            BROWSER_TOOLS,
            journal([]),
            ports(RefusingSidecar(), {"www.example.org"}, []),
            Approvals(tainted=tainted),
        ),
        lambda messages, info: (
            ModelResponse(parts=[TextPart("x")])
            if returns(messages)
            else ModelResponse(
                parts=[ToolCallPart("browser_open", {"url": "https://example.org/a"}, tool_call_id="o1")]
            )
        ),
    ).run("go")
    sidecar = FakeSidecar()
    again = await agent(
        browser_toolset(
            BROWSER_TOOLS, journal([]), ports(sidecar, {"www.example.org"}, []), Approvals(tainted=tainted)
        ),
        lambda messages, info: ModelResponse(
            parts=[ToolCallPart("browser_open", {"url": "https://example.org/a"}, tool_call_id="o2")]
        ),
    ).run("open it again", message_history=first.all_messages())
    assert isinstance(again.output, DeferredToolRequests) and sidecar.calls == []
