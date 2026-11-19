# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The computer-use policy is data: a danger vocabulary with inflections, the
identity-provider hosts, and one host vet the gate and the settings resource share."""

from __future__ import annotations

import pytest

from prismoid.schemas.computer_use import (
    DEFAULT_ALLOWLIST,
    IDENTITY_PROVIDER_HOSTS,
    check_host,
    danger_hit,
)
from prismoid.schemas.egress import domain_allowed


# Real control labels: an irreversible act asks; a place, heading, byline or finished state stays quiet.
@pytest.mark.parametrize(
    ("label", "hit"),
    [
        ("Delete all orders", "delete"),
        ("Deleting…", "deleting"),
        ("Remove member", "remove"),
        ("Send invoice", "send"),
        ("Resend code", "resend"),
        ("Sent items", "sent"),
        ("Pay now", "pay"),
        ("Payment method", "payment"),
        ("Proceed to checkout", "checkout"),
        ("Buy", "buy"),
        ("Place order", "order"),
        ("Transfer funds", "transfer"),
        ("Publish post", "publish"),
        ("Bulk edit", "bulk"),
        ("Email the team", "email"),
        ("Purchase", "purchase"),
        ("Book a table for 4", "book"),
        ("Complete booking", "booking"),
        ("Reserve", "reserve"),
        ("Complete reservation", "reservation"),
        ("CONFIRM BOOKING", "confirm"),
        ("Submit application", "submit"),
        ("Resubmit", "resubmit"),
        ("Re-submit application", "re-submit"),
        ("Post reply", "post"),
        ("Repost", "repost"),
        ("Post code", "post"),  # an address form's submit asks: a question, never a miss
        ("Confirm password", "confirm"),  # the same: every sign-up submit asks
        ("Save changes", None),
        ("Deleted items (view)", None),  # a folder, not the act
        ("Undelete", None),
        ("Orders", None),  # a plural noun is a heading
        ("Bookings", None),
        ("Reservations", None),
        ("Reserves", None),
        ("Transfers", "transfers"),  # the older stems keep their -s form
        ("Emails", "emails"),
        ("Books", None),  # a department
        ("Bookmark this page", None),
        ("Booked", None),  # a status badge
        ("Confirmed", None),
        ("Confirmation number", None),
        ("Submitted", None),
        ("Posted by Ana", None),  # a byline
        ("Postcode", None),
        ("Compost", None),
        ("Add to cart", None),
    ],
)
def test_danger_hit_asks_on_the_act_and_stays_quiet_on_a_label(label: str, hit: str | None) -> None:
    assert danger_hit([label]) == hit


def test_danger_hit_finds_nothing_in_no_names() -> None:
    assert danger_hit([]) is None


def test_danger_hit_checks_every_name_and_returns_the_first_match() -> None:
    assert danger_hit(["Cancel", "Continue", "Delete row"]) == "delete"
    assert danger_hit(["Continue", "Confirm and pay"]) == "confirm"


def test_identity_providers_seed_the_default_allowlist() -> None:
    assert "accounts.google.com" in IDENTITY_PROVIDER_HOSTS
    assert "login.microsoftonline.com" in IDENTITY_PROVIDER_HOSTS
    assert set(IDENTITY_PROVIDER_HOSTS) <= set(DEFAULT_ALLOWLIST)


@pytest.mark.parametrize(
    "tenant", ["evil-tenant.us.auth0.com", "dev-123456.okta.com", "okta.com", "auth0.com"]
)
def test_no_multi_tenant_identity_apex_is_allowed_by_default(tenant: str) -> None:
    """`okta.com` and `auth0.com` match every tenant, an attacker's included, so a
    page could steer the browser to one with no ask. They are not defaults; a person's own tenant
    asks once, and "always" keeps it. Google, Microsoft and Apple sign-in stay."""
    assert not domain_allowed(tenant, DEFAULT_ALLOWLIST)
    assert domain_allowed("appleid.apple.com", DEFAULT_ALLOWLIST)


@pytest.mark.parametrize("host", ["Admin.Example.com.", "admin.example.com", "a-b.example.co.uk"])
def test_check_host_normalizes(host: str) -> None:
    assert check_host(host) == host.lower().rstrip(".")


@pytest.mark.parametrize(
    "host",
    [
        "",
        "localhost",
        "example",
        "*.example.com",
        "https://example.com",
        "example.com/path",
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.8",
        "[::1]",
        "exa mple.com",
    ],
)
def test_check_host_refuses_what_a_gate_must_never_offer(host: str) -> None:
    with pytest.raises(ValueError):
        check_host(host)


def test_domain_allowed_is_a_dot_anchored_suffix_match() -> None:
    assert domain_allowed("example.com", {"example.com"})
    assert domain_allowed("api.example.com", {"example.com"})
    assert not domain_allowed("notexample.com", {"example.com"})
    assert not domain_allowed("evil-example.com", {"example.com"})
