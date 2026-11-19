# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/frames/image_browser/test_browser_sidecar_browser.py
"""`Browser`'s pure/near-pure pieces that never need a live Chromium: the proxy-server argv
value (no credential ever reaches argv) and the `_settle` state machine (a verb that never
actually navigates must not burn the full 10 s waiting for a load event that will never fire)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import pytest

from tests.support.fakes.cdp_scripts import AX_BUTTON, FakeCdp
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.browser import Browser  # noqa: E402
from sidecar.cdp import CdpError  # noqa: E402
from sidecar.scripts import INSPECT_JS, WHOLE_VALUE_JS  # noqa: E402

# ── _proxy_arg ────────────────────────────────────────────────────────────────────────────────


def test_proxy_arg_strips_userinfo_and_keeps_scheme_host_port():
    b = Browser(profile_dir="/nonexistent", proxy="http://sb:secret@proxy:3128")
    assert b._proxy_arg() == "--proxy-server=http://proxy:3128"  # pyright: ignore[reportPrivateUsage]


def test_proxy_arg_never_leaks_the_credential():
    b = Browser(profile_dir="/nonexistent", proxy="http://sb:secret@proxy:3128")
    arg = b._proxy_arg()  # pyright: ignore[reportPrivateUsage]
    assert "secret" not in arg
    assert "sb" not in arg


def test_proxy_arg_with_no_scheme_keeps_bare_host_port():
    b = Browser(profile_dir="/nonexistent", proxy="user:pw@proxy:3128")
    assert b._proxy_arg() == "--proxy-server=proxy:3128"  # pyright: ignore[reportPrivateUsage]


def test_proxy_arg_with_no_credentials_is_unchanged():
    b = Browser(profile_dir="/nonexistent", proxy="http://proxy:3128")
    assert b._proxy_arg() == "--proxy-server=http://proxy:3128"  # pyright: ignore[reportPrivateUsage]


# ── _settle state machine ────────────────────────────────────────────────────────────────────


def _wired_browser() -> tuple[Browser, FakeCdp]:
    """The settle reads the page (`watch.probe`) - a scripted connection that answers nothing,
    so only the events a test fires decide it."""
    b = Browser(profile_dir="/nonexistent", proxy=None)
    fake = FakeCdp()
    b.connect(fake)
    b.verbs._main_frame_id = "F1"  # pyright: ignore[reportPrivateUsage]
    return b, fake


async def test_settle_returns_quickly_when_nothing_navigates():
    b, _fake = _wired_browser()
    start = time.monotonic()
    await b.verbs._settle()  # pyright: ignore[reportPrivateUsage]
    assert time.monotonic() - start < 1.0


async def test_settle_waits_for_the_load_event_once_a_navigation_starts():
    b, fake = _wired_browser()
    fake.emit("Network.requestWillBeSent", {"requestId": "doc"})  # the navigation's own request

    async def drive() -> None:
        await asyncio.sleep(0.05)
        fake.emit("Page.frameStartedLoading", {"frameId": "F1"})
        await asyncio.sleep(0.2)
        fake.emit("Page.loadEventFired", {})

    driver = asyncio.create_task(drive())
    start = time.monotonic()
    await b.verbs._settle()  # pyright: ignore[reportPrivateUsage]
    elapsed = time.monotonic() - start
    await driver
    # It waited past the 0.4s "did it even start" window AND past the moment the load fired,
    # not just the bare 0.15s no-navigation path.
    assert elapsed >= 0.2


async def test_settle_ignores_a_started_loading_event_for_a_different_frame():
    """An iframe navigating is not the main-frame navigation `_settle` cares about."""
    b, fake = _wired_browser()
    fake.emit("Page.frameStartedLoading", {"frameId": "some-iframe"})
    start = time.monotonic()
    await b.verbs._settle()  # pyright: ignore[reportPrivateUsage]
    assert time.monotonic() - start < 1.0


async def test_frame_stopped_loading_for_the_main_frame_also_settles_without_a_load_event():
    """`back` served from bfcache: Chromium may report the navigation as started and stopped
    with no `Page.loadEventFired` ever firing - `frameStoppedLoading` on the main frame must be
    enough to unblock `_settle` on its own."""
    b, fake = _wired_browser()
    fake.emit("Network.requestWillBeSent", {"requestId": "doc"})

    async def drive() -> None:
        await asyncio.sleep(0.05)
        fake.emit("Page.frameStartedLoading", {"frameId": "F1"})
        await asyncio.sleep(0.05)
        fake.emit("Page.frameStoppedLoading", {"frameId": "F1"})

    driver = asyncio.create_task(drive())
    start = time.monotonic()
    await b.verbs._settle()  # pyright: ignore[reportPrivateUsage]
    elapsed = time.monotonic() - start
    await driver
    assert elapsed < 1.0  # settled promptly once the main frame stopped, no 10s wait


# ── scripted CDP: refused pages, expect fingerprints, inspect ───────────────────────────────────


@pytest.fixture
def cdp() -> FakeCdp:
    return FakeCdp()


@pytest.fixture
def browser(cdp: FakeCdp) -> Browser:
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.connect(cdp)
    b.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    b.verbs._main_frame_id = "F"  # pyright: ignore[reportPrivateUsage]
    b.alive = True
    return b


# ── Fetch: the egress proxy's auth challenge ─────────────────────────────────────────────────


@pytest.fixture
def browser_with_credential(cdp: FakeCdp) -> Browser:
    b = Browser(profile_dir="/nonexistent", proxy="http://sb-1:s3cr%3At@proxy:7080")
    b.connect(cdp)
    b.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    b.verbs._main_frame_id = "F"  # pyright: ignore[reportPrivateUsage]
    b.alive = True
    return b


@pytest.fixture
def browser_without_credential(cdp: FakeCdp) -> Browser:
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.connect(cdp)
    b.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    b.verbs._main_frame_id = "F"  # pyright: ignore[reportPrivateUsage]
    b.alive = True
    return b


def test_a_proxy_credential_is_parsed_from_userinfo_but_never_reaches_argv() -> None:
    b = Browser(profile_dir="/tmp/p", proxy="http://sb-1:s3cr%3At@host.docker.internal:7080")
    assert b._proxy_credential == ("sb-1", "s3cr:t")  # pyright: ignore[reportPrivateUsage]
    assert b._proxy_arg() == "--proxy-server=http://host.docker.internal:7080"  # pyright: ignore[reportPrivateUsage]


def test_a_proxy_credential_is_parsed_with_no_scheme_too() -> None:
    b = Browser(profile_dir="/tmp/p", proxy="sb-1:s3cr%3At@proxy:7080")
    assert b._proxy_credential == ("sb-1", "s3cr:t")  # pyright: ignore[reportPrivateUsage]


def test_a_proxy_with_no_userinfo_has_no_credential() -> None:
    b = Browser(profile_dir="/tmp/p", proxy="http://proxy:7080")
    assert b._proxy_credential is None  # pyright: ignore[reportPrivateUsage]


async def test_proxy_challenge_is_answered_with_the_credential(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    cdp.emit(
        "Fetch.authRequired",
        {
            "requestId": "r1",
            "authChallenge": {
                "source": "Proxy",
                "origin": "http://host.docker.internal:7080",
                "scheme": "basic",
            },
        },
    )
    await cdp.drain()
    assert cdp.sent("Fetch.continueWithAuth") == [
        {
            "requestId": "r1",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": "sb-1",
                "password": "s3cr:t",
            },
        }
    ]


async def test_a_server_challenge_is_cancelled_not_answered(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    cdp.emit(
        "Fetch.authRequired",
        {
            "requestId": "r2",
            "authChallenge": {"source": "Server", "origin": "https://site.example", "scheme": "basic"},
        },
    )
    await cdp.drain()
    assert cdp.sent("Fetch.continueWithAuth") == [
        {"requestId": "r2", "authChallengeResponse": {"response": "CancelAuth"}}
    ]


async def test_a_rejected_proxy_credential_is_not_retried_for_the_same_request(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    """Chromium re-issues `Fetch.authRequired` for the SAME requestId when the credential it was
    given is wrong or revoked, rather than fail the request outright - answering
    `ProvideCredentials` again every time would loop forever. The second attempt for a requestId
    must get `CancelAuth`, failing the request once instead."""
    challenge = {
        "requestId": "r1",
        "authChallenge": {"source": "Proxy", "origin": "http://host.docker.internal:7080", "scheme": "basic"},
    }
    cdp.emit("Fetch.authRequired", challenge)
    await cdp.drain()
    cdp.emit("Fetch.authRequired", challenge)
    await cdp.drain()
    sent = cdp.sent("Fetch.continueWithAuth")
    assert sent == [
        {
            "requestId": "r1",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": "sb-1",
                "password": "s3cr:t",
            },
        },
        {"requestId": "r1", "authChallengeResponse": {"response": "CancelAuth"}},
    ]


async def test_a_different_request_id_still_gets_credentials_after_a_rejection(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    proxy_challenge = {"source": "Proxy", "origin": "http://host.docker.internal:7080", "scheme": "basic"}
    cdp.emit("Fetch.authRequired", {"requestId": "r1", "authChallenge": proxy_challenge})
    await cdp.drain()
    cdp.emit("Fetch.authRequired", {"requestId": "r2", "authChallenge": proxy_challenge})
    await cdp.drain()
    sent = cdp.sent("Fetch.continueWithAuth")
    assert sent == [
        {
            "requestId": "r1",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": "sb-1",
                "password": "s3cr:t",
            },
        },
        {
            "requestId": "r2",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": "sb-1",
                "password": "s3cr:t",
            },
        },
    ]


async def test_paused_requests_are_continued(browser_with_credential: Browser, cdp: FakeCdp) -> None:
    cdp.emit("Fetch.requestPaused", {"requestId": "r3", "request": {"url": "https://site.example/"}})
    await cdp.drain()
    assert cdp.sent("Fetch.continueRequest") == [{"requestId": "r3"}]


async def test_without_a_credential_fetch_is_never_enabled(
    browser_without_credential: Browser, cdp: FakeCdp
) -> None:
    # The real `start()` spawns Chromium over a pipe, which this unit test never does; the
    # conditional itself - never enable `Fetch` without a credential - is what this proves.
    await browser_without_credential._enable_fetch_if_credentialed("S1")  # pyright: ignore[reportPrivateUsage]
    assert not cdp.sent("Fetch.enable")


async def test_with_a_credential_fetch_is_enabled_for_every_request(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    await browser_with_credential._enable_fetch_if_credentialed("S1")  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent("Fetch.enable") == [{"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]}]


AX_FORM = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "childIds": ["2"]},
    {
        "nodeId": "2",
        "role": {"value": "textbox"},
        "name": {"value": "Email"},
        "backendDOMNodeId": 51,
        "childIds": [],
    },
]

AX_COMBOBOX = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "childIds": ["2"]},
    {
        "nodeId": "2",
        "role": {"value": "combobox"},
        "name": {"value": "Country"},
        "backendDOMNodeId": 77,
        "childIds": [],
    },
]

AX_TWO_FORMS = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "childIds": ["2", "3"]},
    {
        "nodeId": "2",
        "role": {"value": "textbox"},
        "name": {"value": "Search"},
        "backendDOMNodeId": 61,
        "childIds": [],
    },
    {
        "nodeId": "3",
        "role": {"value": "textbox"},
        "name": {"value": "Type DELETE to confirm"},
        "backendDOMNodeId": 62,
        "childIds": [],
    },
]


def _ref_for(text: str, needle: str) -> str:
    """The reference right before `needle` on its line: the look puts a small form on one line."""
    line = next(line for line in text.splitlines() if needle in line)
    return re.findall(r"\[(e\d+\.\d+)\]", line[: line.index(needle)])[-1]


@pytest.fixture
async def snapshot_with_button(browser: Browser, cdp: FakeCdp) -> str:
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})
    snap = await browser.handle({"verb": "snapshot"})
    return _ref_for(snap["text"], 'button "Save changes"')


@pytest.fixture
async def snapshot_with_form(browser: Browser, cdp: FakeCdp) -> str:
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_FORM})
    cdp.answer(
        "Runtime.callFunctionOn",
        {"result": {"value": {"form_labels": ["Email"], "submit_name": "Send invoice"}}},
    )
    snap = await browser.handle({"verb": "snapshot"})
    return _ref_for(snap["text"], 'textbox "Email"')


@pytest.fixture
async def snapshot_with_combobox(browser: Browser, cdp: FakeCdp) -> str:
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_COMBOBOX})
    snap = await browser.handle({"verb": "snapshot"})
    return _ref_for(snap["text"], 'combobox "Country"')


@pytest.mark.parametrize(
    ("code", "error", "wall"),
    [
        ("TUNNEL_CONNECTION_FAILED", "egress refused", "egress"),
        ("NAME_NOT_RESOLVED", "navigation failed: net::ERR_NAME_NOT_RESOLVED", "unresolved"),
        ("HTTP2_PROTOCOL_ERROR", "navigation failed: net::ERR_HTTP2_PROTOCOL_ERROR", "reset"),
    ],
)
async def test_a_failed_open_says_why_and_names_the_wall(
    browser: Browser, cdp: FakeCdp, code: str, error: str, wall: str
) -> None:
    cdp.answer("Page.navigate", {"frameId": "F", "errorText": f"net::ERR_{code}"})
    result = await browser.handle({"verb": "open", "url": "https://plane.so/"})
    assert result == {"ok": False, "error": error, "blocked": wall}


async def test_open_never_calls_a_chrome_error_page_a_success(browser: Browser, cdp: FakeCdp) -> None:
    cdp.answer("Page.navigate", {"frameId": "F"})
    cdp.answer(
        "Page.getNavigationHistory",
        {"currentIndex": 0, "entries": [{"url": "chrome-error://chromewebdata/", "title": ""}]},
    )
    result = await browser.handle({"verb": "open", "url": "https://plane.so/"})
    assert result["ok"] is False


async def test_click_with_a_matching_expect_acts_and_a_mismatch_intervenes(
    browser: Browser, cdp: FakeCdp, snapshot_with_button: str
) -> None:
    ref = snapshot_with_button
    ok = await browser.handle(
        {
            "verb": "click",
            "ref": ref,
            "expect": {"role": "button", "name": "Save changes", "host": "127.0.0.1", "path": "/index.html"},
        }
    )
    assert ok["ok"] is True
    bad = await browser.handle(
        {
            "verb": "click",
            "ref": ref,
            "expect": {"role": "button", "name": "Delete all", "host": "127.0.0.1", "path": "/index.html"},
        }
    )
    assert bad["ok"] is False and bad["error"] == "intervened" and bad["actual"]["name"] == "Save changes"


async def test_inspect_describes_an_element_and_its_form(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    ref = snapshot_with_form
    info = await browser.handle({"verb": "inspect", "ref": ref})
    assert info["ok"] and info["role"] == "textbox" and info["name"] == "Email"
    assert info["submit_name"] == "Send invoice" and "Email" in info["form_labels"]


def _declares_a_function(source: str) -> bool:
    """What `Runtime.callFunctionOn` asks of its `functionDeclaration` (UI check D3): the source,
    evaluated as an expression, IS a function. `function(...){...}` with nothing after its body;
    an immediately-invoked one evaluates to whatever it returns. Braces in string literals and
    comments are skipped."""
    text = source.strip()
    if not text.startswith("function"):
        return False
    depth, i, quote = 0, text.index("{"), ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 :].strip() == ""
        i += 1
    return False


class CallsOnTheElement(FakeCdp):
    """CDP's own rule for `Runtime.callFunctionOn`: a declaration that does not evaluate to a
    function is refused ("Given expression does not evaluate to a function", as Chromium says it);
    one that does runs with the element as `this`, and its form answers."""

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        if method == "Runtime.callFunctionOn" and params is not None:
            self.calls.append((method, params))
            if not _declares_a_function(str(params.get("functionDeclaration", ""))):
                raise CdpError(-32000, "Given expression does not evaluate to a function")
            assert params.get("objectId") == "obj-1", "it runs on the resolved element"
            return {
                "result": {"value": {"form_labels": ["Email", "Password"], "submit_name": "Delete account"}}
            }
        return await super().call(method, params, session_id=session_id, timeout=timeout)


def test_inspect_js_is_a_function_declaration_never_an_invoked_one() -> None:
    assert _declares_a_function("function(){ return 1; }")
    assert not _declares_a_function("(function(el){ return {}; })(this)")
    assert not _declares_a_function("function(){ return 1; }()")
    assert _declares_a_function(INSPECT_JS), "callFunctionOn must be handed a function to call"


async def test_inspect_hands_the_danger_check_the_form_labels() -> None:
    """UI check D3: `INSPECT_JS` was an IIFE, so CDP refused every inspect on some pages (and with
    it every click, press and fill the toolset inspects first) and on others ran it against
    `window`, handing the submit danger check no labels. Called on the element, the form's labels
    and its submit name come back for the check."""
    cdp = CallsOnTheElement()
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.connect(cdp)
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_FORM})
    snap = await b.handle({"verb": "snapshot"})
    info = await b.handle({"verb": "inspect", "ref": _ref_for(snap["text"], 'textbox "Email"')})
    assert info["ok"] is True, info
    assert info["form_labels"] == ["Email", "Password"] and info["submit_name"] == "Delete account"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JavaScript")
def test_inspect_js_reads_the_element_it_is_called_on() -> None:
    """The declaration itself, run the way CDP runs it (`this` is the element) on a stand-in DOM:
    the labels of the element's form, and its submit control's name."""
    script = f"""
