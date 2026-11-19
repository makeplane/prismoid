# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A browser frame, end to end in a real container: the sidecar answers, the agent user is
locked out of the profile, the recording lands where harvest reads."""

from __future__ import annotations

import io
import json
import re
import shlex
import uuid

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.tools.browser_policy import consent

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-d")]

FIXTURE = """<!doctype html><title>Orders</title><h1>Orders</h1>
<form><input name=q placeholder=Search aria-label=Search><input type=password aria-label=Password value=hunter2>
<button type=button id=save onclick="document.title='Saved'">Save changes</button></form>
<a href="/two.html">Delete all</a>"""


@pytest.fixture
async def frame():
    ensure_image("browser")
    f = DockerFrame(task_id=f"pytest-browser-{uuid.uuid4().hex[:8]}", profile="browser")
    await f.create()
    await f.write_file("/work/site/index.html", FIXTURE)
    await f.write_file("/work/site/two.html", "<title>Two</title><h1>Two</h1>")
    await f.exec(
        "cd /work/site && (python3 -m http.server 8088 >/dev/null 2>&1 &) && "
        "for i in $(seq 1 40); do curl -sf http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
    )
    yield f
    await f.destroy()


TABS = '<!doctype html><title>Tabs</title><a href="/closer.html" target="_blank">Open the closer</a>'
CLOSER = '<!doctype html><title>Closer</title><button onclick="window.close()">Close this tab</button>'


def _ref(snap: dict[str, object], needle: str) -> str:
    """The reference right before `needle` on its line: the look puts a small form on one line."""
    line = next(line for line in str(snap["text"]).splitlines() if needle in line)
    return re.findall(r"\[(e\d+\.\d+)\]", line[: line.index(needle)])[-1]


async def test_a_tab_the_page_opens_is_followed_and_its_closing_comes_back(frame: DockerFrame) -> None:
    """A `target=_blank` link put a new tab in front and the sidecar
    stayed on the page left behind, hidden - the screen froze and input landed unseen."""
    await frame.write_file("/work/site/tabs.html", TABS)
    await frame.write_file("/work/site/closer.html", CLOSER)
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/tabs.html"})
    tabs = await frame.browser({"verb": "snapshot"})
    clicked = await frame.browser({"verb": "click", "ref": _ref(tabs, 'link "Open the closer"')})
    assert clicked["ok"] and clicked["tab"] == "opened", clicked  # the click ends on the new tab
    await frame.browser({"verb": "wait", "for": "Close this tab"})  # its load, not the click's
    opened = await frame.browser({"verb": "snapshot"})
    assert opened["ok"] and opened["title"] == "Closer", opened
    assert str(opened["url"]).endswith("/closer.html")
    closed = await frame.browser({"verb": "click", "ref": _ref(opened, 'button "Close this tab"')})
    assert closed["ok"], closed  # a click that closes its own tab happened: never "refused"
    assert closed.get("tab", "closed") == "closed", closed
    back = await frame.browser({"verb": "snapshot"})
    assert back["ok"] and back["title"] == "Tabs", back


async def test_a_stale_reference_is_refused_after_a_new_snapshot(frame: DockerFrame) -> None:
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
    first = await frame.browser({"verb": "snapshot"})
    ref = _ref(first, 'link "Delete all"')
    await frame.browser({"verb": "snapshot"})  # epoch 2 invalidates e1.*
    res = await frame.browser({"verb": "click", "ref": ref})
    assert res["ok"] is False and "stale reference" in res["error"]


async def test_the_recording_lands_in_artifacts_and_names_no_typed_text(frame: DockerFrame) -> None:
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
    snap = await frame.browser({"verb": "snapshot"})
    q = _ref(snap, 'textbox "Search"')
    await frame.browser({"verb": "type", "ref": q, "text": "secret widget"})
    rows = json.loads(await frame.read_file("/artifacts/recording-1/actions.json"))
    assert [r["verb"] for r in rows] == ["open", "type"]
    assert rows[1]["target"] == 'textbox "Search"' and "secret" not in json.dumps(rows)
    listing = await frame.list_dir("/artifacts/recording-1")
    assert "0001-open.jpg" in listing and "0002-type.jpg" in listing


async def test_a_later_step_records_into_its_own_folder_on_the_warm_sidecar(frame: DockerFrame) -> None:
    """A warm Machine's next step binds the same frame naming another recording folder: the step
    boundary's `record` moves the running sidecar there, so that step's `finalize` and its copy
    out of /artifacts find its own folder."""
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
    assert (await frame.browser({"verb": "finalize"}))["path"] == "/artifacts/recording-1"
    later = DockerFrame(task_id=frame.task_id, profile="browser", name=frame.name, recording="bbbbbbbb-1")
    await later.bind()
    assert await later.browser({"verb": "record"}) == {"ok": True}
    await later.browser({"verb": "open", "url": "http://127.0.0.1:8088/two.html"})
    done = await later.browser({"verb": "finalize"})
    assert done["ok"] and done["acting"] == 1 and done["path"] == later.recording_dir
    assert (await later.exec(f"cp -r {later.recording_dir} /work/recording-copy")).ok


async def test_the_agent_user_cannot_reach_the_profile_or_the_socket(frame: DockerFrame) -> None:
    await frame.browser({"verb": "ping"})
    cookies = await frame.exec("ls /browser/profile")
    assert cookies.exit_code != 0 and "Permission denied" in cookies.stderr
    probe = await frame.exec(
        "python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/browser/sidecar.sock')\""
    )
    assert probe.exit_code != 0 and "Permission denied" in probe.stderr


async def test_refused_host_is_a_result_not_a_page(frame: DockerFrame) -> None:
    opened = await frame.browser({"verb": "open", "url": "https://example.com/"})
    assert opened == {"ok": False, "error": "egress refused", "blocked": "egress"}


async def test_inspect_expect_and_finalize_round_trip(frame: DockerFrame) -> None:
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
    snap = await frame.browser({"verb": "snapshot"})
    save_ref = _ref(snap, 'button "Save changes"')
    info = await frame.browser({"verb": "inspect", "ref": save_ref})
    assert info["ok"] and info["name"] == "Save changes" and info["host"] == "127.0.0.1"
    # Run ON the element (UI check D3): the form's labels reach the submit danger check.
    assert info["form_labels"] == ["Search", "Password"] and info["submit_name"] == ""
    wrong = await frame.browser({"verb": "click", "ref": save_ref, "expect": {"name": "Delete all"}})
    assert wrong["ok"] is False and wrong["error"] == "intervened"
    right = await frame.browser(
        {
            "verb": "click",
            "ref": save_ref,
            "expect": {"role": "button", "name": "Save changes", "host": "127.0.0.1", "path": "/index.html"},
        }
    )
    assert right["ok"]
    done = await frame.browser({"verb": "finalize"})
    assert done["ok"] and done["actions"] >= 2 and done["path"] == frame.recording_dir
    index = await frame.read_file(f"{frame.recording_dir}/index.html")
    assert "data:image/jpeg;base64," in index


FORMS = """<!doctype html><title>Records</title>
<form><input name=q aria-label=Search><button>Search</button></form>
<select aria-label="Bulk action"><option value="">Choose</option><option value="3">Delete selected</option></select>
<form><input aria-label="Type DELETE to confirm"><button type=submit>Delete my account</button></form>
<form><input type=email aria-label=Email><input type=password aria-label=Password>
<button type=button>Show password</button><input type=submit value="Log in"></form>"""


async def test_inspect_reads_the_form_and_the_option_a_value_picks(frame: DockerFrame) -> None:
    """The danger check reads what `inspect` says about a target's form and a select's choice -
    only a real Chromium runs the page script that says it."""
    await frame.write_file("/work/site/forms.html", FORMS)
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/forms.html"})
    snap = await frame.browser({"verb": "snapshot"})

    def ref(needle: str) -> str:
        return _ref(snap, needle)

    confirm = await frame.browser({"verb": "inspect", "ref": ref('textbox "Type DELETE to confirm"')})
    assert confirm["ok"] and confirm["submit_name"] == "Delete my account"
    assert confirm["form_labels"] == ["Type DELETE to confirm"]
    bulk = await frame.browser({"verb": "inspect", "ref": ref('combobox "Bulk action"'), "value": "3"})
    assert bulk["ok"] and bulk["option"] == "Delete selected" and bulk["submit_name"] == ""
    assert confirm["submits"] is False and bulk["submits"] is False


async def test_a_batched_inspect_answers_each_target_as_one_inspect_would(frame: DockerFrame) -> None:
    """A fill's targets are inspected in one call (`refs`): each answer is the one a lone inspect
    gives, in order, and one target that cannot be read refuses the whole batch."""
    await frame.write_file("/work/site/forms.html", FORMS)
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/forms.html"})
    snap = await frame.browser({"verb": "snapshot"})
    refs = [_ref(snap, 'textbox "Email"'), _ref(snap, 'textbox "Type DELETE to confirm"')]
    batch = await frame.browser({"verb": "inspect", "refs": refs})
    lone = [await frame.browser({"verb": "inspect", "ref": ref}) for ref in refs]
    assert batch["ok"] and batch["each"] == lone, batch
    stale = await frame.browser({"verb": "inspect", "refs": [refs[0], "e999.1"]})
    assert not stale["ok"] and "stale reference" in str(stale["error"]), stale


async def test_inspect_says_which_click_submits_its_form(frame: DockerFrame) -> None:
    """A click on a submit input, or on a button the page gave no type, sends its form - the
    danger check then reads that form as it would for an Enter; a `type=button` toggle in the
    same form sends nothing. Only a real Chromium's `type` reflection says which is which."""
    await frame.write_file("/work/site/forms.html", FORMS)
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/forms.html"})
    snap = await frame.browser({"verb": "snapshot"})

    async def inspect(needle: str) -> dict[str, object]:
        ref = _ref(snap, needle)
        info = await frame.browser({"verb": "inspect", "ref": ref})
        assert info["ok"], info
        return info

    log_in = await inspect('button "Log in"')
    assert log_in["submits"] is True and log_in["submit_name"] == "Log in"
    assert log_in["form_labels"] == ["Email", "Password"]
    toggle = await inspect('button "Show password"')
    assert toggle["submits"] is False and toggle["submit_name"] == "Log in"
    search = await inspect('button "Search"')  # no type attribute: a submit button all the same
    assert search["submits"] is True and search["submit_name"] == "Search"


