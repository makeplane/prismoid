# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deterministic half of the OAuth client (ADR-020), pinned.

The point of putting these in `schemas/` as pure functions is exactly this file: the whole
discovery/PKCE/exchange grammar verified in microseconds, no server stood up. The PKCE case is
RFC 7636's own Appendix B vector - a known-answer test, not a round trip that could be wrong
twice in the same way.
"""

from __future__ import annotations

import pytest

from prismoid.schemas.connectors import (
    auth_server_metadata_urls,
    authorize_url,
    check_server_url,
    default_endpoints,
    pkce_pair,
    protected_resource_urls,
    refresh_request,
    resource_metadata_from_www_auth,
    s256_challenge,
    sanitize_server_text,
    slugify,
    token_request,
)


def test_pkce_known_answer_rfc7636_appendix_b():
    assert (
        s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_pkce_pair_shape():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and challenge == s256_challenge(verifier)
    assert "=" not in challenge, "unpadded base64url, per the RFC"


def test_protected_resource_urls_path_insertion_first():
    assert protected_resource_urls("https://mcp.example.com/mcp") == [
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    ]
    assert protected_resource_urls("https://mcp.example.com") == [
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    ]


def test_auth_server_metadata_urls_walk_order():
    assert auth_server_metadata_urls("https://auth.example.com/tenant") == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
        "https://auth.example.com/.well-known/oauth-authorization-server",
        "https://auth.example.com/.well-known/openid-configuration/tenant",
        "https://auth.example.com/tenant/.well-known/openid-configuration",
    ]


def test_default_endpoints_are_the_mcp_spec_fallback():
    assert default_endpoints("https://auth.example.com/anything") == {
        "authorization_endpoint": "https://auth.example.com/authorize",
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
    }


def test_authorize_url_carries_the_whole_grammar():
    url = authorize_url(
        "https://auth.example.com/authorize",
        client_id="abc",
        redirect_uri="https://api.plane.local/v1/connectors/oauth/callback",
        state="cn_state",
        code_challenge="chal",
        resource="https://mcp.example.com/mcp",
        scopes="read write",
    )
    for piece in (
        "response_type=code",
        "client_id=abc",
        "state=cn_state",
        "code_challenge=chal",
        "code_challenge_method=S256",
        "scope=read+write",
        "resource=https%3A%2F%2Fmcp.example.com%2Fmcp",
    ):
        assert piece in url
    assert (
        authorize_url(
            "https://a.example/authorize?audience=x",
            client_id="c",
            redirect_uri="r",
            state="s",
            code_challenge="k",
        ).count("?")
        == 1
    ), "an endpoint that already has a query gets & not a second ?"


def test_token_request_auth_methods():
    data, headers = token_request(
        code="thecode",
        code_verifier="v",
        redirect_uri="r",
        client_id="id",
        client_secret="sec",
        auth_method="client_secret_basic",
    )
    assert headers["Authorization"].startswith("Basic ") and "client_secret" not in data
    assert "client_id" not in data, "basic auth carries the id in the header, not the form"

    data, headers = token_request(
        code="thecode",
        code_verifier="v",
        redirect_uri="r",
        client_id="id",
        client_secret="sec",
        auth_method="client_secret_post",
    )
    assert headers == {} and data["client_secret"] == "sec" and data["client_id"] == "id"

    data, headers = token_request(
        code="thecode",
        code_verifier="v",
        redirect_uri="r",
        client_id="id",
        client_secret=None,
        auth_method="none",
        resource="https://m.example/mcp",
    )
    assert headers == {} and "client_secret" not in data
    assert data["resource"] == "https://m.example/mcp" and data["grant_type"] == "authorization_code"


def test_refresh_request_shape():
    data, _ = refresh_request(refresh_token="rt", client_id="id", client_secret=None, auth_method="none")
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt", "client_id": "id"}


def test_www_authenticate_hint():
    header = 'Bearer error="invalid_token", resource_metadata="https://m.example/.well-known/oauth-protected-resource"'
    assert resource_metadata_from_www_auth(header) == (
        "https://m.example/.well-known/oauth-protected-resource"
    )
    assert resource_metadata_from_www_auth("Bearer realm=x") is None
    assert resource_metadata_from_www_auth("") is None


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://mcp.notion.com/mcp", True),
        ("http://localhost:9004/mcp", True),  # dev flag on
        ("http://127.0.0.1:9004/mcp", True),
        ("http://mcp.example.com/mcp", False),  # http off-box
        ("ftp://mcp.example.com", False),
        ("https://user:pw@mcp.example.com/mcp", False),  # credentials in URL
        ("https://10.0.0.8/mcp", False),  # private
        ("https://169.254.169.254/latest", False),  # cloud metadata
        ("https://192.168.1.10/mcp", False),
        ("https://224.0.0.1/mcp", False),  # multicast
        ("https://0.0.0.0/mcp", False),
        ("not a url", False),
    ],
)
def test_check_server_url_table(url: str, ok: bool):
    if ok:
        assert check_server_url(url, allow_insecure_localhost=True) == url
    else:
        with pytest.raises(ValueError):
            check_server_url(url, allow_insecure_localhost=True)


def test_check_server_url_localhost_is_a_dev_flag():
    with pytest.raises(ValueError):
        check_server_url("http://localhost:9004/mcp", allow_insecure_localhost=False)
    # The name over https too: only the http branch used to ask.
    for named in ("https://localhost:9004/mcp", "https://LOCALHOST/mcp", "https://localhost./mcp"):
        with pytest.raises(ValueError):
            check_server_url(named, allow_insecure_localhost=False)
    assert check_server_url("https://localhost/mcp", allow_insecure_localhost=True)
    with pytest.raises(ValueError):
        check_server_url("https://127.0.0.1/mcp", allow_insecure_localhost=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # decimal 127.0.0.1
        "https://0x7f000001/",  # hex 127.0.0.1
        "https://127.1/mcp",  # short-form loopback
        "https://0177.0.0.1/",  # octal loopback
        "https://2852039166/latest",  # decimal 169.254.169.254 - the cloud metadata service
        "https://[::ffff:127.0.0.1]/mcp",  # IPv4-mapped IPv6 loopback (the dual-stack dial)
        "https://127.0.0.1./mcp",  # the same literal in FQDN dress
        "https://0/mcp",  # inet_aton zero - the unspecified address
        "https://1.2.3.4.5/mcp",  # parses as nothing everywhere and must not reach a resolver
    ],
)
def test_check_server_url_refuses_unnormalized_ip_literals(url: str):
    """The bypass table: every spelling the C library collapses to a
    loopback/private/metadata address is refused BEFORE any resolver sees it - `2130706433`
    IS 127.0.0.1 to `getaddrinfo`, and calling it a DNS name was the door's one gap."""
    with pytest.raises(ValueError):
        check_server_url(url, allow_insecure_localhost=False)


def test_check_server_url_normalized_loopback_honors_the_dev_flag():
    # Dev keeps loopback in any spelling; real DNS names pass untouched either way.
    assert check_server_url("https://127.1/mcp", allow_insecure_localhost=True) == "https://127.1/mcp"
    assert (
        check_server_url("https://mcp.notion.com/mcp", allow_insecure_localhost=False)
        == "https://mcp.notion.com/mcp"
    )


def test_slugify_is_identifier_safe_and_deterministic():
    assert slugify("Gmail") == "gmail"
    assert slugify("My Notion (work)") == "my_notion_work"
    assert slugify("123 files") == "c_123_files"
    assert slugify("!!!") == "c"
    long = slugify("x" * 99)
    assert len(long) <= 32 and slugify("x" * 99) == long


def test_sanitize_server_text_strips_control_and_caps():
    dirty = "Use\x00 me\x1b[31m wisely\n  ok"
    assert sanitize_server_text(dirty, 12) == "Use me[31m w"
    assert sanitize_server_text(None, 10) == ""  # pyright: ignore[reportArgumentType]
