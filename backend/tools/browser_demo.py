# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hands-on demo of the browser frame AND the navigator (computer use).

Boots a `browser`-profile frame, serves the fixture admin app (`tests/fixtures/admin/`) inside
it, and drives Chromium through the ONE driver verb, `frame.browser(command)` - the scripted
tour, no agent, no model. Then the interactive prompt takes over, and from there a plain-English
line goes to a REAL navigator agent (`resolve(ModelClass.COMPUTER_USE, catalog)`, the same
`browser_toolset` the engine uses) instead of a raw sidecar verb. Run from `backend/` with the
dev stack up:

    uv run python tools/browser_demo.py            # scripted tour, then an interactive prompt
    uv run python tools/browser_demo.py --watch    # same, plus http://127.0.0.1:8765 shows the screen
    uv run python tools/browser_demo.py --no-repl  # tour only
    uv run python tools/browser_demo.py --watch --allow=plane.so,example.com   # let the frame reach those hosts
    uv run python tools/browser_demo.py --agent="Sign in as admin / hunter2 and close Ada Lovelace's record"
    uv run python tools/browser_demo.py --allow=plane.town --site=https://phoenix.plane.town/ \
        --agent="describe the intake form"   # drive a REAL site instead of the fixture (tour skipped)
    uv run python tools/browser_demo.py --watch --allow=plane.town --site=https://phoenix.plane.town/x   # watch a real site, no tour

The frame's egress is deny-by-default: with no `--allow=`, every outside URL fails with
ERR_TUNNEL_CONNECTION_FAILED, which is the proxy refusing the CONNECT. `--allow=` takes a
comma-separated list of hostnames; a host allows its subdomains too (`plane.so` covers `app.plane.so`).

At the prompt: a line starting with `{` is a raw sidecar verb (JSON), e.g. {"verb": "snapshot"}.
`quit` (or `q`/`exit`) finalizes the recording and lists it. Anything else is an instruction in
plain words to the navigator, which keeps its message history across turns - "sign in as admin /
hunter2", then "close Grace Hopper's record", then "what is left open?" is one conversation. Every
approval ask prints to the console and reads y/n from stdin; the narration prints after each turn.
`--agent="<goal>"` runs one instruction non-interactively and exits (for scripts and the smoke
path) - the session is built lazily on the first text line/`--agent`, so the JSON-only demo still
needs no database or provider key. `--site=<url>` points the navigator at a real site instead of
the fixture: its host is added to `--allow` automatically, the first-turn preamble/REPL hint name
it, and the scripted fixture tour - which has nowhere to point once the goal is a real site -
defaults OFF (`--tour` forces it back on; `--no-tour` skips it even without `--site`); the fixture
server still starts underneath regardless (harmless). The frame is destroyed on exit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import textwrap
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "admin"
FIXTURE_FILES: tuple[str, ...] = ("index.html", "records.html", "settings.html", "server.py")


def _ref(snapshot_text: str, needle: str) -> str:
    for line in snapshot_text.splitlines():
        if needle in line and line.strip().startswith("[e"):
            return line.strip()[1:].split("]", 1)[0]
    raise SystemExit(f"no reference for {needle!r} in snapshot")


def _save_png(png_b64: str) -> None:
    Path("demo-shot.png").write_bytes(base64.b64decode(png_b64))


def _show(title: str, result: dict) -> None:
    print(f"\n── {title} ──")
    shown = {k: v for k, v in result.items() if k not in ("text", "png_b64")}
    print(json.dumps(shown, indent=2))
    if "text" in result:
        print(textwrap.indent(result["text"], "  │ "))