SHOP = """<!doctype html><title>Product</title>
<form action=/two.html><label>Quantity <select name=quantity><option>1</option><option>2</option></select></label>
<input type=hidden name=asin value=B0TEST>
<input type=submit name="submit.add-to-cart" value="Add to cart">
<input type=submit name="submit.buy-now" aria-label="Buy Now" style="display:none"></form>
<form action=/two.html><input type=submit value="Place your order"></form>"""


async def test_add_to_cart_acts_while_place_your_order_asks(frame: DockerFrame) -> None:
    """A product form's hidden Buy Now is another act, so clicking its Add to cart reads only
    the fields a person fills; the order form's own submit still names the order."""
    await frame.write_file("/work/site/shop.html", SHOP)
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/shop.html"})
    snap = await frame.browser({"verb": "snapshot"})

    async def irreversible(needle: str) -> bool:
        ref = _ref(snap, needle)
        info = await frame.browser({"verb": "inspect", "ref": ref})
        assert info["ok"] and info["submits"] is True, info
        decided = consent(
            "browser.click", (ref,), {ref: info}, submit=False, choice=None, confirm=False, by_ref=False
        )
        return decided.irreversible

    assert await irreversible('button "Add to cart"') is False
    assert await irreversible('button "Place your order"') is True


async def test_profile_archive_round_trips_and_stays_out_of_the_work_checkpoint(frame: DockerFrame) -> None:
    await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
    work, profile = io.BytesIO(), io.BytesIO()
    await frame.archive("/work", work)
    await frame.archive("/browser", profile)
    # Both archives are plain tars: the cookie-bearing profile is its own archive, never `/work`'s.
    assert b"browser/profile" not in work.getvalue()
    assert b"browser/profile" in profile.getvalue()
    other = DockerFrame(task_id=f"pytest-browser-restore-{uuid.uuid4().hex[:8]}", profile="browser")
    try:
        await other.create()
        profile.seek(0)
        await other.unpack("/browser", profile)
        assert other._container is not None  # pyright: ignore[reportPrivateUsage]
        stat = other._container.exec_run(  # pyright: ignore[reportPrivateUsage]
            ["stat", "-c", "%u %a", "/browser", "/browser/profile"], user="1001", demux=True
        )
        stdout_b, _ = stat.output
        lines = (stdout_b or b"").decode().strip().splitlines()
        assert stat.exit_code == 0 and lines == ["1001 700", "1001 700"]
        local_state = other._container.exec_run(  # pyright: ignore[reportPrivateUsage]
            ["test", "-f", "/browser/profile/Local State"], user="1001"
        )
        assert local_state.exit_code == 0
        root_check = await other.browser({"verb": "ping"})
        assert root_check["ok"]
    finally:
        await other.destroy()


