# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The mailer's console backend IS the product in dev (ADR-031): the whole message prints to
stdout, boxed and unmistakable, because locally there is no relay and the reset code has to
land somewhere the developer can actually read. No database, no network - a unit suite.
"""

from __future__ import annotations

import pytest

from prismoid import mailer
from prismoid.conf import settings


async def test_console_backend_prints_the_whole_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.email, "backend", "console")
    ok = await mailer.send(
        to="ana@example.com", subject="Your reset code", text="Paste this:\n\n    cr_notarealcode"
    )
    out = capsys.readouterr().out
    assert ok
    assert "ana@example.com" in out
    assert "Your reset code" in out
    assert "cr_notarealcode" in out  # the code IS the payload - the console must carry it
    assert "nothing left this machine" in out  # the box says what the backend is


async def test_an_unknown_backend_drops_the_message_and_says_so(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.email, "backend", "postal-pigeon")
    ok = await mailer.send(to="ana@example.com", subject="s", text="t")
    assert ok is False
    assert "cr_" not in capsys.readouterr().out  # a misconfig never leaks the payload to stdout


def test_mask_recipient_keeps_enough_to_debug_and_nothing_to_harvest() -> None:
    assert mailer.mask_recipient("ana.lopes@example.com") == "a***@example.com"
    assert mailer.mask_recipient("not-an-address") == "***"


async def test_send_never_raises_even_when_stdout_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented contract, pinned at its weirdest edge: a non-UTF-8 stdout (or a torn pipe
    under nohup) choking on the box glyphs must cost a False, never an exception - in
    reset_password a raise here lands AFTER the commit and 500s an already-rotated password."""
    monkeypatch.setattr(settings.email, "backend", "console")

    def broken_stdout(*args: object, **kwargs: object) -> None:
        raise UnicodeEncodeError("ascii", "┌", 0, 1, "ordinal not in range")

    # Shadow the builtin in the mailer's own namespace only - patching builtins.print breaks
    # the test harness's logging machinery, which is not the contract under test.
    monkeypatch.setattr(mailer, "print", broken_stdout, raising=False)
    assert await mailer.send(to="ana@example.com", subject="s", text="t") is False


async def test_send_never_raises_on_a_bad_header_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EmailMessage construction sits inside the guard too: a header value with a newline raises
    ValueError at set time - before any socket - and that must also be a quiet False."""
    monkeypatch.setattr(settings.email, "backend", "smtp")
    ok = await mailer.send(to="ana@example.com", subject="two\nlines", text="t")
    assert ok is False
