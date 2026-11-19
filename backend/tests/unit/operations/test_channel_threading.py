# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One email thread, one conversation (fixed under ADR-057): the thread key is the
root Message-ID however the mail names it."""

from __future__ import annotations

from prismoid.operations.channels import email_thread_key


def test_the_first_mail_keys_on_its_own_id_and_replies_follow_it() -> None:
    first = email_thread_key({}, "Quick question", "<a@x>")
    reply = email_thread_key({"In-Reply-To": "<a@x>"}, "Re: Quick question", "<b@x>")
    deep = email_thread_key(
        {"References": "<a@x> <b@x>", "In-Reply-To": "<b@x>"}, "Re: Re: Quick question", "<c@x>"
    )
    assert first == reply == deep


def test_an_idless_mail_keys_on_its_normalized_subject() -> None:
    assert email_thread_key({}, "Re: Budget") == email_thread_key({}, "budget")
    assert email_thread_key({}, "") == "main"


def test_different_roots_are_different_threads() -> None:
    assert email_thread_key({}, "x", "<a@x>") != email_thread_key({}, "x", "<b@x>")


def test_header_names_fold_case_and_stacked_prefixes_are_one_thread() -> None:
    # RFC 5322 header names are case-insensitive; a lower-casing relay must not split a thread
    upper = email_thread_key({"In-Reply-To": "<a@x>"}, "Re: Budget", "<b@x>")
    lower = email_thread_key({"in-reply-to": "<a@x>"}, "Re: Budget", "<c@x>")
    assert upper == lower == email_thread_key({}, "x", "<a@x>")
    assert email_thread_key({}, "Re: Re: Fwd: Budget") == email_thread_key({}, "budget")
