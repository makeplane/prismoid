# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connector catalog - known remote MCP servers, as data (ADR-054).

Catalog entries are data: adding a provider is a data edit here, never a deploy of new behavior.
Entries are frozen pydantic rows validated at import, so a typo'd URL or an
uppercase keyword is a red test, not a runtime surprise. `GET /v1/connectors/catalog` serves
this tuple verbatim; the web renders it as cards and runs `suggest`'s grammar client-side;
the CLI resolves names through the API (the old `runner/default_connectors.yaml` died when
this file was born - one source of truth).

Also home to `suggest`, the ONE deterministic intent matcher (pure, word-boundary, no model,
no I/O): the composer's chip and `create_task`'s auto-attach speak the same grammar, so what
the user saw offered is what the door would have attached. A learned router here would be an
unauthenticated control plane (the doorway lesson); a regex is auditable and free.

Every seed URL must pass a live probe before it lands (`just connectors-probe-catalog`):
several vendors publish SSE-only endpoints the streamable-HTTP transport cannot speak, and a
catalog card that cannot connect is worse than no card.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from prismoid.schemas.connectors import ToolPolicy


class CatalogAuth(StrEnum):
    OAUTH = "oauth"  # the server will 401 the probe and walk the OAuth dance
    OPEN = "open"  # answers an anonymous initialize; live on create


