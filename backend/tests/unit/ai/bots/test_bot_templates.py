# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The template lane's pure half (ADR-056): the secret net, the URL surfacing, the ref."""

from __future__ import annotations

import pytest

from prismoid.schemas.bot_templates import TemplateRef, external_urls, scan_for_secrets


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("here is ck_abcdefghijklmnopqrstuv123 pasted", "a Plane API key"),
        ("token rt_ABCDEFGHIJKLMNOPQRSTUVWX", "a routine fire token"),
        ("gAAAAABlongciphertextpayload1234567890", "Fernet ciphertext"),
        ("use sk-proj-abcdefghijklmnopqrstuvwx", "an OpenAI-style key"),
        ("slack xoxb-1234567890-abcdefghij", "a Slack bot token"),
        ("gh ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12", "a GitHub token"),
        ("aws AKIAIOSFODNN7EXAMPLE", "an AWS access key id"),
        ("-----BEGIN RSA PRIVATE KEY-----", "a private key block"),
    ],
)
def test_the_net_names_each_credential_shape(text: str, expected: str) -> None:
    assert expected in scan_for_secrets(text)


def test_clean_prose_passes_the_net() -> None:
    """The false-positive guards: ordinary bot text mentioning tasks, risk, and skus."""
    clean = (
        "Review every clause. Flag risks. The tracking id looks like TASK-1234. "
        "Prices are in USD. Ask about SKU groups. The v5 reviewer checks drafts. "
        "Escalate to legal when a clause caps liability under 12 months."
    )
    assert scan_for_secrets(clean) == []


def test_external_urls_dedupe_in_order_and_trim_punctuation() -> None:
    text = "Read https://docs.example.com/a, then https://docs.example.com/a and http://b.example.io."
    assert external_urls(text) == ["https://docs.example.com/a", "http://b.example.io"]


def test_template_ref_renders_the_provenance_string() -> None:
    assert TemplateRef(slug="bt_abc", version=3).source() == "template:bt_abc@v3"
