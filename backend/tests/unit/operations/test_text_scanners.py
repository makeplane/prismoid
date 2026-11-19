# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scanners a person's text meets run in linear time, and the typed message keeps what the person sees.

Linear time is a growth rate, never a stopwatch reading: a line GROWTH times longer must cost about
GROWTH times more. Quadratic backtracking costs GROWTH**2 (64x) and exponential far more, so a bound
of 3 x GROWTH separates them with room for noise. The clock is this thread's CPU time, so waiting for
a core on a loaded machine never counts; each side is the best of TRIES interleaved runs, which drops
a cache or allocator hiccup on either side.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from prismoid.operations.swarm import canonicalize
from prismoid.schemas.computer_use import named_sites
from prismoid.schemas.connectors import sanitize_server_text
from prismoid.schemas.inputs import visible

GROWTH = 8
TRIES = 5


def growth_of(run: Callable[[str], object], line_of: Callable[[int], str], n: int) -> float:
    """How many times longer `run` takes on `line_of(n)` than on `line_of(n // GROWTH)`."""
    short, long = line_of(n // GROWTH), line_of(n)
    best = {short: float("inf"), long: float("inf")}
    for _ in range(TRIES):
        for line in (short, long):
            began = time.thread_time()
            run(line)
            best[line] = min(best[line], time.thread_time() - began)
    return best[long] / best[short]


@pytest.mark.parametrize(
    ("scan", "line_of", "n"),
    [
        (named_sites, lambda n: "a." * n, 10_000),  # a host chain no TLD ends
        (named_sites, lambda n: "a-" * n, 10_000),
        (canonicalize, lambda n: "a" + " " * n + "x", 19_998),  # the legal-suffix fold
    ],
    ids=["host-dots", "host-dashes", "legal-suffix"],
)
def test_the_scanners_are_linear_on_a_long_line(
    scan: Callable[[str], object], line_of: Callable[[int], str], n: int
) -> None:
    """The hosts a browser step may open are read from a goal (8k), and every gathered name is folded: a pattern
    that retried one long run from every start inside it cost seconds - 2.9 s for a 20k host-shaped run."""
    assert growth_of(scan, line_of, n) < 3 * GROWTH


def test_the_typed_message_drops_invisible_characters_and_keeps_emoji() -> None:
    """A zero-width space pasted after a word is gone from the message the person sent; the ZWJ inside an
    emoji sequence is part of the emoji."""
    assert visible("close​") == "close"
    assert visible("﻿thanks!") == "thanks!"
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    assert visible(family) == family
    assert visible("❤️‍\U0001f525") == "❤️‍\U0001f525"


def test_one_invisible_rule_serves_the_typed_message_and_server_prose() -> None:
    """A bidi override or a tag character is gone from a typed message too; server prose keeps an emoji whole."""
    assert visible("pay‮\U000e0041 now\x1b") == "pay now"
    assert sanitize_server_text("Ship it ❤️‍\U0001f525", 100) == "Ship it ❤️‍\U0001f525"