async def tour(frame: DockerFrame) -> None:
    """open → snapshot → fill login → snapshot → click "Save Ada Lovelace" → follow "Settings" → back."""
    _show("open", await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"}))
    snap = await frame.browser({"verb": "snapshot"})
    _show("snapshot (what the model reads; note: no password value, references on interactive nodes)", snap)

    user_ref = _ref(snap["text"], '"Username"')
    pass_ref = _ref(snap["text"], '"Password"')
    _show(
        "fill the login form and submit",
        await frame.browser(
            {
                "verb": "fill",
                "fields": [{"ref": user_ref, "value": "admin"}, {"ref": pass_ref, "value": "hunter2"}],
                "submit": True,
            }
        ),
    )

    snap = await frame.browser({"verb": "snapshot"})
    _show("snapshot of the records page (signed in)", snap)
    save_ref = _ref(snap["text"], 'button "Save Ada Lovelace"')
    _show(f"click {save_ref} (Save Ada Lovelace)", await frame.browser({"verb": "click", "ref": save_ref}))

    snap = await frame.browser({"verb": "snapshot"})
    settings_ref = _ref(snap["text"], 'link "Settings"')
    _show("stale reference vs live: take a new snapshot first", {"epoch": snap.get("epoch")})
    stale = await frame.browser({"verb": "click", "ref": save_ref})
    _show("click the OLD reference again (epoch changed → refused)", stale)

    _show("follow the Settings link", await frame.browser({"verb": "click", "ref": settings_ref}))
    _show("back", await frame.browser({"verb": "back"}))

    _show("refused scheme", await frame.browser({"verb": "open", "url": "file:///browser/profile"}))
    shot = await frame.browser({"verb": "screenshot"})
    _save_png(shot["png_b64"])
    _show("screenshot → ./demo-shot.png", {k: v for k, v in shot.items() if k != "png_b64"})

    rec = await frame.exec(f"ls -la {frame.recording_dir} && cat {frame.recording_dir}/actions.json")
    print("\n── the recording (evidence, written by the browser user; note: no typed text) ──")
    print(textwrap.indent(rec.stdout, "  │ "))

    locked = await frame.exec("ls /browser/profile")
    print("\n── the agent user trying to read cookies ──")
    print(textwrap.indent((locked.stderr or locked.stdout).strip(), "  │ "), f"(exit {locked.exit_code})")


class _Watch:
    """A local page that shows the browser's latest screenshot, refreshed every second.

    Not the live screen (that is the screen relay, ADR-065); a poll of the `screenshot` verb, kept
    in memory and served on 127.0.0.1 by a stdlib HTTP server in a thread. Good enough to watch
    yourself click around. Before the first screenshot exists there is nothing to show, so the
    page keeps a caption visible until the `<img>`'s `onload` fires (`onerror` brings it back); a
    `/status` endpoint carries the latest journal line (the `· browser.click ✓ ...` text the demo
    already prints to the console, routed through `_report_status`) so the owner watching the page
    sees what the navigator is doing without a terminal open.
    """

    PAGE = (
        b"<!doctype html><meta charset=utf-8><title>browser frame</title>"
        b"<style>body{margin:0;background:#111;color:#ccc;font:14px/1.4 -apple-system,sans-serif;"
        b"display:flex;flex-direction:column;align-items:center;justify-content:center;"
        b"height:100vh;gap:12px}"
        b"img{max-width:100vw;max-height:80vh;box-shadow:0 0 30px #000}"
        b"#caption{max-width:80vw;text-align:center}"
        b"#status{opacity:.7;max-width:90vw;overflow-wrap:anywhere;text-align:center}</style>"
        b"<img id=s src=/shot.png style=display:none>"
        b"<div id=caption>waiting for the first screenshot&hellip; "
        b"(it appears once the browser has a page)</div>"
        b"<div id=status></div>"
        b"<script>"
        b"const s=document.getElementById('s'),c=document.getElementById('caption'),"
        b"t=document.getElementById('status');"
        b"s.onload=()=>{s.style.display='';c.style.display='none'};"
        b"s.onerror=()=>{s.style.display='none';c.style.display=''};"
        b"setInterval(()=>{s.src='/shot.png?'+Date.now()},1000);"
        b"setInterval(()=>{fetch('/status').then(r=>r.text()).then(v=>{t.textContent=v})},1000);"
        b"</script>"
    )

    def __init__(self, port: int) -> None:
        self.port = port
        self.latest: bytes = b""
        self.status: str = "waiting for the first action…"

    def serve(self) -> None:
        import http.server

        watch = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/shot.png"):
                    body, ctype = watch.latest, "image/png"
                elif self.path.startswith("/status"):
                    body, ctype = watch.status.encode(), "text/plain"
                else:
                    body, ctype = watch.PAGE, "text/html"
                self.send_response(200 if body else 204)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:  # quiet
                return

        http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler).serve_forever()

    async def poll(self, frame: DockerFrame) -> None:
        while True:
            shot = await frame.browser({"verb": "screenshot"})
            if shot.get("ok"):
                self.latest = base64.b64decode(shot["png_b64"])
            await asyncio.sleep(1.0)


