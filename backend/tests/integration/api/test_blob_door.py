# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`GET /v1/blobs/{digest}` (ADR-077): local mode's signed download door, no Actor."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from uvicorn.logging import AccessFormatter

from prismoid import keyfile, storage, storage_fs
from prismoid.api.app import create_app, drop_route_arguments
from prismoid.conf import settings
from prismoid.observability import install_log_redaction, redact_server_span

pytestmark = pytest.mark.integration


@pytest.fixture
def fs_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings.worker, "storage_backend", "fs")
    monkeypatch.setattr(settings.local, "home", tmp_path)
    keyfile.forget()
    return tmp_path


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


async def test_blob_door_serves_with_valid_sig(fs_home: Path) -> None:
    digest = storage.put(b"%PDF-1.4 hi")
    url = storage.presign(digest, 'we"ird\r\nname é.pdf', inline=True, media="application/pdf")
    async with _client() as c:
        r = await c.get(_path(url))
    assert r.status_code == 200 and r.content == b"%PDF-1.4 hi"
    assert r.headers["content-type"] == "application/pdf"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("inline; filename=") and "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition and '"ird' not in disposition.split("filename*")[0].replace(
        '\\"', ""
    )
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_the_signature_never_reaches_a_span(fs_home: Path) -> None:
    """What a real request's matched route tells the mapper: the signature is the door's credential,
    so the parsed query never lands on the server span's recorded arguments."""
    scopes: list[dict[str, Any]] = []
    app = create_app()

    async def recording(scope: Any, receive: Any, send: Any) -> None:
        scopes.append(scope)
        await app(scope, receive, send)

    digest = storage.put(b"hi")
    url = storage.presign(digest, "a.txt", media="text/plain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=recording), base_url="http://test") as c:
        r = await c.get(_path(url))
    assert r.status_code == 200
    sig = dict(parse_qsl(urlsplit(url).query))["sig"]
    request: Any = SimpleNamespace(scope=scopes[0])
    kept = drop_route_arguments(request, {"values": {"q": {"sig": sig}}, "errors": []})
    assert sig not in repr(kept) and kept["values"] == {}


async def test_inline_html_is_sandboxed_and_downloads_are_attachments(fs_home: Path) -> None:
    digest = storage.put(b"<script>1</script>")
    async with _client() as c:
        inline = await c.get(_path(storage.presign(digest, "a.html", inline=True, media="text/html")))
        plain = await c.get(_path(storage.presign(digest, "a.bin")))
    assert inline.headers["content-security-policy"] == "sandbox"
    assert plain.headers["content-disposition"].startswith("attachment;")
    assert plain.headers["content-type"].startswith("application/octet-stream")


async def test_blob_door_403_on_bad_or_expired_sig(fs_home: Path) -> None:
    digest = storage.put(b"secret")
    url = storage.presign(digest, "a.txt", media="text/plain")
    q = dict(parse_qsl(urlsplit(url).query))
    base = f"/v1/blobs/{digest}"

    def with_(**over: str) -> str:
        return base + "?" + "&".join(f"{k}={v}" for k, v in {**q, **over}.items())

    other = storage.put(b"other")
    async with _client() as c:
        bad = [
            await c.get(with_(sig="0" * 64)),
            await c.get(with_(name="b.txt")),  # a signed field changed
            await c.get(with_(inline="1")),
            await c.get(with_(media="text/html")),
            await c.get(with_(bucket="snapshots")),  # not a bucket presign is called with
            await c.get(f"/v1/blobs/{other}?" + urlsplit(url).query),  # another blob's sig
            await c.get(base),  # no query at all
            await c.get(with_(exp="notanumber")),
        ]
        # a missing blob under a bad sig is the same 403 as an existing one
        gone = await c.get(f"/v1/blobs/{'a' * 64}?" + urlsplit(url).query)
        past = int(time.time()) - 5
        sig = storage_fs.sign(digest, "artifacts", "a.txt", False, "text/plain", exp=past)
        expired = await c.get(
            f"{base}?bucket=artifacts&name=a.txt&inline=0&media=text%2Fplain&exp={past}&sig={sig}"
        )
    assert [r.status_code for r in bad] == [403] * len(bad)
    assert gone.status_code == 403 and gone.json()["error"]["message"] == bad[0].json()["error"]["message"]
    assert expired.status_code == 403


async def test_blob_door_refuses_an_exp_beyond_the_cap(fs_home: Path) -> None:
    digest = storage.put(b"x")
    exp = int(time.time()) + storage_fs.MAX_TTL_S + 3600
    sig = storage_fs.sign(digest, "artifacts", "f", False, "", exp=exp)
    async with _client() as c:
        r = await c.get(f"/v1/blobs/{digest}?bucket=artifacts&name=f&inline=0&exp={exp}&sig={sig}")
    assert r.status_code == 403


async def test_blob_door_404_on_s3_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings.local, "home", tmp_path)
    monkeypatch.setattr(settings.worker, "storage_backend", "s3")
    async with _client() as c:
        r = await c.get(
            f"/v1/blobs/{'a' * 64}?bucket=artifacts&name=f&inline=0&exp=9999999999&sig={'0' * 64}"
        )
    assert r.status_code == 404