# A credentialed proxy, as sandbar's is: 407 until a request carries `user:pw`. Run in the frame on
# 127.0.0.1:3130; it serves every host from the frame's own http.server on 8088. The opener is on
# 127.0.0.1 (Chromium's bypass list: no proxy), so a new tab's request is Chromium's first through
# the proxy - with no credential cached yet, only the tab's own `Fetch` handler can answer it.
CRED_PROXY = r"""
import asyncio, base64
GOOD = "Basic " + base64.b64encode(b"user:pw").decode()
async def handle(reader, writer):
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
    method, target, _ = head[0].split(" ", 2)
    auth = next((h.split(":", 1)[1].strip() for h in head[1:] if h.lower().startswith("proxy-authorization:")), "")
    with open("/tmp/cred-proxy.log", "a") as log:
        log.write(f"{method} {target} {'auth' if auth == GOOD else '407'}\n")
    if auth != GOOD:
        writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="frame"\r\n'
                     b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain(); writer.close(); return
    path = "/" + target.split("://", 1)[-1].split("/", 1)[-1]
    up_r, up_w = await asyncio.open_connection("127.0.0.1", 8088)
    up_w.write(f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode()); await up_w.drain()
    writer.write(await up_r.read()); await writer.drain(); writer.close(); up_w.close()
async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 3130)
    async with server: await server.serve_forever()
asyncio.run(main())
"""
CRED_OPENER = '<!doctype html><title>Opener</title><a href="http://site.test:8088/child.html" target="_blank">Open the child</a>'
CRED_CHILD = "<!doctype html><title>Child</title><h1>Child loaded</h1>"


