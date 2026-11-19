# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The egress door - the pure vocabulary of *may this process dial that address?* (ADR-046).

Two doors share it, with different tempers. The connectors door vets a handful of
**user-configured** MCP servers; the web door vets **model-authored** URLs, which is
the DNS-rebinding threat model itself. What is common - every spelling of a loopback/private/
metadata literal, the refusal grammar for a resolved address, control-character stripping for
text that will enter a prompt - lives here, once. What differs - each door's message
vocabulary, and the resolve-time pinning that needs I/O - lives with its door: connectors keep
`check_server_url` (built on these primitives, messages unchanged), and `ai/tools/fetch.py`
does the resolving, because schemas never resolve, never dial, never wait.

Moved out of `schemas/connectors.py`: the door check was connector-private
until the fetch tool needed the same literals. Replacement, not duplication - connectors
re-export the shared names, and there is exactly one copy of each rule.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_localhost(host: str) -> bool:
    return host.lower() in _LOCAL_HOSTS


def ip_literal(host: str) -> IPAddress | None:
    """Every spelling the platform resolver short-circuits as an ADDRESS, normalized - or None
    for a name that will genuinely go to DNS. `ipaddress` covers dotted-quad and IPv6 (zone
    ids included); `inet_aton` covers the C-library spellings that dial loopback while parsing
    as neither - decimal (`2130706433`), hex (`0x7f000001`), octal (`0177.0.0.1`), short-form
    (`127.1`). An IPv4-mapped IPv6 literal (`::ffff:127.0.0.1`) unwraps to the address it
    reaches on a dual-stack host. Parsing only - nothing here resolves."""
    bare = host.rstrip(".")  # `127.0.0.1.` is the same literal in FQDN dress
    addr: IPAddress | None = None
    try:
        addr = ipaddress.ip_address(bare)
    except ValueError:
        try:
            addr = ipaddress.IPv4Address(socket.inet_aton(bare))
        except OSError:
            return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def refuse_ip(addr: IPAddress, *, allow_loopback: bool = False) -> str | None:
    """The reason a vetted dial must not reach this address - or None for the public internet.

    One classification, used at BOTH depths: on a literal at parse time, and on every resolved
    answer at connect time (the fetch tool's resolve-and-pin). An IPv4-mapped IPv6 address is
    judged as the IPv4 it reaches - `::ffff:10.0.0.1` is 10.0.0.1 wearing a costume.

    **The test is `is_global`, not a hand-written list of ranges**:
    an enumeration of private/link-local/reserved/multicast/unspecified LOOKS complete and is
    not - RFC 6598 carrier-grade NAT (`100.64.0.0/10`) is none of those five, and it is where
    Tailscale puts an entire mesh and where several clouds put internal endpoints. Anything the
    stdlib does not call globally routable is refused, so a range added to a future Python is
    covered the day we run on it. Two ranges ARE globally routable and still must not be dialed,
    so they stay named: IPv4 multicast (`224.0.0.0/4`, `is_global` True) and the reserved space
    that carries NAT64 (`64:ff9b::/96`, `is_global` True, translating straight to an inside
    address on a NAT64 network). Union, not replacement - strictly stronger than either half.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return None if allow_loopback else "is loopback - this machine, not the web"
    if not addr.is_global or addr.is_multicast or addr.is_reserved:
        return "points inside the network, not at the public web"
    return None


def check_server_url(url: str, *, allow_insecure_localhost: bool) -> str:
    """The one gate a user-supplied MCP server URL passes before this process will ever dial it
    (messages unchanged from when it lived in connectors).

    Raises `ValueError` with the user-facing reason (operations turns it into a 422). Pure and
    resolution-free by design - a literal private/link-local/metadata IP and the loopback name
    are refused here; the DNS-rebinding depth (resolve-and-pin on every request) is
    `ai/tools/pin.py`'s, which both doors mount (it closes the connector door's deferral).
    """
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if not host:
        raise ValueError("that is not a URL a server can be reached at")
    if "@" in parts.netloc:
        raise ValueError("a server URL must not carry credentials in it")
    if parts.scheme == "http":
        if not (allow_insecure_localhost and is_localhost(host)):
            raise ValueError("MCP servers must be https (http is allowed only for localhost, in dev)")
    elif parts.scheme != "https":
        raise ValueError("MCP servers must be https")
    if is_localhost(host.rstrip(".")) and not allow_insecure_localhost:
        # The NAME over https too: only the http branch used to ask, so
        # `https://localhost:<port>` reached this process's own services.
        raise ValueError("loopback MCP servers are a dev-only setting")
    addr = ip_literal(host)
    if addr is None:
        if not any(ch.isalpha() for ch in host):
            # Digits-and-dots that even inet_aton refuses ("1.2.3.4.5") are not DNS names
            # either - a public TLD is never all-numeric. Refuse, rather than hand the
            # resolver an exotic literal dressed as a "name".
            raise ValueError("that is not a valid server address")
        return url.strip()  # a DNS name - the pinned client vets what it resolves to
    if addr.is_loopback:
        if not allow_insecure_localhost:
            raise ValueError("loopback MCP servers are a dev-only setting")
    elif refuse_ip(addr, allow_loopback=True) is not None:
        raise ValueError("that address points inside the network, not at a service")
    return url.strip()


def vet_fetch_url(url: str, *, allow_insecure_localhost: bool = False) -> str:
    """The web door's parse-time gate: what `web_fetch` accepts before resolving.

    Same skeleton as `check_server_url`, different temper: http IS allowed (the tool upgrades
    to https first and falls back honestly - the public web still has http-only pages), and the
    messages speak fetch. Raises `ValueError` with the reason; the resolve-time half - every
    DNS answer through `refuse_ip`, connection pinned to the vetted address, every redirect hop
    re-vetted - lives in `ai/tools/fetch.py`, the one caller that may do I/O.
    """
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"only http(s) URLs can be fetched - {parts.scheme or 'that'!r} is not a web address"
        )
    if not host:
        raise ValueError("that is not a URL a page can be fetched from")
    if "@" in parts.netloc:
        raise ValueError("a URL carrying credentials is refused - never fetch as somebody")
    if is_localhost(host) and not allow_insecure_localhost:
        # The NAME is refused at parse time; the resolve-and-pin depth in `ai/tools/fetch.py`
        # catches every other name that merely resolves here.
        raise ValueError("that address is loopback - this machine, not the web")
    addr = ip_literal(host)
    if addr is None:
        if not any(ch.isalpha() for ch in host):
            raise ValueError("that is not a valid web address")
    else:
        reason = refuse_ip(addr, allow_loopback=allow_insecure_localhost)
        if reason is not None:
            raise ValueError(f"that address {reason}")
    # The fragment is the reader's business, never the wire's - and stripping it here keeps the
    # page cache keyed on what was actually fetched. The host is case-insensitive by DNS law, so
    # it is lowercased for the same reason: `EXAMPLE.com` and `example.com` were two cache keys
    # and two budget units for one page. Only the netloc - a path and
    # a query are case-SENSITIVE and are somebody else's to define.
    netloc = parts.netloc.lower() if parts.netloc else parts.netloc
    return parts._replace(netloc=netloc, fragment="").geturl().strip()


# ── sites a text names ────────────────────────────────────────────────────────────────────────

# What a URL may carry past its host, and so where one written in prose ends.
_URL_TAIL: Final = r"[^\s<>\"')\]]"
# `(?![a-z0-9.-]{254})`: no host is longer than 253 characters (RFC 1035). Without the bound a long
# dotted or dashed run that never ends in a top-level label was rescanned from every start inside it
# (2.9 s for 20k characters); with it each start reads at most 254 characters.
_SITE_RE: Final = re.compile(
    rf"(?P<url>https?://{_URL_TAIL}+)"
    rf"|\b(?![a-z0-9.-]{{254}})(?P<host>(?:[a-z0-9-]+\.)+[a-z]{{2,24}})\b(?P<path>/{_URL_TAIL}*)?",
    re.IGNORECASE,
)
_URL_END: Final = ".,;:!?"  # sentence punctuation after a link closes the sentence, not the link


@dataclass(frozen=True, slots=True)
class Site:
    """One site a text names: an http(s) URL as written, or a bare host and the path typed after it;
    `start`/`end` span the host for a bare one and the whole URL for a link."""

    host: str
    start: int
    end: int
    url: str | None  # the link as written, trailing sentence punctuation dropped; None for a bare host
    path: str = ""  # the path typed after a bare host, as written ("" when none)


def url_host(url: str) -> str:
    """The URL's host, lowercased; `""` when it names none or will not parse (an unbalanced IPv6
    bracket: the URL pattern stops at a closing bracket)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def sites_in(text: str) -> list[Site]:
    """Every site `text` names, in order: the one URL and host extractor (ADR-046). Linear in the
    text; a bare host is never read inside a URL, and nothing here resolves or dials."""
    sites: list[Site] = []
    for match in _SITE_RE.finditer(text or ""):
        if match.group("url") is not None:
            url = match.group("url").rstrip(_URL_END)
            sites.append(Site(url_host(url), match.start(), match.start() + len(url), url))
        else:
            host = match.group("host")
            sites.append(
                Site(host.lower(), match.start("host"), match.end("host"), None, match.group("path") or "")
            )
    return sites