async def test_blob_door_404_outside_local_mode_even_with_fs(
    fs_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = storage.put(b"x")
    path = _path(storage.presign(digest, "f"))
    monkeypatch.setattr(settings, "environment", "dev")
    async with _client() as c:
        assert (await c.get(path)).status_code == 404


@pytest.mark.parametrize("sig", ["é" * 64, "é", "A" * 64, "0" * 63, "0" * 65, "zz" * 32])
async def test_a_malformed_sig_is_the_same_403_never_a_500(fs_home: Path, sig: str) -> None:
    digest = storage.put(b"x")
    exp = int(time.time()) + 60
    async with _client() as c:
        r = await c.get(
            f"/v1/blobs/{digest}",
            params={"bucket": "artifacts", "name": "f", "inline": "0", "exp": exp, "sig": sig},
        )
    assert r.status_code == 403


@pytest.mark.parametrize("exp", ["", "-5", "1.5", "+9", "9" * 13, " 9", "١٢٣"])
async def test_a_malformed_exp_is_403(fs_home: Path, exp: str) -> None:
    digest = storage.put(b"x")
    async with _client() as c:
        r = await c.get(
            f"/v1/blobs/{digest}",
            params={"bucket": "artifacts", "name": "f", "inline": "0", "exp": exp, "sig": "0" * 64},
        )
    assert r.status_code == 403


async def test_an_unusable_key_file_is_a_logged_503_with_no_detail(fs_home: Path) -> None:
    digest = storage.put(b"x")
    url = _path(storage.presign(digest, "f"))
    (fs_home / "blob.key").chmod(0o644)
    keyfile.forget()
    async with _client() as c:
        r = await c.get(url)
    assert r.status_code == 503
    assert "blob.key" not in r.text and str(fs_home) not in r.text


async def test_blob_door_serves_only_known_buckets_and_well_formed_digests(fs_home: Path) -> None:
    exp = int(time.time()) + 60
    async with _client() as c:
        # a validly signed but unlisted bucket, and a digest that is not hex: still 403
        sig = storage_fs.sign("a" * 64, "evil", "f", False, "", exp=exp)
        r1 = await c.get(f"/v1/blobs/{'a' * 64}?bucket=evil&name=f&inline=0&exp={exp}&sig={sig}")
        upper = "A" * 64  # validly signed, 64 chars, but not a lowercase sha256 hex digest
        sig = storage_fs.sign(upper, "artifacts", "f", False, "", exp=exp)
        r2 = await c.get(f"/v1/blobs/{upper}?bucket=artifacts&name=f&inline=0&exp={exp}&sig={sig}")
        nonhex = "g" * 64
        sig = storage_fs.sign(nonhex, "artifacts", "f", False, "", exp=exp)
        r3 = await c.get(f"/v1/blobs/{nonhex}?bucket=artifacts&name=f&inline=0&exp={exp}&sig={sig}")
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)


async def test_a_validly_signed_missing_blob_is_404(fs_home: Path) -> None:
    exp = int(time.time()) + 60
    digest = "b" * 64
    sig = storage_fs.sign(digest, "artifacts", "f", False, "", exp=exp)
    async with _client() as c:
        r = await c.get(f"/v1/blobs/{digest}?bucket=artifacts&name=f&inline=0&exp={exp}&sig={sig}")
    assert r.status_code == 404


async def test_sig_never_reaches_the_access_log_or_the_server_span(fs_home: Path) -> None:
    """A real signed request is served, then the line uvicorn writes for it and the span the ASGI
    middleware starts for it are run through the app's own redaction."""
    digest = storage.put(b"x")
    url = storage.presign(digest, "a.bin")
    path = _path(url)
    sig = dict(parse_qsl(urlsplit(url).query))["sig"]
    async with _client() as c:
        assert (await c.get(path)).status_code == 200

    install_log_redaction()
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Keep()
    handler.setFormatter(AccessFormatter("%(request_line)s %(status_code)s", use_colors=False))
    access = logging.getLogger("uvicorn.access")
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", path, "1.1", 200)
    finally:
        access.removeHandler(handler)
    assert lines and sig not in lines[0] and "sig=[redacted]" in lines[0]

    class Span:
        def __init__(self) -> None:
            self.attributes = {"http.url": f"http://test{path}", "http.target": path}

        def set_attribute(self, key: str, value: str) -> None:
            self.attributes[key] = value

    span = Span()
    redact_server_span(span, {"query_string": urlsplit(url).query.encode()})
    assert sig not in "".join(span.attributes.values())
