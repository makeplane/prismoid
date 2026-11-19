# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Computer use, as data: the danger vocabulary, the identity-provider hosts
the default allowlist seeds, the hosts Chromium calls on its own, the one host vet the gate's `always` scope and the settings
resource share, and the check of one act (`ActCheck`: did it land, on the browser's own facts).
Pure and stdlib-only: the proxy may one day mount it the way it mounts `egress.py`. Workspace overrides arrive later; every misroute becomes an
eval row, never a special case here.

`DANGER_FORMS` lists only the inflections that name the act: the book, reserve, confirm, submit and
post stems leave out the -s form, the plural and a state-naming participle ("Bookings", "Posted by"
are headings), and `delete` has no past participle ("Deleted items" is a folder). The model's own
`confirm` covers the rest, and the golden suite grows a row per miss."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast
from urllib.parse import urlsplit

from prismoid.schemas.egress import ip_literal, is_localhost, refuse_ip, sites_in

# Hosts Chromium calls on its own - component and model updates, autofill and safe-browsing
# lookups - however a page or the agent behaves. The launch flags (`sidecar/browser.py`) silence
# most of them; one that slips through is not the agent's activity, so it is no call's row.
BROWSER_BACKGROUND: Final = frozenset(
    {
        "update.googleapis.com",
        "clients1.google.com",
        "clients2.google.com",
        "content-autofill.googleapis.com",
        "optimizationguide-pa.googleapis.com",
        "safebrowsing.googleapis.com",
        "redirector.gvt1.com",
    }
)

# stem → the inflections that count as the act; the older stems keep theirs until the ask rate is measured
DANGER_FORMS: Final[dict[str, tuple[str, ...]]] = {
    "pay": ("pay", "pays", "paid", "paying", "payment"),
    "purchase": ("purchase", "purchases", "purchased", "purchasing"),
    "checkout": ("checkout", "check out"),
    "buy": ("buy", "buys", "bought", "buying"),
    "order": ("order", "ordered", "ordering"),
    "send": ("send", "sends", "sent", "sending"),
    "email": ("email", "emails", "emailed", "emailing", "e-mail"),
    "delete": ("delete", "deletes", "deleting"),
    "remove": ("remove", "removes", "removed", "removing"),
    "transfer": ("transfer", "transfers", "transferred", "transferring"),
    "publish": ("publish", "publishes", "published", "publishing"),
    "bulk": ("bulk",),
    "book": ("book", "booking"),
    "reserve": ("reserve", "reserving", "reservation"),
    "confirm": ("confirm", "confirming"),
    "submit": ("submit", "submitting"),
    "post": ("post", "posting"),
}
DANGER_STEMS: Final[tuple[str, ...]] = tuple(DANGER_FORMS)

_FORMS: Final[tuple[str, ...]] = tuple(
    sorted({f for forms in DANGER_FORMS.values() for f in forms}, key=len, reverse=True)
)
# a repeat of the act is the act ("Resubmit", "Re-send"); any other prefix is another word ("Undelete")
_DANGER_RE: Final = re.compile(
    r"(?<![\w-])((?:re-?)?(?:" + "|".join(re.escape(f) for f in _FORMS) + r"))(?![\w-])",
    re.IGNORECASE,
)


def danger_hit(names: Iterable[str]) -> str | None:
    """The first vocabulary word found in any of `names`, lowercased; None when none."""
    for name in names:
        found = _DANGER_RE.search(name or "")
        if found:
            return found.group(1).lower()
    return None


# Single-tenant sign-in hosts only. `okta.com` and `auth0.com` are not here: matched by suffix they
# admit every tenant, an attacker's free one included, so a page
# could steer the browser there with no ask. A person's own tenant asks once; "always" keeps it.
IDENTITY_PROVIDER_HOSTS: Final[tuple[str, ...]] = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "appleid.apple.com",
)
# v1's shipped default allowlist is the providers and nothing else.
DEFAULT_ALLOWLIST: Final[tuple[str, ...]] = IDENTITY_PROVIDER_HOSTS