def said_in(text: str, host: str) -> bool:
    """True when `host` appears in `text` on host boundaries - the person's own words."""
    return re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", text, re.IGNORECASE) is not None


# ── untrusted text ────────────────────────────────────────────────────────────────────────────

_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control(text: str) -> str:
    """Control characters go before somebody else's text enters a prompt - the connector rule for
    server prose, shared by the web door for page content. Length is the CALLER's law: the
    connectors door caps at the cache step, the fetch tool paginates."""
    return _CONTROL_RE.sub("", text or "")


class Taint(StrEnum):
    """Whose text a run has read, from least to most foreign; a run's taint is the highest it read."""

    TRUSTED = "trusted"  # Prismoid wrote it: prompts, tool schemas, its own tool replies
    USER = "user"  # a person on the task wrote it: the goal, answers, steers, their files
    THIRD_PARTY = "third_party"  # anyone else: pages, search results, a browser, a connected service


_TAINT_ORDER: Final = tuple(Taint)


def joined(*taints: Taint) -> Taint:
    """The highest of `taints` (`TRUSTED` for none): what text from all of them together is."""
    return max(taints, key=_TAINT_ORDER.index, default=Taint.TRUSTED)


# The run of bars every tool fence line wears (`web_fetch`, `web_search`, the browser's page
# fence, the change judge's data blocks).
FENCE_MARK: Final = "―――"