class CatalogEntry(BaseModel):
    """One known provider. `slug` doubles as the web's brand-icon key (unless `icon` aliases
    it) and the default connector name; `keywords` are the suggestion grammar's brand half -
    conservative on purpose, because auto-attach rides them (generic nouns over-trigger; the
    connector's own probed tool names carry the verb half, see `operations.connectors`)."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    vendor: str
    description: str
    category: str
    server_url: str
    auth: CatalogAuth
    icon: str = ""  # brand mark key; empty = the slug itself
    docs_url: str = ""
    # One honest sentence about connecting, when the dance has a catch the card should say
    # up front (e.g. Google ships no dynamic client registration - you bring an OAuth client).
    auth_note: str = ""
    keywords: tuple[str, ...] = ()
    # Policy overrides seeded onto a row created from this entry (owner-editable after).
    recommended_policies: dict[str, ToolPolicy] = {}

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", v):
            raise ValueError("slug must be lowercase [a-z0-9_], letter-first, <=32 chars")
        return v

    @field_validator("server_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("catalog servers are https only")
        return v

    @field_validator("keywords")
    @classmethod
    def _keywords_shape(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for kw in v:
            if kw != kw.lower().strip() or len(kw) < 3:
                raise ValueError(f"keyword {kw!r} must be lowercase, trimmed, >=3 chars")
        return v


# ── the seed (every URL
# below answered a LIVE probe on landing day - `just connectors-catalog-probe` re-proves it;
# growth stays a data edit + one probe run) ──────────────────────────────────────────────────


def _e(
    slug: str,
    name: str,
    vendor: str,
    description: str,
    category: str,
    server_url: str,
    auth: CatalogAuth,
    *,
    icon: str = "",
    docs_url: str = "",
    keywords: tuple[str, ...] = (),
    auth_note: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        slug=slug,
        name=name,
        vendor=vendor,
        description=description,
        category=category,
        server_url=server_url,
        auth=auth,
        icon=icon,
        docs_url=docs_url,
        keywords=keywords,
        auth_note=auth_note,
    )


# Google ships the Workspace MCP servers WITHOUT dynamic client registration: connecting
# needs an OAuth client id + secret from the user's own Google Cloud project. Said once here,
# worn by every Google entry - the card tells the truth before the first click.
_GOOGLE_NOTE = (
    "Google requires your own OAuth client (no automatic registration): create one in Google "
    "Cloud, then connect with its client id and secret."
)

CATALOG: Final[tuple[CatalogEntry, ...]] = (
    # ── Productivity + knowledge ──────────────────────────────────────────────────────────
    _e(
        "notion",
        "Notion",
        "Notion",
        "Search, read, and create pages and databases in your Notion workspace.",
        "Productivity",
        "https://mcp.notion.com/mcp",
        CatalogAuth.OAUTH,
        docs_url="https://developers.notion.com/docs/mcp",
        keywords=("notion",),
    ),
    _e(
        "todoist",
        "Todoist",
        "Doist",
        "Read and manage your Todoist tasks and projects.",
        "Productivity",
        "https://ai.todoist.net/mcp",
        CatalogAuth.OAUTH,
        keywords=("todoist",),
    ),
    _e(
        "fireflies",
        "Fireflies",
        "Fireflies.ai",
        "Search your meeting transcripts, notes, and action items.",
        "Meetings",
        "https://api.fireflies.ai/mcp",
        CatalogAuth.OAUTH,
        keywords=("fireflies", "transcript"),
    ),
    # ── Project management + work tracking ────────────────────────────────────────────────
    _e(
        "linear",
        "Linear",
        "Linear",
        "Find, create, and update issues, projects, and cycles in Linear.",
        "Project management",
        "https://mcp.linear.app/mcp",
        CatalogAuth.OAUTH,
        docs_url="https://linear.app/docs/mcp",
        keywords=("linear",),
    ),
    _e(
        "plane",
        "Plane",
        "Plane",
        "Find, create, and update work items, projects, cycles, and pages in Plane.",
        "Project management",
        "https://mcp.plane.so/http/mcp",
        CatalogAuth.OAUTH,
        docs_url="https://developers.plane.so/dev-tools/mcp-server",
        keywords=("plane",),
    ),
    _e(
        "atlassian",
        "Atlassian",
        "Atlassian",
        "Jira issues and Confluence pages: search, read, create, and update.",
        "Project management",
        "https://mcp.atlassian.com/v1/mcp",
        CatalogAuth.OAUTH,
        keywords=("jira", "confluence", "atlassian"),
    ),
    _e(
        "monday",
        "monday.com",
        "monday.com",
        "Read and manage boards, items, and updates in monday.com.",
        "Project management",
        "https://mcp.monday.com/mcp",
        CatalogAuth.OAUTH,
        icon="monday",
        keywords=("monday.com",),
    ),
    # ── CRM + support ─────────────────────────────────────────────────────────────────────
    _e(
        "intercom",
        "Intercom",
        "Intercom",
        "Search conversations, contacts, and help center content in Intercom.",
        "CRM",
        "https://mcp.intercom.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("intercom",),
    ),
    # ── Payments + commerce ───────────────────────────────────────────────────────────────
    _e(
        "paypal",
        "PayPal",
        "PayPal",
        "Invoices, orders, and payments on your PayPal business account.",
        "Payments",
        "https://mcp.paypal.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("paypal",),
    ),
    _e(
        "square",
        "Square",
        "Block",
        "Catalog, orders, payments, and customers on your Square account.",
        "Payments",
        "https://mcp.squareup.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("squareup",),
    ),
    # ── Design + sites ────────────────────────────────────────────────────────────────────
    _e(
        "canva",
        "Canva",
        "Canva",
        "Search, read, and create designs in your Canva account.",
        "Design",
        "https://mcp.canva.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("canva",),
    ),
    _e(
        "jam",
        "Jam",
        "Jam.dev",
        "Read bug reports with console, network, and repro context from Jam.",
        "Design",
        "https://mcp.jam.dev/mcp",
        CatalogAuth.OAUTH,
        keywords=("jam.dev",),
    ),
    _e(
        "webflow",
        "Webflow",
        "Webflow",
        "Sites, CMS collections, and pages in your Webflow account.",
        "Sites",
        "https://mcp.webflow.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("webflow",),
    ),
    _e(
        "wix",
        "Wix",
        "Wix",
        "Manage your Wix site: content, bookings, store, and more.",
        "Sites",
        "https://mcp.wix.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("wix",),
    ),
    _e(
        "netlify",
        "Netlify",
        "Netlify",
        "Sites, deploys, and environment configuration on Netlify.",
        "Sites",
        "https://netlify-mcp.netlify.app/mcp",
        CatalogAuth.OAUTH,
        keywords=("netlify",),
    ),
    _e(
        "vercel",
        "Vercel",
        "Vercel",
        "Projects, deployments, and logs on your Vercel account.",
        "Sites",
        "https://mcp.vercel.com",
        CatalogAuth.OAUTH,
        keywords=("vercel",),
    ),
    # ── Developer + infrastructure ────────────────────────────────────────────────────────
    _e(
        "sentry",
        "Sentry",
        "Sentry",
        "Query issues, events, and performance data from your Sentry projects.",
        "Developer",
        "https://mcp.sentry.dev/mcp",
        CatalogAuth.OAUTH,
        docs_url="https://docs.sentry.io/product/sentry-mcp/",
        keywords=("sentry", "stack trace"),
    ),
    _e(
        "semgrep",
        "Semgrep",
        "Semgrep",
        "Scan code for security findings with Semgrep rules.",
        "Developer",
        "https://mcp.semgrep.ai/mcp",
        CatalogAuth.OAUTH,
        keywords=("semgrep",),
    ),
    _e(
        "globalping",
        "Globalping",
        "jsDelivr",
        "Run ping, traceroute, and DNS measurements from probes worldwide.",
        "Developer",
        "https://mcp.globalping.dev/mcp",
        CatalogAuth.OAUTH,
        keywords=("globalping", "latency"),
    ),
    _e(
        "neon",
        "Neon",
        "Neon",
        "Manage Neon Postgres projects, branches, and databases.",
        "Databases",
        "https://mcp.neon.tech/mcp",
        CatalogAuth.OAUTH,
        keywords=("neon",),
    ),
    _e(
        "supabase",
        "Supabase",
        "Supabase",
        "Projects, tables, and SQL on your Supabase databases.",
        "Databases",
        "https://mcp.supabase.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("supabase",),
    ),
    _e(
        "cloudflare_docs",
        "Cloudflare Docs",
        "Cloudflare",
        "Search and read Cloudflare's developer documentation.",
        "Developer",
        "https://docs.mcp.cloudflare.com/mcp",
        CatalogAuth.OPEN,
        icon="cloudflare",
        keywords=("cloudflare",),
    ),
    _e(
        "cloudflare_bindings",
        "Cloudflare Workers",
        "Cloudflare",
        "Build with Workers: KV, R2, D1, and bindings on your Cloudflare account.",
        "Developer",
        "https://bindings.mcp.cloudflare.com/mcp",
        CatalogAuth.OAUTH,
        icon="cloudflare",
        keywords=("workers kv",),
    ),
    # ── Knowledge + research ──────────────────────────────────────────────────────────────
    _e(
        "hugging_face",
        "Hugging Face",
        "Hugging Face",
        "Search models, datasets, papers, and Spaces on the Hub.",
        "Knowledge",
        "https://huggingface.co/mcp",
        CatalogAuth.OPEN,
        icon="huggingface",
        keywords=("hugging face", "huggingface"),
    ),
    _e(
        "deepwiki",
        "DeepWiki",
        "Cognition",
        "Ask questions about any public GitHub repository's architecture and docs.",
        "Knowledge",
        "https://mcp.deepwiki.com/mcp",
        CatalogAuth.OPEN,
        docs_url="https://docs.devin.ai/work-with-devin/deepwiki-mcp",
        keywords=("deepwiki",),
    ),
    _e(
        "context7",
        "Context7",
        "Upstash",
        "Version-accurate library documentation and code examples, on demand.",
        "Knowledge",
        "https://mcp.context7.com/mcp",
        CatalogAuth.OPEN,
        docs_url="https://context7.com",
        keywords=("context7",),
    ),
)


# Shelved: kept as data, offered nowhere. `entry_for` does not see them, so no card, gate or bot
# offers one, and a row already made from one keeps its words (`vocabulary`). Two reasons:
# - no client registration: connecting needs the person's own OAuth client, which the web cannot
#   take yet, so the card could not sign anyone in (and a Google row read "connected" while every
#   call failed, because those servers list tools anonymously). Back when the web takes a client.
# - registration unproven: the server registers clients, but not Prismoid's as asked (Stripe takes
#   public clients only, Prismoid asks for a secret) or maybe not at all (Figma may accept only the
#   apps it approved). Back once a real registration succeeds.
SHELVED: Final[tuple[CatalogEntry, ...]] = (
    # ── Google Workspace (official) ───────────────────────────────────────────────────────
    _e(
        "gmail",
        "Gmail",
        "Google",
        "Search threads, read messages, manage labels, and create drafts in your Gmail.",
        "Email",
        "https://gmailmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        docs_url="https://developers.google.com/workspace/gmail/api/reference/mcp",
        keywords=("gmail", "email", "mail", "inbox"),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_calendar",
        "Google Calendar",
        "Google",
        "Read availability and create, update, and cancel events on your calendars.",
        "Calendar",
        "https://calendarmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googlecalendar",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("calendar", "meeting", "schedule"),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_drive",
        "Google Drive",
        "Google",
        "Search and read files across your Google Drive.",
        "Files",
        "https://drivemcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googledrive",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("google drive",),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_docs",
        "Google Docs",
        "Google",
        "Read and create documents in Google Docs.",
        "Documents",
        "https://docsmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googledocs",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("google doc", "google docs"),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_sheets",
        "Google Sheets",
        "Google",
        "Read and update spreadsheets in Google Sheets.",
        "Documents",
        "https://sheetsmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googlesheets",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("google sheet", "google sheets", "spreadsheet"),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_slides",
        "Google Slides",
        "Google",
        "Read and create presentations in Google Slides.",
        "Documents",
        "https://slidesmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googleslides",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("google slides",),
        auth_note=_GOOGLE_NOTE,
    ),
    _e(
        "google_chat",
        "Google Chat",
        "Google",
        "Read spaces and send messages in Google Chat.",
        "Messaging",
        "https://chatmcp.googleapis.com/mcp/v1",
        CatalogAuth.OAUTH,
        icon="googlechat",
        docs_url="https://developers.google.com/workspace/guides/configure-mcp-servers",
        keywords=("google chat",),
        auth_note=_GOOGLE_NOTE,
    ),
    # ── Files, CRM, code, work tracking ───────────────────────────────────────────────────
    _e(
        "box",
        "Box",
        "Box",
        "Search, read, and manage files in your Box account.",
        "Files",
        "https://mcp.box.com",
        CatalogAuth.OAUTH,
        keywords=("box",),
    ),
    _e(
        "hubspot",
        "HubSpot",
        "HubSpot",
        "Read and manage CRM contacts, companies, deals, and tickets.",
        "CRM",
        "https://mcp.hubspot.com/anthropic",
        CatalogAuth.OAUTH,
        keywords=("hubspot",),
    ),
    _e(
        "github",
        "GitHub",
        "GitHub",
        "Repositories, issues, and pull requests through the official GitHub MCP server.",
        "Developer",
        "https://api.githubcopilot.com/mcp/",
        CatalogAuth.OAUTH,
        docs_url="https://github.com/github/github-mcp-server",
        keywords=("github", "pull request", "repository"),
    ),
    _e(
        "asana",
        "Asana",
        "Asana",
        "Search and manage tasks, projects, and goals in Asana.",
        "Project management",
        "https://mcp.asana.com/v2/mcp",
        CatalogAuth.OAUTH,
        keywords=("asana",),
    ),
    # ── Registration unproven ─────────────────────────────────────────────────────────────
    _e(
        "stripe",
        "Stripe",
        "Stripe",
        "Customers, invoices, subscriptions, refunds: read and act on your Stripe account.",
        "Payments",
        "https://mcp.stripe.com",
        CatalogAuth.OAUTH,
        docs_url="https://docs.stripe.com/mcp",
        keywords=("stripe", "invoice", "refund"),
    ),
    _e(
        "figma",
        "Figma",
        "Figma",
        "Read files, components, and comments from your Figma projects.",
        "Design",
        "https://mcp.figma.com/mcp",
        CatalogAuth.OAUTH,
        keywords=("figma",),
    ),
)


def entry_for(slug: str, *, shelved: bool = False) -> CatalogEntry | None:
    """The one lookup, over what is offered (`shelved` adds `SHELVED`, for a row already made from
    one). Case-insensitive on purpose: `connect Notion` should work."""
    want = slug.strip().lower()
    return next((e for e in (*CATALOG, *(SHELVED if shelved else ())) if e.slug == want), None)


# ── the suggestion grammar ────────────────────────────────────────────────────────────────────

_MIN_KEYWORD: Final = 3

# Single tool-name tokens that are ordinary English verbs: as AUTO-ATTACH vocabulary they
# over-trigger wildly ("Create a Notion page..." auto-attached Gmail
# on `create_draft`'s first token). Multi-token PHRASES ("send email", "create draft") stay -
# a phrase is specific; a bare verb is the language itself.
GENERIC_TOOL_TOKENS: Final = frozenset(
    {
        "add",
        "create",
        "delete",
        "find",
        "get",
        "list",
        "make",
        "new",
        "read",
        "remove",
        "run",
        "search",
        "send",
        "set",
        "update",
        "write",
    }
)


def vocabulary(
    *, slug: str, name: str, catalog_slug: str | None, tool_names: tuple[str, ...] | list[str]
) -> set[str]:
    """One connector's suggestion vocabulary - the ONE recipe, over PLAIN values so
    it lives here in the grammar's home (persistence-free): slug + name words + its catalog
    entry's keywords + probed tool-name tokens (bare generic verbs stoplisted - "Create a
    Notion page" must never mount Gmail via create_draft; multi-token phrases
    kept). Auto-attach, the action door, and the wire the composer chip reads all call this,
    so what attaches is exactly what routes is exactly what the chip offers."""
    words: set[str] = {slug.replace("_", " ")}
    words.update(w.lower() for w in name.split() if len(w) >= _MIN_KEYWORD)
    entry = entry_for(catalog_slug, shelved=True) if catalog_slug else None
    if entry is not None:
        words.update(entry.keywords)
    for tool in tool_names:
        tokens = [t for t in tool.lower().split("_") if len(t) >= _MIN_KEYWORD]
        words.update(t for t in tokens if t not in GENERIC_TOOL_TOKENS)
        if len(tokens) > 1:
            words.add(" ".join(tokens))  # "send_email" also matches the phrase "send email"
    return words


def suggest(text: str, candidates: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, str]]:
    """Deterministic intent match: (candidate id, keywords) pairs in, (id, matched keyword)
    pairs out, in candidate order, at most one hit per candidate (its LONGEST matched keyword,
    so `pull request` beats `pull` and the provenance names the real reason).

    Word-boundary only - `linear` never fires on `nonlinear` - and phrases match as phrases.
    Keywords under three characters never match (a two-letter keyword is a false-positive
    machine). Case-insensitive; the text is the user's goal, verbatim.
    """
    lowered = text.lower()
    hits: list[tuple[str, str]] = []
    for cid, keywords in candidates:
        best = ""
        for kw in keywords:
            k = kw.strip().lower()
            if len(k) < _MIN_KEYWORD or len(k) <= len(best):
                continue
            if re.search(rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])", lowered):
                best = k
        if best:
            hits.append((cid, best))
    return hits