_active_watch: _Watch | None = None


def _set_active_watch(watch: _Watch | None) -> None:
    """Module-level hook: the journal callback is built deep inside `NavigatorSession`, with no
    reference of its own to the CLI's `_Watch` instance, so `main()` registers it here and
    `_report_status` reaches it from wherever the `·` lines are printed."""
    global _active_watch
    _active_watch = watch


def _report_status(line: str) -> None:
    if _active_watch is not None:
        _active_watch.status = line


SITE_URL = "http://127.0.0.1:8088"
# The site as context, the way a real task's contract would hand it in (`/task/context.md`) -
# a first-user-turn preamble, so the owner's plain-text lines need not repeat the URL and the
# navigator never has to guess or ask for it.
SITE_PREAMBLE = (
    f"The site for this session is {SITE_URL}/ - a local test server the task allows; open it directly.\n\n"
)


def _site_preamble(url: str) -> str:
    """Same shape as `SITE_PREAMBLE`, for a `--site=<url>` the owner named instead of the
    fixture."""
    return f"The site for this session is {url} - open it directly.\n\n"


class NavigatorSession:
    """A persistent navigator conversation: one real agent, one message history kept
    across turns, so "sign in as admin / hunter2", then "close Grace Hopper's record", then "what
    is left open?" is one conversation. Built LAZILY on the first `say()` - the tour and the
    JSON-only REPL never touch the database or a provider key; only typing an instruction does.
    """

    def __init__(
        self,
        frame: DockerFrame,
        *,
        allow_hosts: Iterable[str] = (),
        site_preamble: str = SITE_PREAMBLE,
        site_display: str = f"{SITE_URL}/index.html",
    ) -> None:
        self.frame = frame
        # `--allow=`'s hosts, exactly as already handed to `frame.create` - the toolset's OWN ask
        # gate (`domain_allowed`, a suffix match) must see the same allowlist the frame's egress
        # proxy already honours, or a host the operator explicitly allowed still gets asked about.
        self.allow_hosts = frozenset(allow_hosts)
        self.site_preamble = site_preamble
        self.site_display = site_display
        self._runner: Any = None
        self.history: list[Any] = []

    async def _ensure_runner(self) -> Any:
        if self._runner is not None:
            return self._runner

        import prismoid.ai.agents.all  # noqa: F401  - the import IS the registration
        from prismoid.ai.agents.base import build
        from prismoid.ai.deps import TaskDeps
        from prismoid.ai.tools.browser import BrowserPorts
        from prismoid.db.session import session_scope
        from prismoid.operations import models as model_ops

        async with session_scope() as session:
            catalog = await model_ops.catalog(session, enabled_only=True)

        # The navigator's habit (navigator.md rule 1) is to read these before anything else - a
        # real task always has them. This ad-hoc demo frame does not, so without this the first
        # two journal lines are always a FileNotFoundError miss. Write them once, before the
        # first turn, so the habit finds what it expects (belt and braces with the site preamble
        # still carried in the first-turn message below).
        await self.frame.write_file("/task/context.md", self.site_preamble)
        await self.frame.write_file("/task/answers.json", "{}\n")

        async def journal(
            tool: str,
            ok: bool,
            summary: str = "",
            duration_ms: int = 0,
            denied_reason: str = "",
            tool_call_id: str = "",
        ) -> None:
            mark = "✓" if ok else f"✗ {denied_reason}".rstrip()
            line = f"· {tool}  {mark}  {summary}".rstrip()
            print(f"  {line}")
            _report_status(line)

        # `127.0.0.1`/`localhost` for the fixture, PLUS every `--allow=`/`--site=` host the frame's
        # own egress proxy already honours - `domain_allowed` is a suffix match, so `plane.town`
        # here covers `phoenix.plane.town` the same way the proxy's allowlist does.
        allowed = {"127.0.0.1", "localhost"} | set(self.allow_hosts)
        deps = TaskDeps(
            frame=self.frame,
            browser=BrowserPorts(browse=self.frame.browser, allow=self.frame.allow, allowed=allowed),
            journal_tool=journal,
        )
        self._runner = build("operator", deps, catalog)
        return self._runner

    async def say(self, text: str) -> str:
        """One turn: run the goal, resolve every approval ask AND every ask_user question at
        the console, return the narration. Message history rides across calls, so the REPL is
        one conversation. The navigator (`can_ask_user=True`) can defer TWO different ways:
        `.approvals` (a browser y/n ask) and `.calls` (`ask_user`'s question batch) -
        both must be answered or pydantic-ai refuses the resume outright."""
        from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied

        loop = asyncio.get_running_loop()

        async def _input(prompt: str) -> str | None:
            # No controlling stdin (a non-interactive `--agent` invocation piped from nowhere) -
            # None signals EOF distinctly, rather than an empty string a human could also have
            # typed, so the caller can give the model an unambiguous "no one is here" answer
            # instead of a raw EOFError crashing the run mid-turn.
            try:
                return await loop.run_in_executor(None, input, prompt)
            except EOFError:
                return None

        runner = await self._ensure_runner()
        # The site as context, once, on the conversation's first turn only - later turns already
        # have it in message_history, and repeating it would just be more text to re-read.
        prompt = text if self.history else f"{self.site_preamble}{text}"
        result = await runner.run(prompt, history=self.history or None)
        while isinstance(result.output, DeferredToolRequests):
            approvals: dict[str, ToolApproved | ToolDenied] = {}
            calls: dict[str, str] = {}
            for part in result.output.approvals:
                meta = result.output.metadata.get(part.tool_call_id) or {}
                host = f"  host={meta['host']}" if meta.get("scopes") else ""
                print(f"\nASK  {meta.get('tool')}  {meta.get('preview')}{host}")
                typed = await _input("approve? [y/N] ")
                yn = (typed or "").strip().lower()
                if yn != "y":
                    approvals[part.tool_call_id] = ToolDenied("declined at the console")
                    continue
                if meta.get("scopes"):  # the allowlist ask (browser_open) - no fingerprint to carry
                    approvals[part.tool_call_id] = ToolApproved()
                else:  # an acting verb - carry the fingerprint it was approved with as `expect`
                    approvals[part.tool_call_id] = ToolApproved(
                        override_args={**part.args_as_dict(), "expect": meta.get("fingerprint")}
                    )
            for part in result.output.calls:
                meta = result.output.metadata.get(part.tool_call_id) or {}
                if meta.get("kind") == "ask_user":
                    print("\nASK  ask_user")
                    lines = []
                    for q in meta.get("questions") or []:
                        question = str(q.get("question", ""))
                        options = q.get("options") or []
                        suffix = f"  [{' / '.join(str(o) for o in options)}]" if options else ""
                        print(f"  · {question}{suffix}")
                        reply = await _input("  > ")
                        answer = (
                            "no answer is available in this non-interactive run"
                            if reply is None
                            else (reply.strip() or "no answer available at the console")
                        )
                        lines.append(f"{question} {answer}")
                    calls[part.tool_call_id] = "\n".join(lines)
                else:  # a deferred call this demo does not model (e.g. request_connector)
                    calls[part.tool_call_id] = "not available in this demo"
            result = await runner.run(
                "",
                history=result.all_messages(),
                answers=DeferredToolResults(approvals=approvals, calls=calls),
            )
        self.history = result.all_messages()
        return str(result.output)


