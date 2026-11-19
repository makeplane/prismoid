# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`egress.sites_in`, the one URL and host extractor (ADR-046): the web door's anchor, the computer
door and the record's reason scrub all read sites through it. Pure; no network."""

from __future__ import annotations

import time

from prismoid.schemas.egress import Site, sites_in, url_host


def test_a_link_a_bare_host_and_its_path_are_read_in_order() -> None:
    text = "See https://Example.com/a?b=1. Then at www.Foo.org/x/y, or mail me@mail.io"
    assert sites_in(text) == [
        Site("example.com", 4, 29, "https://Example.com/a?b=1"),
        Site("www.foo.org", 39, 50, None, "/x/y,"),
        Site("mail.io", 67, 74, None),
    ]


def test_a_host_inside_a_link_is_not_read_twice() -> None:
    assert [s.host for s in sites_in("open https://docs.example.com/guide.html now")] == ["docs.example.com"]


def test_what_is_not_a_host_is_not_read() -> None:
    for text in ("version 1.2.3", "a.b", "foo_example.com", "no sites here", "", "x." * 5):
        assert [s.host for s in sites_in(text)] == [], text


def test_a_link_that_will_not_parse_names_no_host() -> None:
    [site] = sites_in("go to http://[::1 now")
    assert site.url == "http://[::1" and site.host == ""
    assert url_host("http://[::1") == "" and url_host("https://A.example/x") == "a.example"


def test_a_long_host_shaped_run_is_read_in_linear_time() -> None:
    """A dotted or dashed run that never ends in a top-level label was rescanned from every start
    inside it (2.9 s for 20k characters, quadratic); the 254-character bound keeps each start short.
    At 30k units the old scan takes 13-100 s and the bounded one a fifth of a second, so a 3 s
    ceiling catches the regression with an order of magnitude to spare under load."""
    for unit in ("a.", "a-", "a-a."):
        started = time.perf_counter()
        sites_in(unit * 30_000)
        took = time.perf_counter() - started
        assert took < 3.0, f"{unit!r}: {took:.2f}s for {len(unit) * 30_000} characters is not linear"