def fence_guarded(text: str) -> str:
    """Untrusted text cannot spoof the fence boundary it is about to be wrapped in: every run of
    the fence's own marker inside it is broken up before wrapping, so a page, a search result or
    an agent's report that carries a fake end-of-content line cannot close the real fence early
    and pose as instructions. One helper serves every fence."""
    return text.replace(FENCE_MARK, "- - -")


def fence_open(title: str, warning: str, *, heading: str = "") -> str:
    """A fence's first line: what the block is (`heading`, else `title`) and the warning that
    what follows is data."""
    return f"{FENCE_MARK} {heading or title} · {warning} {FENCE_MARK}"


def fence_close(title: str) -> str:
    return f"{FENCE_MARK} end of {title} {FENCE_MARK}"


def fenced(title: str, warning: str, body: str, *, heading: str = "", nonce: str | None = None) -> str:
    """Untrusted `body` between an opening data line and a closing line it cannot forge; with a `nonce`
    the data also sits between `⟦data:<nonce>⟧` lines, the nonce escaped in the body."""
    guarded = fence_guarded(body)
    if nonce is not None:
        escaped = guarded.replace(nonce, "·" * len(nonce))
        guarded = f"⟦data:{nonce}⟧\n{escaped}\n⟦/data:{nonce}⟧"
    return f"{fence_open(title, warning, heading=heading)}\n{guarded}\n{fence_close(title)}"


def one_line(text: str, cap: int) -> str:
    """Untrusted text for a header or a list row: whitespace collapsed to single spaces, control
    characters stripped, the fence marker broken up, capped."""
    return fence_guarded(strip_control(" ".join(text.split())))[:cap]


def domain_allowed(host: str, allowed: Iterable[str]) -> bool:
    """`example.com` allows `example.com` and `api.example.com` - never `notexample.com`.

    The dot anchor is the whole point: a plain `endswith` would let `evil-example.com` through an
    allowlist for `example.com`, which is the classic way an allowlist becomes decorative."""
    host = host.lower().rstrip(".")
    for domain in allowed:
        d = domain.lower().rstrip(".")
        if host == d or host.endswith(f".{d}"):
            return True
    return False