async def repl(session: NavigatorSession) -> None:
    print('\nInteractive. `{"verb": ...}` = a raw sidecar verb · plain English = the navigator agent · quit')
    print(f"site: {session.site_display} · type an instruction, a {{json}} verb, or quit")
    loop = asyncio.get_running_loop()
    frame = session.frame
    while True:
        try:
            line = await loop.run_in_executor(None, input, "browser> ")
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            done = await frame.browser({"verb": "finalize"})
            print(f"\nfinalize: {json.dumps(done, indent=2)}")
            out = await frame.exec(f"ls -la {frame.recording_dir}")
            print(textwrap.indent(out.stdout or out.stderr, "  │ "))
            break
        if line.startswith("{"):
            try:
                cmd = json.loads(line)
            except ValueError:
                print('not JSON. try {"verb": "snapshot"}')
                continue
            _show(cmd.get("verb", "?"), await frame.browser(cmd))
            continue
        narration = await session.say(line)
        print(f"\nNARRATION\n{narration}")


async def main() -> None:
    print("building/checking the browser image (first time takes minutes)…")
    ensure_image("browser")
    frame = DockerFrame(task_id=f"demo-{uuid.uuid4().hex[:6]}", profile="browser")
    allow = [
        h
        for arg in sys.argv
        if arg.startswith("--allow=")
        for h in arg.removeprefix("--allow=").split(",")
        if h
    ]
    agent_goal = next((a.removeprefix("--agent=") for a in sys.argv if a.startswith("--agent=")), None)
    site_arg = next((a.removeprefix("--site=") for a in sys.argv if a.startswith("--site=")), None)
    site_host = urlsplit(site_arg).hostname if site_arg else None
    if site_host and site_host not in allow:
        # So the owner does not have to repeat --site's host in --allow too.
        allow.append(site_host)
    # The fixture tour is furniture for the fixture; a `--site` run has nowhere to point it, so
    # it defaults OFF once `--site` is given - `--tour` overrides that (and `--no-tour` overrides
    # the no-`--site` default of ON), so either flag always wins over the `--site` default.
    run_tour = "--tour" in sys.argv or (site_arg is None and "--no-tour" not in sys.argv)
    await frame.create(allow_domains=allow or None)
    print(f"egress allowlist: {allow or 'none (every outside host is refused)'}")
    try:
        for name in FIXTURE_FILES:
            await frame.write_file(f"/work/site/{name}", (FIXTURE_DIR / name).read_text())
        await frame.exec(
            "cd /work/site && (python3 server.py 8088 >/dev/null 2>&1 &) && "
            "for i in $(seq 1 40); do curl -sf --noproxy '*' http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
        )
        poller: asyncio.Task[None] | None = None
        watch: _Watch | None = None
        if "--watch" in sys.argv:
            import threading

            watch = _Watch(port=8765)
            _set_active_watch(watch)
            threading.Thread(target=watch.serve, daemon=True).start()
            poller = asyncio.create_task(watch.poll(frame))
            print("\nwatch the browser at  http://127.0.0.1:8765  (refreshes every second)\n")
        if run_tour:
            await tour(frame)
        session = NavigatorSession(
            frame,
            allow_hosts=allow,
            site_preamble=_site_preamble(site_arg) if site_arg else SITE_PREAMBLE,
            site_display=site_arg if site_arg else f"{SITE_URL}/index.html",
        )
        if agent_goal:
            narration = await session.say(agent_goal)
            print(f"\nNARRATION\n{narration}")
            done = await frame.browser({"verb": "finalize"})
            print(f"\nfinalize: {json.dumps(done, indent=2)}")
        elif "--no-repl" not in sys.argv:
            if watch is not None:
                print(f"\nwatch: http://127.0.0.1:{watch.port} (open it now; it refreshes by itself)\n")
            await repl(session)
        if poller is not None:
            poller.cancel()
    finally:
        await frame.destroy()
        print("frame destroyed.")


if __name__ == "__main__":
    asyncio.run(main())