async def _cred_verb(frame: DockerFrame, home: str, cmd: dict[str, object]) -> dict[str, object]:
    """One verb to a sidecar of its own in the frame, behind the credentialed proxy."""
    env = {
        "SIDECAR_SOCK": f"{home}/s.sock",
        "SIDECAR_PROFILE": f"{home}/profile",
        "SIDECAR_RECORDING": f"{home}/rec",
        "HTTPS_PROXY": "http://user:pw@127.0.0.1:3130",
        "HTTP_PROXY": "http://user:pw@127.0.0.1:3130",
        "PYTHONPATH": "/opt/plane/browser",
        "SIDECAR_CMD": json.dumps(cmd),
    }
    line = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    out = await frame.exec(f"mkdir -p {home} && {line} python3 -m sidecar.ctl", timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


async def test_a_new_tabs_first_request_through_a_credentialed_proxy_is_answered(frame: DockerFrame) -> None:
    """A tab the page opens is held at its start until its proxy-auth handler
    exists, so its first request - Chromium's first through the proxy, no credential cached - is
    answered instead of failing on the 407. Each try is a fresh Chromium: once one request has
    authenticated, Chromium sends the credential with the next on its own."""
    await frame.write_file("/work/site/cred-proxy.py", CRED_PROXY)
    await frame.write_file("/work/site/opener.html", CRED_OPENER)
    await frame.write_file("/work/site/child.html", CRED_CHILD)
    await frame.exec("(python3 /work/site/cred-proxy.py >/tmp/cred-proxy.out 2>&1 &) && sleep 0.5")
    loaded = []
    for attempt in range(5):
        home = f"/tmp/cred-{attempt}"
        opened = await _cred_verb(frame, home, {"verb": "open", "url": "http://127.0.0.1:8088/opener.html"})
        assert opened["ok"] and opened["title"] == "Opener", opened
        snap = await _cred_verb(frame, home, {"verb": "snapshot"})
        clicked = await _cred_verb(frame, home, {"verb": "click", "ref": _ref(snap, 'link "Open the child"')})
        assert clicked["ok"] and clicked.get("tab") == "opened", clicked
        await _cred_verb(frame, home, {"verb": "wait", "for": "Child loaded"})
        child = await _cred_verb(frame, home, {"verb": "snapshot"})
        loaded.append(child.get("title") == "Child")
        await _cred_verb(frame, home, {"verb": "shutdown"})
    log = (await frame.exec("cat /tmp/cred-proxy.log")).stdout
    assert all(loaded), f"new tab loads: {loaded}\nproxy:\n{log}"
    assert log.count("child.html auth") == len(loaded), log  # each answered by the tab's own handler