const fn = ({INSPECT_JS});
if (typeof fn !== "function") {{ console.log(JSON.stringify({{error: typeof fn}})); process.exit(0); }}
const control = (label, extra = {{}}) => ({{
  labels: label ? [{{textContent: " " + label + " "}}] : null,
  getAttribute: (k) => (extra[k] ?? null),
  name: extra.name || "",
}});
const submit = {{getAttribute: (k) => (k === "aria-label" ? "Delete account" : null), value: "", textContent: "Go"}};
const form = {{
  getAttribute: () => null,
  querySelectorAll: () => [control("Email"), control(null, {{placeholder: "Password"}}), control(null)],
  querySelector: () => submit,
}};
const el = {{form, closest: () => null}};
const select = {{form: null, closest: () => null, options: [{{value: "ca", text: " Canada "}}]}};
console.log(JSON.stringify({{
  called: fn.call(el),
  lonely: fn.call({{form: null, closest: () => null}}),
  picked: fn.call(select, "ca"),
}}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout)
    blank: dict[str, object] = {"submit_name": "", "option": ""}
    blank |= dict.fromkeys(("submits", "navigates", "form_navigates"), False)
    assert got["called"] == blank | {"form_labels": ["Email", "Password"], "submit_name": "Delete account"}
    assert got["lonely"] == blank | {"form_labels": []}
    assert got["picked"] == blank | {"form_labels": [], "option": "Canada"}


async def test_inspect_hands_a_select_value_to_the_page_and_answers_the_option_it_picks(
    browser: Browser, cdp: FakeCdp, snapshot_with_combobox: str
) -> None:
    cdp.answer(
        "Runtime.callFunctionOn",
        {"result": {"value": {"form_labels": [], "submit_name": "", "option": "Canada"}}},
    )
    info = await browser.handle({"verb": "inspect", "ref": snapshot_with_combobox, "value": "ca"})
    assert info["ok"] and info["option"] == "Canada"
    sent = cdp.sent("Runtime.callFunctionOn")[-1]
    assert sent is not None and sent["arguments"] == [{"value": "ca"}], "the value is data, never code"


async def test_inspect_fails_closed_when_the_page_script_throws(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    """A form the script could not read is not a form without a submit control: answering
    "no form" would let the danger check read nothing and act."""
    cdp.answer(
        "Runtime.callFunctionOn",
        {"result": {"type": "object", "subtype": "error"}, "exceptionDetails": {"text": "Uncaught"}},
    )
    info = await browser.handle({"verb": "inspect", "ref": snapshot_with_form})
    assert info["ok"] is False


# ── core safety: an intervened action never touches the page ────────────────────────────────────


async def test_an_intervened_click_sends_no_input_calls(
    browser: Browser, cdp: FakeCdp, snapshot_with_button: str
) -> None:
    ref = snapshot_with_button
    bad = await browser.handle({"verb": "click", "ref": ref, "expect": {"name": "Delete all"}})
    assert bad["ok"] is False and bad["error"] == "intervened"
    assert not any(method.startswith("Input.") for method, _ in cdp.calls)


async def test_an_intervened_type_sends_no_input_calls(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    ref = snapshot_with_form
    bad = await browser.handle({"verb": "type", "ref": ref, "text": "x", "expect": {"name": "Password"}})
    assert bad["ok"] is False and bad["error"] == "intervened"
    assert not any(method.startswith("Input.") for method, _ in cdp.calls)


async def test_scroll_with_a_stale_ref_and_an_expect_answers_stale_not_intervened(
    browser: Browser, cdp: FakeCdp
) -> None:
    """A non-empty `ref` that isn't live is a stale reference, not "no ref at all" - it must
    never fall into `_live_role_name`'s focused-element branch and come back a false
    "intervened" wearing the focused element's real role/name as `actual`."""
    result = await browser.handle(
        {"verb": "scroll", "ref": "e0.7", "expect": {"role": "button", "name": "Save changes"}}
    )
    assert result == {"ok": False, "error": "stale reference e0.7; take a new snapshot"}
    assert not any(method.startswith("Input.") for method, _ in cdp.calls)


# ── expect, honoured verb by verb (live role/name, not the snapshot-time cache) ─────────────────


async def test_type_with_a_matching_expect_acts_and_a_mismatch_intervenes(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    ref = snapshot_with_form
    cdp.answer(
        "Accessibility.getPartialAXTree",
        {"nodes": [{"role": {"value": "textbox"}, "name": {"value": "Email"}}]},
    )
    ok = await browser.handle(
        {"verb": "type", "ref": ref, "text": "a@b.com", "expect": {"role": "textbox", "name": "Email"}}
    )
    assert ok["ok"] is True
    bad = await browser.handle(
        {"verb": "type", "ref": ref, "text": "a@b.com", "expect": {"role": "textbox", "name": "Password"}}
    )
    assert bad["ok"] is False and bad["error"] == "intervened" and bad["actual"]["name"] == "Email"


async def test_fill_checks_every_field_against_its_own_fingerprint_before_typing_any(
    browser: Browser, cdp: FakeCdp
) -> None:
    """Enter lands in the LAST field's form, so a fill is bound field by field (`expect` keyed
    by ref): a page that moved under the second field intervenes before the first keystroke."""
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_TWO_FORMS})
    snap = await browser.handle({"verb": "snapshot"})
    search, confirm = _ref_for(snap["text"], '"Search"'), _ref_for(snap["text"], '"Type DELETE')
    names = {61: "Search", 62: "Type DELETE to confirm"}
    cdp.answer(
        "Accessibility.getPartialAXTree",
        lambda p: {"nodes": [{"role": {"value": "textbox"}, "name": {"value": names[p["backendNodeId"]]}}]},
    )
    fields = [{"ref": search, "value": "x"}, {"ref": confirm, "value": "DELETE"}]
    expect = {
        search: {"role": "textbox", "name": "Search"},
        confirm: {"role": "textbox", "name": "Type DELETE to confirm"},
    }
    moved = {**expect, confirm: {"role": "textbox", "name": "Search"}}
    bad = await browser.handle({"verb": "fill", "fields": fields, "submit": True, "expect": moved})
    assert bad["ok"] is False and bad["error"] == "intervened"
    assert bad["actual"]["name"] == "Type DELETE to confirm"
    assert not any(m.startswith("Input.") or m == "DOM.focus" for m, _ in cdp.calls), "nothing typed"
    ok = await browser.handle({"verb": "fill", "fields": fields, "submit": True, "expect": expect})
    assert ok["ok"] is True


async def test_select_with_a_matching_expect_acts_and_a_mismatch_intervenes(
    browser: Browser, cdp: FakeCdp, snapshot_with_combobox: str
) -> None:
    ref = snapshot_with_combobox
    cdp.answer(
        "Accessibility.getPartialAXTree",
        {"nodes": [{"role": {"value": "combobox"}, "name": {"value": "Country"}}]},
    )
    ok = await browser.handle(
        {"verb": "select", "ref": ref, "value": "US", "expect": {"role": "combobox", "name": "Country"}}
    )
    assert ok["ok"] is True
    bad = await browser.handle(
        {"verb": "select", "ref": ref, "value": "US", "expect": {"role": "combobox", "name": "Language"}}
    )
    assert bad["ok"] is False and bad["error"] == "intervened" and bad["actual"]["name"] == "Country"


async def test_press_with_a_matching_expect_acts_on_the_focused_element_and_a_mismatch_intervenes(
    browser: Browser, cdp: FakeCdp
) -> None:
    """`press` has no `ref` at all - it acts on whatever currently has focus, so its `expect`
    must be checked against the LIVE focused element, never the (always-empty, for a ref-less
    call) snapshot-meta cache. This is the regression `_check_expect` used to get wrong."""
    cdp.answer("Runtime.evaluate", {"result": {"objectId": "focus-1"}})
    cdp.answer(
        "Accessibility.getPartialAXTree",
        {"nodes": [{"role": {"value": "textbox"}, "name": {"value": "Search"}}]},
    )
    ok = await browser.handle(
        {"verb": "press", "key": "Enter", "expect": {"role": "textbox", "name": "Search"}}
    )
    assert ok["ok"] is True
    bad = await browser.handle(
        {"verb": "press", "key": "Enter", "expect": {"role": "textbox", "name": "Other"}}
    )
    assert bad["ok"] is False and bad["error"] == "intervened" and bad["actual"]["name"] == "Search"


# ── screen.action: the box of the element an acting verb touched ─────────────────────────────

BOX = {"x": 0, "y": 0, "w": 10, "h": 10}  # DEFAULT_ANSWERS' content quad, [0,0, 10,0, 10,10, 0,10]


async def test_click_returns_its_box_and_clicks_exactly_as_before(
    browser: Browser, cdp: FakeCdp, snapshot_with_button: str
) -> None:
    cdp.calls.clear()
    result = await browser.handle({"verb": "click", "ref": snapshot_with_button})
    assert result["ok"] is True and result["box"] == BOX
    methods = [m for m, _ in cdp.calls]
    assert methods.count("DOM.getBoxModel") == 1, "the click's own quad is the box: no second read"
    assert methods.index("DOM.scrollIntoViewIfNeeded") < methods.index("DOM.getBoxModel")
    assert methods.index("DOM.getBoxModel") < methods.index("Input.dispatchMouseEvent")
    mouse = cdp.sent("Input.dispatchMouseEvent")
    assert [p["type"] for p in mouse if p] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert all(p is not None and (p["x"], p["y"]) == (5.0, 5.0) for p in mouse), "the quad's centre"


async def test_type_fill_and_select_return_the_fields_box_without_scrolling(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    cdp.calls.clear()
    typed = await browser.handle({"verb": "type", "ref": snapshot_with_form, "text": "a@b.com"})
    filled = await browser.handle(
        {"verb": "fill", "fields": [{"ref": snapshot_with_form, "value": "a@b.com"}]}
    )
    selected = await browser.handle({"verb": "select", "ref": snapshot_with_form, "value": "US"})
    assert typed["box"] == filled["box"] == selected["box"] == BOX
    assert "DOM.scrollIntoViewIfNeeded" not in [m for m, _ in cdp.calls], "typing never scrolled before"


async def test_a_box_that_cannot_be_read_is_left_out_never_an_error(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    cdp.answer("DOM.getBoxModel", {})  # a detached node: no model
    typed = await browser.handle({"verb": "type", "ref": snapshot_with_form, "text": "a@b.com"})
    assert typed["ok"] is True and "box" not in typed


# ── a tab the page opens (a `target=_blank` link used to freeze the screen) ─────────
# Chromium put the new tab in front and the sidecar stayed on the page it attached at start, now
# hidden: no screencast frames, and a person's input landed where nobody could see it.


def _tabs(b: Browser, cdp: FakeCdp) -> Browser:
    """`b` on page T1 (session S1); the next tab it attaches gets session S2."""
    b.verbs._target = "T1"  # pyright: ignore[reportPrivateUsage]
    b.verbs._tabs = {"T1": "S1"}  # pyright: ignore[reportPrivateUsage]
    b.verbs._order = ["T1"]  # pyright: ignore[reportPrivateUsage]
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})
    return b


@pytest.fixture
def tabbed(browser: Browser, cdp: FakeCdp) -> Browser:
    return _tabs(browser, cdp)


def _opened(
    cdp: FakeCdp,
    target: str = "T2",
    *,
    kind: str = "page",
    opener: str | None = "T1",
    subtype: str = "",
    session: str | None = None,
) -> str:
    """A page opening as Chromium reports it under `AUTO_ATTACH`: discovered, then attached on
    its own and held at its start (`waitingForDebugger`), on session `Sn` for target `Tn` unless
    `session` names another. Returns the session."""
    info: dict[str, Any] = {"targetId": target, "type": kind, "url": "about:blank"}
    if opener is not None:
        info["openerId"] = opener
    if subtype:
        info["subtype"] = subtype
    session = session or "S" + target.removeprefix("T")
    cdp.emit("Target.targetCreated", {"targetInfo": info})
    cdp.emit(
        "Target.attachedToTarget", {"sessionId": session, "targetInfo": info, "waitingForDebugger": True}
    )
    return session


async def test_a_new_tab_is_set_up_and_let_run_at_once_but_followed_only_by_follow(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp)
    await cdp.drain()
    assert cdp.sent_on("Runtime.runIfWaitingForDebugger") == ["S2"]
    assert not cdp.sent("Target.attachToTarget"), "Chromium attached it, held at its start"
    await tabbed.verbs._where()  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent_on("Page.getNavigationHistory") == ["S1"], "an event handler never moves the page"


async def test_a_new_tabs_proxy_answers_are_on_before_it_may_run(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    """The tab's first request - maybe Chromium's first through the proxy,
    no credential cached - must find its `Fetch` auth handler there, so it runs only after."""
    _opened(cdp)
    await cdp.drain()
    on_s2 = [m for m, sess in cdp.sessions if sess == "S2"]
    assert on_s2 == ["Fetch.enable", "Runtime.runIfWaitingForDebugger"]


async def test_a_tab_whose_setup_fails_is_still_let_run(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    """A tab never stays held: its setup failing (the tab closing under it) still releases it."""
    cdp.hooks["Fetch.enable"] = lambda _s, _p: (_ for _ in ()).throw(TimeoutError())
    _opened(cdp)
    await cdp.drain()
    assert cdp.sent_on("Runtime.runIfWaitingForDebugger") == ["S2"]


async def test_a_session_we_let_go_detaching_never_closes_a_tab(tabbed: Browser, cdp: FakeCdp) -> None:
    """Chromium may attach the page itself again; that second session is let go, and its detach
    (which still names the page's target) leaves the page where it is."""
    tabbed.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    _opened(cdp, "T1", opener=None, session="S1-again")
    await cdp.drain()
    assert cdp.sent("Target.detachFromTarget") == [{"sessionId": "S1-again"}]
    cdp.emit("Target.detachedFromTarget", {"sessionId": "S1-again", "targetId": "T1"})
    assert tabbed.page_open and await tabbed.follow() is False


async def test_follow_moves_every_verb_to_the_new_tab_and_brings_it_to_the_front(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp)
    await cdp.drain()
    assert await tabbed.follow() is True
    await tabbed.verbs._where()  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent_on("Page.getNavigationHistory") == ["S2"]
    assert cdp.sent_on("Page.bringToFront") == ["S2"]
    assert {"Page.enable", "DOM.enable", "Accessibility.enable", "Runtime.enable"} <= {
        m for m, s in cdp.sessions if s == "S2"
    }
    assert await tabbed.follow() is False, "nothing left to follow"


async def test_a_tab_that_closes_returns_the_page_to_the_tab_that_opened_it(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp)
    await cdp.drain()
    await tabbed.follow()
    cdp.emit("Target.targetDestroyed", {"targetId": "T2"})
    assert await tabbed.follow() is True
    await tabbed.verbs._where()  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent_on("Page.getNavigationHistory") == ["S1"]
    assert cdp.sent_on("Page.bringToFront") == ["S2", "S1"]


async def test_a_switch_makes_every_reference_of_the_old_page_stale(tabbed: Browser, cdp: FakeCdp) -> None:
    tabbed.verbs._refs = {"e1.1": 7}  # pyright: ignore[reportPrivateUsage]
    _opened(cdp)
    await cdp.drain()
    await tabbed.follow()
    result = await tabbed.handle({"verb": "click", "ref": "e1.1"})
    assert result == {"ok": False, "error": "stale reference e1.1; take a new snapshot"}


@pytest.mark.parametrize(
    ("target", "kind"),
    [("W1", "service_worker"), ("I1", "iframe"), ("T1", "page")],  # T1: the page itself, re-announced
)
async def test_what_is_not_a_new_tab_is_never_followed(
    tabbed: Browser, cdp: FakeCdp, target: str, kind: str
) -> None:
    session = _opened(cdp, target, kind=kind, session=f"S-{target}-again")
    await cdp.drain()
    assert cdp.sent("Target.detachFromTarget") == [{"sessionId": session}], "let run, and let go"
    assert cdp.sent_on("Runtime.runIfWaitingForDebugger") == [session]
    assert await tabbed.follow() is False


async def test_a_page_chromium_prerenders_is_never_followed(tabbed: Browser, cdp: FakeCdp) -> None:
    """A site's speculation rules prerender the next page as a `page` target nobody opened."""
    _opened(cdp, subtype="prerender")
    await cdp.drain()
    assert cdp.sent("Target.detachFromTarget") == [{"sessionId": "S2"}]
    assert await tabbed.follow() is False


async def test_a_new_tab_with_no_opener_is_followed_too(tabbed: Browser, cdp: FakeCdp) -> None:
    """A `target=_blank` link is `noopener` by default: the tab may name no opener at all."""
    _opened(cdp, opener=None)
    await cdp.drain()
    assert await tabbed.follow() is True
    cdp.emit("Target.targetDestroyed", {"targetId": "T2"})
    assert await tabbed.follow() is True
    await tabbed.verbs._where()  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent_on("Page.getNavigationHistory") == ["S1"], "back to the page it came from"


async def test_events_of_a_tab_in_the_background_never_reach_the_page(tabbed: Browser, cdp: FakeCdp) -> None:
    frames: list[int] = []
    tabbed.on_screencast(lambda _meta, _jpeg, ack: frames.append(ack))
    _opened(cdp)
    await cdp.drain()
    await tabbed.follow()
    cdp.emit("Page.screencastFrame", {"sessionId": 9, "data": "", "metadata": {}}, session="S1")
    cdp.emit("Page.loadEventFired", {}, session="S1")
    assert frames == [] and not tabbed.verbs._loaded.is_set()  # pyright: ignore[reportPrivateUsage]
    cdp.emit("Page.screencastFrame", {"sessionId": 10, "data": "", "metadata": {}}, session="S2")
    cdp.emit("Page.loadEventFired", {}, session="S2")
    assert frames == [10] and tabbed.verbs._loaded.is_set()  # pyright: ignore[reportPrivateUsage]


async def test_a_new_tab_answers_the_proxy_before_it_is_followed(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    _tabs(browser_with_credential, cdp)
    _opened(cdp)
    await cdp.drain()
    assert cdp.sent_on("Fetch.enable") == ["S2"]


async def test_a_paused_request_is_continued_on_the_tab_it_came_from(
    browser_with_credential: Browser, cdp: FakeCdp
) -> None:
    b = _tabs(browser_with_credential, cdp)
    _opened(cdp)
    await cdp.drain()
    await b.follow()
    cdp.emit("Fetch.requestPaused", {"requestId": "r9"}, session="S1")
    cdp.emit("Fetch.authRequired", {"requestId": "r8", "authChallenge": {"source": "Proxy"}}, session="S1")
    await cdp.drain()
    assert cdp.sent_on("Fetch.continueRequest") == ["S1"], "the opener's request never hangs"
    assert cdp.sent_on("Fetch.continueWithAuth") == ["S1"]


# ── where the page belongs: computed at the move, never left on a closed tab (review round 1) ───


def _closed(cdp: FakeCdp, target: str) -> None:
    cdp.dead.add("S" + target.removeprefix("T"))
    cdp.emit("Target.targetDestroyed", {"targetId": target})


async def _where_session(b: Browser, cdp: FakeCdp) -> str | None:
    await b.verbs._where()  # pyright: ignore[reportPrivateUsage]
    return cdp.sent_on("Page.getNavigationHistory")[-1]


async def test_the_page_goes_back_past_an_opener_that_closed_too(tabbed: Browser, cdp: FakeCdp) -> None:
    """T1 opened T2, T2 opened T3; T3 then T2 close before any verb: back to T1, never a dead tab."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T2")
    await cdp.drain()
    await tabbed.follow()
    _closed(cdp, "T3")
    _closed(cdp, "T2")
    assert tabbed.follow_pending and await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_page_that_opens_a_tab_and_closes_itself_moves_to_that_tab(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """`window.open(x); window.close()` in one task: the new tab is still being attached."""
    _opened(cdp, "T2")
    _closed(cdp, "T1")
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S2"


async def test_a_popup_that_opens_a_tab_and_closes_itself_hands_the_page_to_that_tab(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T2")
    _closed(cdp, "T2")
    await cdp.drain()
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S3"
    assert await tabbed.follow() is False, "nothing pulls the page back to T1"


async def test_a_tab_that_closes_during_the_move_leaves_the_page_where_it_was(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp, "T2")
    await cdp.drain()

    def close_after_answer(session: str | None, _params: object) -> None:
        if session == "S2":  # it answers bringToFront, then closes before the move resumes
            cdp.emit("Target.targetDestroyed", {"targetId": "T2"})

    cdp.hooks["Page.bringToFront"] = close_after_answer
    assert await tabbed.follow() is False
    assert not tabbed.follow_pending
    assert await _where_session(tabbed, cdp) == "S1"


async def test_the_page_closing_during_a_move_to_a_newer_tab_still_lands_there(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T2")
    await cdp.drain()
    cdp.hooks["Page.enable"] = lambda session, _p: _closed(cdp, "T2") if session == "S3" else None
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S3"
    assert not tabbed.follow_pending, "no leftover move back to T1"


async def test_closing_a_tab_returns_to_its_opener_not_the_newest_other_tab(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """T1 opens T2 and then T3 before the page moves: it moves to T3, and closing T3 goes back to
    T1, its opener, not to T2."""
    _opened(cdp, "T2")
    _opened(cdp, "T3", opener="T1")
    await cdp.drain()
    await tabbed.follow()
    assert await _where_session(tabbed, cdp) == "S3"
    _closed(cdp, "T3")
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_tab_a_background_tab_opens_never_takes_the_page(tabbed: Browser, cdp: FakeCdp) -> None:
    """The page moved T1 -> T2; T1, now in the background, opens T3 (a late popup, an ad): the
    page stays on T2, where the flow is, and T3 closing leaves it there."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T1")
    await cdp.drain()
    assert cdp.sent_on("Runtime.runIfWaitingForDebugger")[-1] == "S3", "still adopted, its proxy answered"
    assert not tabbed.follow_pending and await tabbed.follow() is False
    _closed(cdp, "T3")
    assert await tabbed.follow() is False
    assert await _where_session(tabbed, cdp) == "S2"


async def test_a_tab_opened_by_a_tab_still_being_attached_returns_to_that_tab(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp, "T2")
    _opened(cdp, "T3", opener="T2")  # before T2's attach answered
    await cdp.drain()
    await tabbed.follow()
    _closed(cdp, "T3")
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S2"


async def test_a_move_waits_for_a_tab_whose_setup_is_still_on_the_wire(tabbed: Browser, cdp: FakeCdp) -> None:
    gate = cdp.gates["Runtime.runIfWaitingForDebugger"] = asyncio.Event()
    _opened(cdp, "T2")
    move = asyncio.create_task(tabbed.follow())
    await cdp.drain()
    gate.set()
    assert await move is True
    assert await _where_session(tabbed, cdp) == "S2"


async def test_after_a_move_a_navigation_of_the_new_tab_is_waited_for(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    cdp.emit("Page.frameStartedLoading", {"frameId": "F2"}, session="S2")
    assert tabbed.verbs._navigating  # pyright: ignore[reportPrivateUsage]


def _times_out(cdp: FakeCdp, times: int, session_id: str = "S2") -> None:
    """`Page.enable` on `session_id` times out `times` times, then answers."""
    left = [times]

    def slow(session: str | None, _params: object) -> None:
        if session == session_id and left[0] > 0:
            left[0] -= 1
            raise TimeoutError

    cdp.hooks["Page.enable"] = slow


async def test_a_tab_that_answers_slowly_is_tried_again(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    _times_out(cdp, 2)
    assert [await tabbed.follow() for _ in range(3)] == [False, False, True]
    assert await _where_session(tabbed, cdp) == "S2"


async def test_a_tab_that_never_answers_is_left_after_three_tries(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    _times_out(cdp, 99)
    assert [await tabbed.follow() for _ in range(4)] == [False, False, False, False]
    assert cdp.sent_on("Page.enable").count("S2") == 3, "three tries, then the page stays"
    assert not tabbed.follow_pending
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_page_whose_tab_closed_keeps_trying_a_slow_opener(tabbed: Browser, cdp: FakeCdp) -> None:
    """The popup T2 closes while its opener T1 is too slow for three moves: a slow tab beats a
    dead one, so the page keeps trying T1 and lands there once it answers - never left on the
    closed tab, where every verb is refused until Chromium restarts."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _closed(cdp, "T2")
    _times_out(cdp, 3, "S1")
    assert [await tabbed.follow() for _ in range(3)] == [False, False, False]
    assert tabbed.follow_pending and await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_tab_that_moved_after_slow_answers_starts_its_tries_afresh(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """T2 was slow twice before the page moved there. Later its popup T3 closes and T2 is slow
    once more: its first failure since it answered, so the page still goes back to T2, where the
    flow continues - not past it to T1."""
    _opened(cdp, "T2")
    await cdp.drain()
    _times_out(cdp, 2)
    assert [await tabbed.follow() for _ in range(3)] == [False, False, True]
    _opened(cdp, "T3", opener="T2")
    await cdp.drain()
    assert await tabbed.follow() is True
    _closed(cdp, "T3")
    _times_out(cdp, 1)
    assert [await tabbed.follow() for _ in range(2)] == [False, True]
    assert await _where_session(tabbed, cdp) == "S2"


async def test_a_click_that_opens_a_tab_reports_the_tab_it_opened(tabbed: Browser, cdp: FakeCdp) -> None:
    tabbed.verbs._refs = {"e1.1": 7}  # pyright: ignore[reportPrivateUsage]
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, params: (
        _opened(cdp, "T2") if (params or {}).get("type") == "mouseReleased" else None
    )
    result = await tabbed.handle({"verb": "click", "ref": "e1.1"})
    assert result["ok"] is True and result["tab"] == "opened"
    assert await _where_session(tabbed, cdp) == "S2"


async def test_an_action_asked_for_the_page_returns_the_tab_it_moved_to(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """The page an action brings back is the one the model's next call acts on: a click that
    opened a tab returns THAT tab's tree, its references live - never the tab it left, whose
    references the move made stale."""
    tabbed.verbs._refs = {"e1.1": 7}  # pyright: ignore[reportPrivateUsage]
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, params: (
        _opened(cdp, "T2") if (params or {}).get("type") == "mouseReleased" else None
    )
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON}, on="S2")
    result = await tabbed.handle({"verb": "click", "ref": "e1.1", "snapshot": True})
    assert result["ok"] is True and result["tab"] == "opened"
    assert cdp.sent_on("Accessibility.getFullAXTree") == ["S2"]
    ref = _ref_for(result["text"], 'button "Save changes"')
    assert tabbed.verbs._refs == {ref: 42}  # pyright: ignore[reportPrivateUsage]


async def test_a_click_that_closes_the_pages_tab_succeeds_and_reports_the_opener(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """A sign-in popup's last click closes it: the click happened, and the page is the opener."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    tabbed.verbs._refs = {"e2.1": 7}  # pyright: ignore[reportPrivateUsage]

    def close_on_release(_session: str | None, params: dict[str, Any] | None) -> None:
        if (params or {}).get("type") == "mouseReleased":
            cdp.dead.add("S2")
            cdp.emit("Target.detachedFromTarget", {"sessionId": "S2", "targetId": "T2"})

    cdp.hooks["Input.dispatchMouseEvent"] = close_on_release
    result = await tabbed.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is True and result["tab"] == "closed"
    assert await _where_session(tabbed, cdp) == "S1"


# ── review round 2: a tab closing mid-call, honest results, a stopped browser ─────────────────────


async def test_a_move_lands_past_a_tab_that_closes_during_it(tabbed: Browser, cdp: FakeCdp) -> None:
    """The page's tab closed, and the newer tab it opened closes while the page moves to it: the
    same move goes on to the opener, so no verb ever runs on a closed tab."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T2")
    await cdp.drain()
    _closed(cdp, "T2")

    def close_after_answer(session: str | None, _params: object) -> None:
        if session == "S3":
            cdp.emit("Target.targetDestroyed", {"targetId": "T3"})

    cdp.hooks["Page.bringToFront"] = close_after_answer
    assert await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_verb_with_no_tab_left_to_go_to_is_never_reported_done(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _closed(cdp, "T1")
    _closed(cdp, "T2")  # every tab gone: an error, never "ok, the tab closed"
    result = await tabbed.handle({"verb": "open", "url": "http://127.0.0.1:8088/two.html"})
    assert result["ok"] is False


async def test_a_tab_that_closes_after_the_verb_looked_says_closed(tabbed: Browser, cdp: FakeCdp) -> None:
    """The popup closes while the action's picture is taken: the page goes back to the opener and
    the result says the tab closed, never that one opened."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    tabbed.verbs._refs = {"e2.1": 7}  # pyright: ignore[reportPrivateUsage]
    cdp.hooks["Page.captureScreenshot"] = lambda session, _p: _closed(cdp, "T2") if session == "S2" else None
    result = await tabbed.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is True and result["tab"] == "closed"
    assert await _where_session(tabbed, cdp) == "S1"


async def test_a_tab_given_up_on_leaves_the_page_in_front(tabbed: Browser, cdp: FakeCdp) -> None:
    """The new tab never answers: the page stays, and it is brought back to the front, so the
    screen shows the page verbs act on instead of a frozen one."""
    _opened(cdp, "T2")
    await cdp.drain()
    _times_out(cdp, 99)
    for _ in range(3):
        await tabbed.follow()
    await cdp.drain()
    assert cdp.sent_on("Page.bringToFront") == ["S1"]


async def test_a_stopped_browser_has_no_tab_to_move_to(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    assert tabbed.follow_pending
    await tabbed.stop()
    assert not tabbed.follow_pending


async def test_a_tab_opened_during_a_move_is_the_next_move(tabbed: Browser, cdp: FakeCdp) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    cdp.hooks["Page.enable"] = lambda session, _p: (
        _opened(cdp, "T3", opener="T2") if session == "S2" else None
    )
    assert await tabbed.follow() is True
    cdp.hooks.clear()
    await cdp.drain()
    assert tabbed.follow_pending and await tabbed.follow() is True
    assert await _where_session(tabbed, cdp) == "S3"


async def test_a_click_that_opens_a_tab_reports_that_tabs_address(tabbed: Browser, cdp: FakeCdp) -> None:
    cdp.answer(
        "Page.getNavigationHistory",
        {"currentIndex": 0, "entries": [{"url": "http://127.0.0.1:8088/closer.html", "title": "Closer"}]},
        on="S2",
    )
    tabbed.verbs._refs = {"e1.1": 7}  # pyright: ignore[reportPrivateUsage]
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, params: (
        _opened(cdp, "T2") if (params or {}).get("type") == "mouseReleased" else None
    )
    result = await tabbed.handle({"verb": "click", "ref": "e1.1"})
    assert (result["tab"], result["url"], result["title"]) == (
        "opened",
        "http://127.0.0.1:8088/closer.html",
        "Closer",
    )


async def test_a_tab_that_closes_before_the_action_reports_nothing_done(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """The tab closed on its own (a page timer) before the click's input was sent: the click did
    not happen, and the result says so - only a verb whose action landed is done."""
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    tabbed.verbs._refs = {"e2.1": 7}  # pyright: ignore[reportPrivateUsage]
    cdp.hooks["DOM.getBoxModel"] = lambda session, _p: _closed(cdp, "T2") if session == "S2" else None
    result = await tabbed.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is False and result["tab"] == "closed"
    assert cdp.sent_on("Input.dispatchMouseEvent") == [], "nothing was clicked"
    assert await _where_session(tabbed, cdp) == "S1"


# ── the page's origin: the screen's address line, stated by Chromium, never by the agent ──────────


def _navigated(cdp: FakeCdp, target: str, url: str) -> None:
    """Chromium reporting a tab's new URL (a navigation committed)."""
    cdp.emit("Target.targetInfoChanged", {"targetInfo": {"targetId": target, "type": "page", "url": url}})


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://user:pw@Accounts.Example.com/login?next=/x#top", "https://accounts.example.com"),
        ("http://127.0.0.1:8088/index.html", "http://127.0.0.1:8088"),
        ("http://[::1]:9000/", "http://[::1]:9000"),
        ("about:blank", ""),
        ("data:text/html,<h1>Sign in to Google</h1>", ""),
        ("chrome-error://chromewebdata/", ""),
    ],
)
async def test_the_origin_is_scheme_host_and_port_of_a_web_page_only(
    tabbed: Browser, cdp: FakeCdp, url: str, origin: str
) -> None:
    _navigated(cdp, "T1", url)
    assert tabbed.origin == origin


async def test_the_origin_follows_the_page_across_tabs_and_ends_with_the_browser(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """A tab the page opens becomes the page, so the origin is the page's tab's - not the newest
    navigation's: a tab in the background navigating never moves it."""
    assert tabbed.origin == ""
    _navigated(cdp, "T1", "https://admin.example.com/users")
    _opened(cdp, "T2")
    await cdp.drain()
    _navigated(cdp, "T2", "https://accounts.example.org:8443/sign-in")
    assert tabbed.origin == "https://admin.example.com", "the page has not moved yet"
    assert await tabbed.follow() is True
    assert tabbed.origin == "https://accounts.example.org:8443"
    _navigated(cdp, "T1", "https://elsewhere.example.net/")
    assert tabbed.origin == "https://accounts.example.org:8443"
    cdp.emit("Target.targetDestroyed", {"targetId": "T2"})
    assert await tabbed.follow() is True
    assert tabbed.origin == "https://elsewhere.example.net"
    await tabbed.stop()
    assert tabbed.origin == ""


# ── what a person's input holds down is let go when their control ends ─────────────────────────


async def test_release_lets_go_of_every_key_and_button_a_person_holds(browser: Browser, cdp: FakeCdp) -> None:
    """Held keys and buttons are released when driving ends - also when the
    server ends it (an owner's End, an expiry, a re-claim elsewhere), which the page never hears."""
    await browser.pointer({"type": "down", "x": 10, "y": 20, "button": "left", "buttons": 1})
    await browser.pointer({"type": "move", "x": 30, "y": 40, "buttons": 1})  # a drag
    await browser.key({"type": "down", "key": "Shift", "code": "ShiftLeft", "keyCode": 16})
    await browser.key({"type": "down", "key": "a", "code": "KeyA", "text": "A", "keyCode": 65})
    await browser.key({"type": "up", "key": "a", "code": "KeyA", "keyCode": 65})
    before = len(cdp.calls)
    await browser.release_all()
    released = cdp.calls[before:]
    assert released == [
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Shift",
                "code": "ShiftLeft",
                "modifiers": 0,
                "windowsVirtualKeyCode": 16,
            },
        ),
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": 30.0,
                "y": 40.0,
                "button": "left",
                "buttons": 0,
                "modifiers": 0,
                "clickCount": 1,
            },
        ),
    ]
    await browser.release_all()
    assert len(cdp.calls) == before + 2, "nothing is held now"


async def test_a_tab_whose_detach_names_only_its_session_is_closed_too(tabbed: Browser, cdp: FakeCdp) -> None:
    """`Target.detachedFromTarget` names the session; its `targetId` is deprecated and may be
    absent. A closing tab known only by its session was ignored, and the page stayed on it,
    refusing every verb ("browser refused")."""
    _opened(cdp, "T2")
    await cdp.drain()
    assert await tabbed.follow() is True
    cdp.dead.add("S2")
    cdp.emit("Target.detachedFromTarget", {"sessionId": "S2"})
    assert await tabbed.follow() is True
    await tabbed.verbs._where()  # pyright: ignore[reportPrivateUsage]
    assert cdp.sent_on("Page.getNavigationHistory")[-1] == "S1", "back on the opener"


# ── a page the browser cannot reach: the screen says so, and a person can go back ──────────────


def _failed(cdp: FakeCdp, session: str, url: str | None) -> None:
    """Chromium committing the page's main frame: its own error page for `url` (what a proxy's
    refusal shows, `unreachableUrl` set, seen live), or a page that loaded (`url` None)."""
    frame: dict[str, Any] = {
        "id": "F",
        "url": "chrome-error://chromewebdata/" if url else "https://ok.example/",
    }
    if url:
        frame["unreachableUrl"] = url
    cdp.emit("Page.frameNavigated", {"frame": frame}, session=session)


async def test_a_page_the_browser_could_not_reach_is_stated_by_its_origin(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    tabbed.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    assert tabbed.unreachable == ""
    _failed(cdp, "S1", "https://refused.example/a/path?q=secret")
    assert tabbed.unreachable == "https://refused.example", "an origin: never the path or the query"
    cdp.emit(
        "Page.frameNavigated",
        {"frame": {"id": "C", "parentId": "F", "url": "https://x.example/"}},
        session="S1",
    )
    assert tabbed.unreachable == "https://refused.example", "a subframe is not the page"
    _failed(cdp, "S1", None)
    assert tabbed.unreachable == ""


async def test_the_unreachable_page_is_the_pages_tabs_and_a_moved_to_tab_is_read_at_the_move(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """A `target=_blank` link to a refused host: the new tab fails before the page reaches it (no
    `Page` events from a tab in the background), so the move reads its frame tree."""
    tabbed.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    _opened(cdp, "T2")
    await cdp.drain()
    cdp.answer(
        "Page.getFrameTree",
        {"frameTree": {"frame": {"id": "F2", "unreachableUrl": "https://refused.example/"}}},
        on="S2",
    )
    assert tabbed.unreachable == ""
    assert await tabbed.follow() is True
    assert tabbed.unreachable == "https://refused.example"
    _closed(cdp, "T2")
    assert await tabbed.follow() is True
    assert tabbed.unreachable == "", "back on the opener, which loaded"


async def test_back_returns_to_the_previous_page_or_closes_a_tab_with_none(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """The screen's way back from a page it cannot reach: never recorded, never a verb."""
    tabbed.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    cdp.answer(
        "Page.getNavigationHistory",
        {"currentIndex": 1, "entries": [{"id": 4, "url": "https://ok.example/"}, {"id": 5, "url": "x"}]},
    )
    await tabbed.go_back()
    assert cdp.sent("Page.navigateToHistoryEntry") == [{"entryId": 4}] and not cdp.sent("Target.closeTarget")
    _opened(cdp, "T2")
    await cdp.drain()
    assert await tabbed.follow() is True
    cdp.answer("Page.getNavigationHistory", {"currentIndex": 0, "entries": [{"id": 9, "url": "x"}]})
    await tabbed.go_back()
    assert cdp.sent("Target.closeTarget") == [{"targetId": "T2"}], "a tab the page opened closes"
    _closed(cdp, "T2")
    assert await tabbed.follow() is True
    await tabbed.go_back()
    assert cdp.sent("Target.closeTarget") == [{"targetId": "T2"}], "the last tab is never closed"


# ── review round 3: a closing tab never holds the lock, and "done" means the action landed ────────


async def _on_tab_two(b: Browser, cdp: FakeCdp) -> Browser:
    _opened(cdp, "T2")
    await cdp.drain()
    await b.follow()
    b.verbs._refs = {"e2.1": 7}  # pyright: ignore[reportPrivateUsage]
    return b


def _close_on(cdp: FakeCdp, method: str, *, when: Any = None) -> None:
    """The page's tab (T2) closes as `method` reaches it on S2 (that call itself is refused)."""

    def hook(session: str | None, params: dict[str, Any] | None) -> None:
        if session == "S2" and (when is None or when(params or {})):
            _closed(cdp, "T2")

    cdp.hooks[method] = hook


async def test_an_open_into_a_tab_that_closes_while_loading_returns_at_once_undone(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    """Measured live: the next `open` navigated a popup about to close and held the verb lock
    10 s waiting for a load that never came, then called it done."""
    b = await _on_tab_two(tabbed, cdp)

    def start_loading_then_close(session: str | None, _params: object) -> None:
        if session == "S2":
            cdp.emit("Page.frameStartedLoading", {"frameId": "F2"}, session="S2")
            asyncio.get_running_loop().call_later(0.1, _closed, cdp, "T2")

    cdp.hooks["Page.navigate"] = start_loading_then_close
    started = time.monotonic()
    result = await asyncio.wait_for(b.handle({"verb": "open", "url": "http://127.0.0.1:8088/two.html"}), 5)
    assert time.monotonic() - started < 2.0
    assert result["ok"] is False and result["tab"] == "closed"
    assert await _where_session(b, cdp) == "S1"


async def test_a_tab_that_closes_as_the_pointer_presses_is_not_clicked(tabbed: Browser, cdp: FakeCdp) -> None:
    """The pointer's move landed, the press did not: nothing was clicked."""
    b = await _on_tab_two(tabbed, cdp)
    _close_on(cdp, "Input.dispatchMouseEvent", when=lambda p: p.get("type") == "mousePressed")
    result = await b.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is False and result["tab"] == "closed"


async def test_a_tab_that_closes_between_clearing_and_typing_is_not_typed(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    b = await _on_tab_two(tabbed, cdp)
    _close_on(cdp, "Input.insertText")
    result = await b.handle({"verb": "type", "ref": "e2.1", "text": "hello"})
    assert result["ok"] is False and result["tab"] == "closed"


@pytest.mark.parametrize(
    "cmd",
    [{"verb": "press", "key": "Enter"}, {"verb": "type", "ref": "e2.1", "text": "hello"}],
    ids=["press", "type"],
)
async def test_a_key_or_text_that_landed_before_the_tab_closed_is_done(
    tabbed: Browser, cdp: FakeCdp, cmd: dict[str, Any]
) -> None:
    b = await _on_tab_two(tabbed, cdp)
    _close_on(cdp, "Page.getNavigationHistory")  # the verb's last look, after its action
    result = await b.handle(cmd)
    assert result["ok"] is True and result["tab"] == "closed"


_TWO_FIELDS = [{"ref": "e2.1", "value": "ada"}, {"ref": "e2.2", "value": "hunter2"}]


@pytest.mark.parametrize(
    ("cmd", "method", "when"),
    [
        ({"verb": "fill", "fields": _TWO_FIELDS}, "Input.insertText", {"text": "hunter2"}),
        (
            {"verb": "fill", "fields": _TWO_FIELDS, "submit": True},
            "Input.dispatchKeyEvent",
            {"type": "keyDown"},
        ),
        (
            {"verb": "type", "ref": "e2.1", "text": "ada", "submit": True},
            "Input.dispatchKeyEvent",
            {"type": "keyDown"},
        ),
    ],
    ids=["fill-second-field", "fill-submit", "type-submit"],
)
async def test_a_verb_whose_tab_closed_before_its_last_part_is_not_done(
    tabbed: Browser, cdp: FakeCdp, cmd: dict[str, Any], method: str, when: dict[str, str]
) -> None:
    """A verb in parts acts with its last one - the last field's text, or the Enter that submits:
    a tab that closes before it landed leaves the form half-filled or unsent, never done."""
    b = await _on_tab_two(tabbed, cdp)
    b.verbs._refs["e2.2"] = 8  # pyright: ignore[reportPrivateUsage]
    _close_on(cdp, method, when=lambda p: all(p.get(k) == v for k, v in when.items()))
    result = await b.handle(cmd)
    assert result["ok"] is False and result["tab"] == "closed"


async def test_a_submit_that_landed_before_the_tab_closed_is_done(tabbed: Browser, cdp: FakeCdp) -> None:
    """The sign-in popup closes on its own Enter: the key went down, so the form was sent."""
    b = await _on_tab_two(tabbed, cdp)
    b.verbs._refs["e2.2"] = 8  # pyright: ignore[reportPrivateUsage]
    _close_on(cdp, "Input.dispatchKeyEvent", when=lambda p: p.get("type") == "keyUp")
    result = await b.handle({"verb": "fill", "fields": _TWO_FIELDS, "submit": True})
    assert result["ok"] is True and result["tab"] == "closed"


async def test_an_earlier_verb_that_acted_never_makes_a_later_one_done(tabbed: Browser, cdp: FakeCdp) -> None:
    b = await _on_tab_two(tabbed, cdp)
    assert (await b.handle({"verb": "press", "key": "a"}))["ok"] is True
    _close_on(cdp, "DOM.getBoxModel")  # before this click's pointer
    result = await b.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is False


async def test_an_action_whose_tab_closed_with_no_tab_left_is_an_error(tabbed: Browser, cdp: FakeCdp) -> None:
    b = await _on_tab_two(tabbed, cdp)
    _closed(cdp, "T1")
    _close_on(cdp, "Page.getNavigationHistory")  # the click landed, then the last tab closed
    result = await b.handle({"verb": "click", "ref": "e2.1"})
    assert result["ok"] is False


async def test_a_tab_whose_move_call_fails_as_it_closes_is_stepped_past(
    tabbed: Browser, cdp: FakeCdp
) -> None:
    _opened(cdp, "T2")
    await cdp.drain()
    await tabbed.follow()
    _opened(cdp, "T3", opener="T2")
    await cdp.drain()
    _closed(cdp, "T2")
    cdp.hooks["Page.bringToFront"] = lambda session, _p: _closed(cdp, "T3") if session == "S3" else None
    assert await tabbed.follow() is True, "a close is not a failure: the same move goes on"
    assert await _where_session(tabbed, cdp) == "S1"


@pytest.mark.parametrize(
    ("cmd", "last"),
    [
        ({"verb": "scroll", "direction": "down"}, "Page.getNavigationHistory"),
        ({"verb": "select", "ref": "e2.1", "value": "US"}, "Page.getNavigationHistory"),
    ],
    ids=["scroll", "select"],
)
async def test_a_wheel_or_a_value_that_landed_before_the_tab_closed_is_done(
    tabbed: Browser, cdp: FakeCdp, cmd: dict[str, Any], last: str
) -> None:
    b = await _on_tab_two(tabbed, cdp)
    _close_on(cdp, last)  # after the wheel or the value, at the verb's last look
    result = await b.handle(cmd)
    assert result["ok"] is True and result["tab"] == "closed"


async def test_a_snapshot_with_find_mints_references_for_the_matches_only(
    browser: Browser, cdp: FakeCdp
) -> None:
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_TWO_FORMS})
    snap = await browser.handle({"verb": "snapshot", "find": "delete"})
    assert snap["text"] == '[e1.1] textbox "Type DELETE to confirm"'
    assert browser.verbs._refs == {"e1.1": 62}  # pyright: ignore[reportPrivateUsage]


async def test_an_action_asked_for_the_page_returns_it_in_a_new_epoch(
    browser: Browser, cdp: FakeCdp, snapshot_with_button: str
) -> None:
    acted = await browser.handle({"verb": "click", "ref": snapshot_with_button, "snapshot": True})
    assert acted["ok"] and acted["epoch"] == 2 and acted["text"] == '[e2.1] button "Save changes"'
    assert browser.verbs._refs == {"e2.1": 42}  # pyright: ignore[reportPrivateUsage]
    plain = await browser.handle({"verb": "click", "ref": "e2.1"})
    assert plain["ok"] and "text" not in plain, "an action not asked for the page leaves the epoch alone"


async def test_a_page_that_cannot_be_read_after_an_action_never_fails_the_action(
    browser: Browser, cdp: FakeCdp, snapshot_with_button: str
) -> None:
    """The click happened; a tree that cannot be read right after it (a frame detached by the
    navigation it caused) only means the model has to look again."""

    def detached(params: dict[str, Any]) -> dict[str, Any]:
        raise CdpError(-32000, "No frame with given id found")

    cdp.answer("Accessibility.getFullAXTree", detached)
    acted = await browser.handle({"verb": "click", "ref": snapshot_with_button, "snapshot": True})
    assert acted["ok"] is True and "text" not in acted


# ── whole-value inputs: a date or a time picker takes its value set, never typed ───────────────


def _time_field(params: dict[str, Any]) -> dict[str, Any]:
    """The page's answer for an <input type=time>: Chromium keeps a value only in HH:MM."""
    if params.get("functionDeclaration") != WHOLE_VALUE_JS:
        return {"result": {"value": {"form_labels": [], "submit_name": ""}}}
    v = params["arguments"][0]["value"]
    ok = len(v) == 5 and v[2] == ":" and v.replace(":", "").isdigit()
    return {"result": {"value": {"whole": True, "type": "time", "value": v if ok else ""}}}


async def test_a_time_field_takes_its_value_whole_and_no_keystroke(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    """Typed digits never land in a time picker (`Input.insertText` is
    ignored), so the computer spent twenty keystrokes and never set a delivery time."""
    cdp.answer("Runtime.callFunctionOn", _time_field)
    done = await browser.handle({"verb": "type", "ref": snapshot_with_form, "text": "19:30"})
    assert done["ok"] is True
    assert cdp.sent("Input.insertText") == []
    sent = [p for p in cdp.sent("Runtime.callFunctionOn") if p and p["functionDeclaration"] == WHOLE_VALUE_JS]
    assert sent[-1] is not None and sent[-1]["arguments"] == [{"value": "19:30"}], (
        "the value is data, never code"
    )


async def test_a_time_in_the_wrong_format_is_an_error_naming_the_format(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    cdp.answer("Runtime.callFunctionOn", _time_field)
    typed = await browser.handle({"verb": "type", "ref": snapshot_with_form, "text": "7:30 PM"})
    assert typed["ok"] is False and "HH:MM, 24-hour" in typed["error"]
    filled = await browser.handle(
        {"verb": "fill", "fields": [{"ref": snapshot_with_form, "value": "7:30 PM"}], "submit": True}
    )
    assert filled["ok"] is False and "HH:MM, 24-hour" in filled["error"]
    assert not any(m == "Input.dispatchKeyEvent" for m, _ in cdp.calls), (
        "a field left wrong is never submitted"
    )


async def test_an_ordinary_field_is_still_typed(
    browser: Browser, cdp: FakeCdp, snapshot_with_form: str
) -> None:
    done = await browser.handle({"verb": "type", "ref": snapshot_with_form, "text": "a@b.com"})
    assert done["ok"] is True and cdp.sent("Input.insertText") == [{"text": "a@b.com"}]
