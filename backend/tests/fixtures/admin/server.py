# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fixture admin app: static pages, a stdlib server, and a log of every
mutating request - so a test asserts what HAPPENED, not what a screenshot shows."""

from __future__ import annotations

import json
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "log.jsonl")
RECORDS = [
    {"id": 1, "name": "Ada Lovelace", "status": "open"},
    {"id": 2, "name": "Grace Hopper", "status": "open"},
    {"id": 3, "name": "Mary Jackson", "status": "closed"},
]
_START = [dict(r) for r in RECORDS]


def _page(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def _rows() -> str:
    out = []
    for r in RECORDS:
        opts = "".join(
            f'<option value="{s}"{" selected" if s == r["status"] else ""}>{s}</option>'
            for s in ("open", "closed")
        )
        out.append(
            f"<tr><td>{r['name']}</td><td>"
            f'<form method="post" action="/records/{r["id"]}"><select name="status" aria-label="Status for {r["name"]}">{opts}</select> '
            f'<button type="submit">Save {r["name"]}</button></form></td><td>'
            f'<form method="post" action="/records/{r["id"]}/delete"><button type="submit">Delete {r["name"]}</button></form></td></tr>'
        )
    return "".join(out)


def _log(method: str, path: str, keys: list[str]) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "method": method, "path": path, "keys": sorted(keys)}) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _signed_in(self) -> bool:
        return "sid=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(_page("index.html").replace("{{message}}", ""))
        if path == "/records.html":
            if not self._signed_in():
                return self._redirect("/index.html")
            return self._send(_page("records.html").replace("{{rows}}", _rows()))
        if path == "/settings.html":
            return self._send(_page("settings.html"))
        if path == "/stuck.html":  # the web suite's stall row: a button that never answers
            return self._send(_page("stuck.html"))
        if path == "/log":
            lines: list[str] = []
            if os.path.exists(LOG):
                with open(LOG, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            body = "[" + ",".join(lines) + "]"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send("<h1>Not found</h1>", 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
        path = self.path.split("?", 1)[0]
        if path == "/reset":
            # The web suite's per-row reset (evals/suites/web.py): the records as they started
            # and an empty log. Linked from no page, so an agent never reaches it.
            RECORDS[:] = [dict(r) for r in _START]
            with open(LOG, "w", encoding="utf-8"):
                pass
            return self._send("", HTTPStatus.NO_CONTENT)
        _log("POST", path, list(form))
        if path == "/login":
            if form.get("user") == "admin" and form.get("password") == "hunter2":
                return self._redirect("/records.html", cookie="sid=ok; Path=/")
            return self._send(_page("index.html").replace("{{message}}", "Wrong credentials"))
        if not self._signed_in():
            return self._redirect("/index.html")
        if path == "/records/delete-all":
            RECORDS.clear()
            return self._redirect("/records.html")
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "records" and parts[1].isdigit():
            rid = int(parts[1])
            if len(parts) == 3 and parts[2] == "delete":
                RECORDS[:] = [r for r in RECORDS if r["id"] != rid]
            elif len(parts) == 2 and form.get("status") in ("open", "closed"):
                for r in RECORDS:
                    if r["id"] == rid:
                        r["status"] = form["status"]
            return self._redirect("/records.html")
        self._send("<h1>Not found</h1>", 404)

    def log_message(self, format: str, *args: object) -> None:
        return  # quiet


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    if os.path.exists(LOG):
        os.remove(LOG)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