_LABEL_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def check_host(host: str) -> str:
    """Normalize a host a human typed or a page named, or raise ValueError with the reason.

    A gate may only ever OFFER a public host with at least two labels: an address the egress vet
    refuses, localhost, a wildcard, a scheme or a path are never askable."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        raise ValueError("empty host")
    if "://" in h or "/" in h or "?" in h or "#" in h:
        raise ValueError("a host, not a URL")
    if "*" in h:
        raise ValueError("wildcards are not allowed")
    if is_localhost(h):
        raise ValueError("localhost is never askable")
    h_ip = h[1:-1] if h.startswith("[") and h.endswith("]") else h
    literal = ip_literal(h_ip)
    if literal is not None:
        reason = refuse_ip(literal)
        raise ValueError(reason or "IP literals are not allowed; name the host")
    labels = h.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        raise ValueError("not a public hostname")
    return h


# A step opens at most this many hosts the person named without asking.
MAX_NAMED_HOSTS: Final = 5
# A bare host that is really a filename ("report.md", "notes.csv") reads as a host to the site extractor;
# the extension is the tell.
FILE_EXTENSIONS: Final = frozenset(
    {"txt", "md", "py", "csv", "json", "pdf", "docx", "xlsx", "pptx", "html", "js", "ts", "yaml", "yml"}
    | {"toml", "png", "jpg", "jpeg", "zip", "tar", "gz"}
)


def named_hosts(raw: object, *, limit: int = MAX_NAMED_HOSTS) -> tuple[str, ...]:
    """Hosts from what the person named, each through `check_host` (a URL cut to its host), invalid ones
    dropped, deduplicated, in order, at most `limit`; anything but a list or tuple names none."""
    items = cast("Sequence[object]", raw) if isinstance(raw, list | tuple) else ()
    kept: list[str] = []
    for item in items:
        text = str(item).strip()
        if "://" in text:
            text = urlsplit(text).hostname or ""
        try:
            host = check_host(text)
        except ValueError:
            continue
        if host not in kept:
            kept.append(host)
    return tuple(kept[:limit])


ACTING_VERBS: Final[frozenset[str]] = frozenset(
    {"open", "back", "click", "type", "fill", "select", "press", "scroll"}
)
RECORDED_VERBS: Final[frozenset[str]] = ACTING_VERBS | {"screenshot"}
# The acts a person may be asked about (they carry the approver's `expect`): the ones a step's work rests on.
GUARDED_VERBS: Final[frozenset[str]] = frozenset({"click", "type", "fill", "select", "press"})


class Landed(StrEnum):
    """Whether an act reached the page, as the browser's own facts say (never its words)."""

    LEFT = "left"  # it took the browser to another document or tab
    CHANGED = "changed"  # the page changed under it
    UNSEEN = "unseen"  # it ran; its change is one the probe cannot see (a field's value, a toggle)
    UNCHANGED = "unchanged"  # it ran, and the page did not change at all
    FAILED = "failed"  # the browser refused it: nothing (or not all of it) was done


_MISSED: Final = frozenset({Landed.UNCHANGED, Landed.FAILED})
_CHANGES: Final[dict[object, Landed]] = {
    "navigated": Landed.LEFT,
    "changed": Landed.CHANGED,
    "none": Landed.UNCHANGED,
}


@dataclass(frozen=True, slots=True)
class ActCheck:
    """One act, checked once: the verb, whether it landed, and whether the page settled after it."""

    verb: str
    landed: Landed
    settled: bool = True

    @property
    def missed(self) -> bool:
        """The act did not land: refused, or it ran and the page did not move."""
        return self.landed in _MISSED


def act_check(verb: str, result: Mapping[str, object]) -> ActCheck:
    """The check of one sidecar answer, from its typed facts (`ok`, `moved`, `change`, `settled`)."""
    if not result.get("ok"):
        landed = Landed.FAILED
    elif result.get("moved"):
        landed = Landed.LEFT
    else:
        landed = _CHANGES.get(result.get("change"), Landed.UNSEEN)
    return ActCheck(verb, landed, settled=result.get("settled") is not False)


def named_sites(text: str) -> tuple[str, ...]:
    """The public hosts `text` names (URL hosts and bare hosts, never a filename), each through
    `named_hosts`: what a browser step may open without asking, because the person typed it."""
    hosts = {
        site.host.removeprefix("www.")
        for site in sites_in(text or "")
        if site.host and (site.url is not None or site.host.rsplit(".", 1)[-1] not in FILE_EXTENSIONS)
    }
    return named_hosts(sorted(hosts))
