# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors - create, probe, mount, and hand the engine its specs (ADR-020).

This module owns every transaction and every network leg of the connector story except the OAuth
2.1 legs (discovery, registration, consent, code exchange, refresh), which live in
`operations/connector_oauth.py`. The deterministic pieces (PKCE, discovery URLs, request
assembly, the SSRF door) live in `schemas/connectors.py` as pure functions; the MCP protocol
itself lives in `ai/tools/connectors.py` (`probe_server`); the mount's pure decisions live in
`operations/connector_judge.py`; what remains here is choreography and rows - which is exactly
what `operations/` is for.

Two disciplines run through the whole file:

- **Commit before HTTP.** A session must sit committed and idle across any network call (the
  idle-in-transaction law `execution._attempt` is built on). Every function here reads, commits,
  dials, then writes in a fresh short transaction.
- **Tokens first, inventory second.** The OAuth grant is the thing that must never be lost:
  the callback commits ciphertext before it probes, so a flaky probe costs a status, never a
  re-consent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import logfire
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools import catalog
from prismoid.ai.tools.call_rows import JournalTool
from prismoid.ai.tools.connectors import ProbeAuthRequired, ProbeFailed, probe_server
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, ValidationFailed
from prismoid.operations import cipher, connector_judge
from prismoid.operations import thread as thread_ops
from prismoid.operations.connector_oauth import (
    CLIENT_NAME,
    AppSignIn,
    CallbackOutcome,
    begin_authorization,
    callback_url,
    complete_flow,
    finish_app_flow,
    knock_broken,
    refresh_if_stale,
)
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.connector_catalog import entry_for
from prismoid.schemas.connectors import (
    ATTACHABLE,
    AuthKind,
    ConnectorSpec,
    ConnectorStatus,
    ProbeResult,
    ServiceReach,
    ToolPolicy,
    check_server_url,
    resource_metadata_from_www_auth,
    sanitize_server_text,
    slugify,
)
from prismoid.schemas.egress import domain_allowed
from prismoid.schemas.ids import ConnectorId, hash_token
from prismoid.schemas.side_effects import SideEffect

MAX_TASK_CONNECTORS = 8  # the same lip as skills - a task is not a bus bar


# ── create ────────────────────────────────────────────────────────────────────────────────────


def _derive_name(server_url: str) -> str:
    host = urlsplit(server_url).hostname or "connector"
    parts = [p for p in host.split(".") if p not in ("mcp", "www", "api")]
    return parts[0] if parts else "connector"


async def create_connector(
    session: AsyncSession,
    actor: Actor,
    *,
    server_url: str,
    name: str | None = None,
    bearer_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    catalog_slug: str | None = None,
) -> Connector:
    """Validate at the door, commit the row, then probe best-effort. The probe decides the
    opening status: an anonymous server goes straight to `live` with its tools cached; a 401
    parks at `pending_auth` holding the discovery hint; a pasted bearer is tried on the spot.
    `catalog_slug` is provenance: which catalog entry minted this row - it stamps
    the column and seeds the entry's recommended policy overrides, owner-editable after."""
    try:
        url = check_server_url(
            server_url, allow_insecure_localhost=settings.connectors.allow_insecure_localhost
        )
    except ValueError as bad:
        raise ValidationFailed(str(bad), fields={"server_url": str(bad)}) from None

    display = (name or _derive_name(url)).strip()[:80]
    slug = slugify(display)
    if await Connector.resolve_refs(session, actor.id, [slug]):
        raise Conflict(f"a connector named {slug!r} already exists - pick a different --name")

    entry = entry_for(catalog_slug) if catalog_slug else None
    row = Connector(
        owner_actor_id=actor.id,
        name=display,
        slug=slug,
        server_url=url,
        status=ConnectorStatus.PENDING_AUTH,
        auth_kind=AuthKind.NONE,
        key_id=cipher.key_id(),
        catalog_slug=entry.slug if entry else None,
        tool_policies={k: v.value for k, v in entry.recommended_policies.items()} if entry else {},
    )
    if client_id:
        # Pre-registered client (a DCR-less server): the registration we never have to perform.
        info = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[AnyUrl(callback_url())],
            token_endpoint_auth_method="client_secret_basic" if client_secret else "none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=CLIENT_NAME,
        )
        row.client_info_enc = cipher.encrypt(info.model_dump_json(exclude_none=True))
        row.auth_kind = AuthKind.OAUTH
    if bearer_token:
        row.tokens_enc = cipher.encrypt(json.dumps({"access_token": bearer_token, "token_type": "Bearer"}))
        row.auth_kind = AuthKind.BEARER

    session.add(row)
    await session.commit()
    # ── the boundary. The row is durable; the probe below is best-effort and writes its own tx. ──
    await probe_and_cache(session, row)
    logfire.info(
        "connector {slug} created for {actor}: {status}",
        slug=slug,
        actor=str(actor.id),
        status=row.status,
    )
    return row


async def connect(
    session: AsyncSession,
    actor: Actor,
    *,
    catalog_slug: str | None = None,
    server_url: str | None = None,
    name: str | None = None,
    bearer_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    binding: str,
    app: AppSignIn | None = None,
) -> tuple[Connector, str | None]:
    """One-click connect: create the row AND begin authorization in one round trip.

    Exactly one of `catalog_slug` / `server_url`. Returns `(row, authorize_url | None)`: an
    open server comes back live with no URL; an OAuth server comes back `authorizing` with the
    consent URL the caller's browser must visit. A begin failure (no DCR and no client id, a
    dead discovery) is NOT an exception here - the row exists and is worth keeping, so the
    reason lands on `last_error` and the caller renders it. One button, honest outcomes.
    """
    if (catalog_slug is None) == (server_url is None):
        raise ValidationFailed(
            "pass exactly one of catalog_slug or server_url",
            fields={"catalog_slug": "exactly one of catalog_slug/server_url"},
        )
    if catalog_slug is not None:
        entry = entry_for(catalog_slug)
        if entry is None:
            raise ValidationFailed(
                "unknown catalog entry", fields={"catalog_slug": f"no catalog entry {catalog_slug!r}"}
            )
        server_url = entry.server_url
        name = name or entry.name

    row = await create_connector(
        session,
        actor,
        server_url=str(server_url),
        name=name,
        bearer_token=bearer_token,
        client_id=client_id,
        client_secret=client_secret,
        catalog_slug=catalog_slug,
    )
    if row.status != ConnectorStatus.PENDING_AUTH or row.auth_kind == AuthKind.BEARER:
        return row, None  # live already, bearer, or an error the row wears - nothing to authorize
    try:
        return row, await begin_authorization(session, actor, row.id, binding=binding, app=app)
    except Conflict as why:
        # The row survives with the reason on it (e.g. "no client registration - pass
        # --client-id"): a card showing the fix beats a 409 hiding the connector it made.
        row.last_error = sanitize_server_text(str(why), 500)
        await session.commit()
        logfire.warn("connector {slug} one-click authorize failed: {why}", slug=row.slug, why=str(why))
        return row, None


# ── probe ─────────────────────────────────────────────────────────────────────────────────────


def _authorization_for(row: Connector) -> str | None:
    if not row.tokens_enc:
        return None
    tokens = json.loads(cipher.decrypt(row.tokens_enc))
    kind = str(tokens.get("token_type") or "Bearer")
    kind = "Bearer" if kind.lower() == "bearer" else kind
    return f"{kind} {tokens['access_token']}"


async def probe_and_cache(session: AsyncSession, row: Connector) -> None:
    """One initialize + list_tools against the row's server, and the honest status that follows.
    Called post-commit - always its own short write transaction, never inside somebody's read."""
    try:
        result = await probe_server(
            row.server_url,
            authorization=_authorization_for(row),
            timeout_s=settings.connectors.probe_timeout_s,
        )
    except ProbeAuthRequired as need:
        had_tokens = row.tokens_enc is not None
        previous = row.status
        row.status = ConnectorStatus.NEEDS_REAUTH if had_tokens else ConnectorStatus.PENDING_AUTH
        if row.auth_kind == AuthKind.NONE:
            row.auth_kind = AuthKind.OAUTH
        hint = resource_metadata_from_www_auth(need.www_authenticate)
        if hint:
            row.flow_enc = cipher.encrypt(json.dumps({"resource_metadata": hint}))
        row.last_error = "the server requires authorization"
        if had_tokens:
            await knock_broken(session, row, previous, "The server stopped accepting its authorization.")
        await session.commit()
        return
    except ProbeFailed as bad:
        row.status = ConnectorStatus.ERROR
        row.last_error = sanitize_server_text(str(bad), 500)
        await session.commit()
        return

    row.tools = [t.model_dump() for t in result.tools]
    row.instructions = result.instructions or None
    row.server_info = dict(result.server_info)
    # A DISABLED row keeps its OFF status through a probe: the owner switched it off
    # deliberately, and a successful probe must refresh the cached inventory without silently
    # re-enabling it. Only `enable_connector` promotes a disabled row back to live.
    if row.status != ConnectorStatus.DISABLED:
        row.status = ConnectorStatus.LIVE
        row.last_error = None
    await session.commit()


# ── the OAuth callback ────────────────────────────────────────────────────────────────────────


async def handle_callback(
    session: AsyncSession,
    *,
    state: str,
    binding: str,
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> CallbackOutcome:
    """The browser comes back. The state token finds the flow: hashed lookup, single-use, TTL - a
    replay or a stranger matches zero rows and learns nothing. The browser binding proves it is
    the browser that started the flow: a shared consent link opened anywhere else spends the state
    and exchanges nothing, so a victim's code never lands on someone else's connector."""
    row = await Connector.consume_state(session, hash_token(state))
    if row is not None:
        tenant.bind(session, row.workspace_id)  # the cn_ state resolved the connector: act for its workspace
    if row is None:
        # Commit, not rollback: the zero-row claim changed nothing, and a rollback would expire
        # every object in the caller's session - punishment for a stranger's stale link.
        await session.commit()
        return CallbackOutcome(
            False, "This authorization link was already used, or it expired. Start again from Plane."
        )

    outcome = await complete_flow(
        session, row, binding=binding, code=code, error=error, error_description=error_description
    )
    if outcome is not None:
        return outcome
    # ── tokens are durable. The inventory probe below is best-effort. ──
    await probe_and_cache(session, row)
    if row.status == ConnectorStatus.LIVE:
        return CallbackOutcome(True, "Connected. You can close this tab.", row.name, row.slug)
    return CallbackOutcome(
        True, f"Authorized - but the first probe said: {row.last_error}", row.name, row.slug
    )


async def finish_app_sign_in(
    session: AsyncSession, actor: Actor, connector_id: uuid.UUID, *, return_code: str, code_verifier: str
) -> Connector:
    """The phone app finishes the sign-in its callback handed back (`connector_oauth._hand_to_app`):
    the owner's own row, the return code and the app's secret - then the same probe the web's
    callback runs. A refused finish is not an exception: the row wears the reason, as connect's do."""
    row = await Connector.get_for(session, connector_id, actor)
    outcome = await finish_app_flow(session, row, return_code=return_code, code_verifier=code_verifier)
    if outcome is None:
        await probe_and_cache(session, row)
    return row


# ── the step seam ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ResolvedConnectors:
    """What a step mounts, and what it must say about the ones it could not."""

    specs: tuple[ConnectorSpec, ...] = ()
    notes: list[str] = field(default_factory=list)  # engine_fact lines, mounted and dropped alike
    reach: tuple[ServiceReach, ...] = ()  # what each service does beyond what mounted, by name


async def resolve_for_task(
    session: AsyncSession,
    task: Task,
    *,
    journal: JournalTool | None = None,
    bots: Sequence[ResolvedBot] = (),
    reads_only: bool = False,
) -> ResolvedConnectors:
    """Rows to refreshed, probed, decrypted specs, owner-scoped (`for_ids` refuses foreign rows); a connector
    that cannot mount is dropped with a journaled failure. `reads_only` keeps only unasked read tools."""
    ids = [uuid.UUID(s) for s in (task.connector_ids or [])]
    if not ids:
        return ResolvedConnectors()

    rows = await Connector.for_ids(session, ids, owner_id=task.creator_actor_id)
    found = {r.id for r in rows}
    borrowed = await borrowed_turn(session, task) or connector_judge.foreign_bot(bots, task.creator_actor_id)
    await session.commit()  # reads done - refresh/probe below dial out on an idle session

    out = ResolvedConnectors()

    for missing in (i for i in ids if i not in found):
        await _drop(out, journal, str(missing)[:8], "it no longer exists")

    for row in rows:
        auto_ask = str(row.id) in (task.connectors_auto or [])
        await _mount_row(
            session, row, out, journal, auto_ask=auto_ask, borrowed=borrowed, reads_only=reads_only
        )
    return out


async def drop_all(
    session: AsyncSession, task: Task, bot_slugs: Sequence[str], *, journal: JournalTool | None, reason: str
) -> ResolvedConnectors:
    """Nothing mounts: each connector the step would have tried (the task's attaches, then the bots'
    declared slugs not among them) is a journaled drop naming `reason`. Reads only; never dials out."""
    ids = [uuid.UUID(s) for s in (task.connector_ids or [])]
    rows = await Connector.for_ids(session, ids, owner_id=task.creator_actor_id) if ids else []
    attached = [row.slug for row in rows]
    out = ResolvedConnectors()
    for label in [*attached, *(slug for slug in dict.fromkeys(bot_slugs) if slug not in attached)]:
        await _drop(out, journal, label, reason)
    return out


async def borrowed_turn(session: AsyncSession, task: Task) -> str | None:
    """Why this turn borrows the CREATOR's credential, or None when the task is hers alone.

    "Connecting was the consent" is the creator's consent for her own work - not for Bob's
    follow-up reading her inbox (a teammate AUTHORS the turn), and not
    for Bob's steer or answer folded into HER live turn either (if Bob
    is steering Ada's prompt and it is using Ada's connector, approval comes only to Ada).
    On a borrowed turn every mounted tool asks, and the approve gate (creator-only in
    `gates.resolve`, its knock creator-only in `open_approval`) makes her consent per call.

    Per TASK, not per turn: once a teammate has taken a
    turn, steered or answered anywhere in the task, every later turn - her own included - stays
    borrowed, because his words stay in the thread those turns read (her
    next turn does not start clean). The same holds through forks: a fork's thread carries its
    ancestors' conversation up to each fork point, so a teammate's word there borrows the fork
    from its first turn (`thread.teammate_word`, the thread's one question). A NULL author is an
    older row's first turn: the creator's own. Skipped when the turn's author already decides."""
    if task.turn_actor_id is not None and task.turn_actor_id != task.creator_actor_id:
        return "this turn is a teammate's"
    match await thread_ops.teammate_word(session, task):
        case thread_ops.FORKED:
            return "a teammate worked on the task this was forked from"
        case "task.continued":
            return "a teammate has taken a turn on this task"
        case "plan.edited":
            return "a teammate has steered this task"
        case "gate.resolved":
            return "a teammate has answered in this task"
        case _:
            return None


async def _drop(out: ResolvedConnectors, journal: JournalTool | None, label: str, reason: str) -> None:
    out.notes.append(f"Dropped connector {label} - {reason}")
    if journal is not None:
        await journal(f"connector.{label}", ok=False, denied_reason=reason)


async def _mount_row(
    session: AsyncSession,
    row: Connector,
    out: ResolvedConnectors,
    journal: JournalTool | None,
    *,
    auto_ask: bool,
    borrowed: str | None = None,
    reads_only: bool = False,
) -> None:
    """One row through the whole mount walk: status, refresh, probe, the policy cut, the spec.
    Factored so the task-level resolver and the bot-manifest resolver can never
    drift on what mounting means. `auto_ask` is the no-one-clicked-Connect posture:
    non-read-only tools without an explicit owner override gate on approval. `borrowed` is why a
    teammate's turn - or any turn of a task a teammate has put their word into - runs on the
    creator's row (`borrowed_turn`): every tool that mounts asks, reads and owner overrides
    included - only `off` still wins, since an off tool never mounts."""
    result = await _probe_for_mount(session, row, out, journal)
    if result is None:
        return

    row.tools = [t.model_dump() for t in result.tools]
    row.instructions = result.instructions or None
    row.server_info = dict(result.server_info)
    await session.commit()

    policy = connector_judge.mount_policy(
        row.tool_policies, result.tools, auto_ask=auto_ask, borrowed=borrowed
    )
    on = tuple(t.name for t in result.tools if policy[t.name] is not ToolPolicy.OFF)
    mounted = on
    if reads_only:
        reads = catalog.connector_side_effects(row.slug, result.tools)
        mounted = tuple(
            n
            for n in on
            if policy[n] is ToolPolicy.ALLOW and reads[catalog.connector_tool(row.slug, n)] is SideEffect.READ
        )
    off = tuple(t.name for t in result.tools if policy[t.name] is ToolPolicy.OFF)
    beyond = tuple(n for n in on if n not in mounted)
    if beyond or off:
        out.reach += (ServiceReach(name=row.name, steps_can=beyond, turned_off=off),)
    if reads_only and not mounted:
        return  # nothing here reads unasked: a step calls the rest
    if not mounted:
        await _drop(out, journal, row.slug, "every tool is off by policy - nothing to mount")
        return
    off_count = len(result.tools) - len(mounted)

    auth = _authorization_for(row)
    out.specs += (
        ConnectorSpec(
            id=ConnectorId(row.id),
            slug=row.slug,
            name=row.name,
            url=row.server_url,
            tool_names=mounted,
            instructions=result.instructions,
            authorization=SecretStr(auth) if auth else None,
            tool_policies={n: policy[n] for n in mounted},
        ),
    )
    out.notes.append(connector_judge.mount_note(row.slug, len(mounted), off_count, borrowed))


async def _probe_for_mount(
    session: AsyncSession, row: Connector, out: ResolvedConnectors, journal: JournalTool | None
) -> ProbeResult | None:
    """The row's live inventory, or None after a journaled drop: disabled, not authorized, the
    authorization refused (a needs_reauth knock, committed), or the server silent (the error
    noted, committed, the row still `live`). Refreshes stale tokens first."""
    if row.status == ConnectorStatus.DISABLED:
        await _drop(out, journal, row.slug, "it is disabled")
        return None
    await refresh_if_stale(session, row)
    if row.status not in ATTACHABLE:
        await _drop(
            out, journal, row.slug, f"needs authorization - run: prismoid connectors authorize {row.slug}"
        )
        return None
    try:
        return await probe_server(
            row.server_url,
            authorization=_authorization_for(row),
            timeout_s=settings.connectors.probe_timeout_s,
        )
    except ProbeAuthRequired:
        previous = row.status
        row.status = ConnectorStatus.NEEDS_REAUTH
        row.last_error = "the server refused our authorization"
        await knock_broken(session, row, previous, "The server stopped accepting its authorization.")
        await session.commit()
        await _drop(
            out, journal, row.slug, f"authorization expired - run: prismoid connectors authorize {row.slug}"
        )
        return None
    except ProbeFailed as bad:
        row.last_error = sanitize_server_text(str(bad), 500)
        await session.commit()  # still `live` - a wobble is not a revocation
        await _drop(out, journal, row.slug, "the server did not answer - continuing without it")
        return None


async def resolve_for_bot(
    session: AsyncSession,
    task: Task,
    slugs: Sequence[str],
    *,
    already: Sequence[str] = (),
    journal: JournalTool | None = None,
    bots: Sequence[ResolvedBot] = (),
) -> ResolvedConnectors:
    """A bot manifest's declared connector slugs → the TASK CREATOR's own mounted specs
    Credentials never travel with a bot: each
    runner mounts their own rows, and a slug the creator has not connected is a journaled
    drop that names the fix - it costs the capability, never the task. Step-scoped by the
    caller: nothing here writes `task.connector_ids`. Every mount rides `auto_ask=True` -
    nobody clicked Connect for this task, so non-read-only tools without an explicit owner
    override gate on approval (the per-tool consent rule, applied to manifests), every tool on a
    bot the creator did not write (`connector_judge.foreign_bot`)."""
    wanted = [s for s in dict.fromkeys(slugs) if s not in set(already)]
    if not wanted:
        return ResolvedConnectors()

    resolved = await Connector.resolve_refs(session, task.creator_actor_id, list(wanted))
    borrowed = await borrowed_turn(session, task) or connector_judge.foreign_bot(bots, task.creator_actor_id)
    await session.commit()  # reads done - refresh/probe below dial out on an idle session

    out = ResolvedConnectors()
    for slug in wanted:
        row = resolved.get(slug)
        if row is None:
            await _drop(
                out,
                journal,
                slug,
                "the bot wants it and you have not connected it - connect it on /connectors",
            )
            continue
        await _mount_row(session, row, out, journal, auto_ask=True, borrowed=borrowed)
    return out


# ── the doors ─────────────────────────────────────────────────────────────────────────────────


async def validate_refs(session: AsyncSession, actor: Actor, refs: list[str]) -> list[str]:
    """The task door (mirrors the skill resolve line): refs in, resolved row ids out, 422 with
    per-ref reasons for anything that cannot honestly be attached."""
    if len(refs) > MAX_TASK_CONNECTORS:
        raise ValidationFailed("too many connectors", fields={"connectors": f"at most {MAX_TASK_CONNECTORS}"})
    resolved = await Connector.resolve_refs(session, actor.id, refs)
    bad: dict[str, str] = {}
    for ref in refs:
        row = resolved.get(ref)
        if row is None:
            bad[ref] = "no connector by this id or slug"
        elif row.status not in ATTACHABLE:
            bad[ref] = f"not connected ({row.status}) - run: prismoid connectors authorize {row.slug}"
    if bad:
        raise ValidationFailed("these connectors cannot be attached", fields=bad)
    seen: list[str] = []
    for ref in refs:
        rid = str(resolved[ref].id)
        if rid not in seen:
            seen.append(rid)
    return seen


async def filter_live(session: AsyncSession, actor: Actor, ids: list[str]) -> list[str]:
    """The fork door: inherit what still mounts, silently drop what died - a dead connector
    must not orphan a branch (the disabled-seat rule, tasks.py's model_refs precedent).
    Scoped to the FORKING actor: a co-member's branch keeps its own connectors and sheds the
    parent creator's, because inheriting a credential across users is the delegation connectors
    defer - and a dropped connector costs a capability, never the branch."""
    rows = await Connector.for_ids(session, [uuid.UUID(s) for s in ids], owner_id=actor.id)
    live = {str(r.id) for r in rows if r.status in ATTACHABLE}
    return [s for s in ids if s in live]


# ── the humans' verbs ─────────────────────────────────────────────────────────────────────────


async def expire_stale_flows(session: AsyncSession, rows: Sequence[Connector]) -> None:
    """An abandoned consent tab must not park a row in `authorizing` forever. Past the flow
    TTL the status reverts honestly - `needs_reauth` if tokens ever existed, `pending_auth`
    otherwise - so the web's poll settles and Connect comes back. Lazy, at read time (the
    gate-expiry trick): no sweeper, the next look tells the truth."""
    dirty = False
    for row in rows:
        stale = row.status == ConnectorStatus.AUTHORIZING and (
            row.flow_expires_at is None or row.flow_expires_at <= utcnow()
        )
        if not stale:
            continue
        row.status = ConnectorStatus.NEEDS_REAUTH if row.tokens_enc else ConnectorStatus.PENDING_AUTH
        row.last_error = "authorization was not completed"
        row.oauth_state_hash = None  # the dead flow's claim ticket, retired with it
        dirty = True
    if dirty:
        await session.commit()


async def list_connectors(
    session: AsyncSession, actor: Actor, *, limit: int = 50, cursor: str | None = None
) -> list[Connector]:
    rows = await Connector.for_owner(session, actor.id, limit=limit, cursor=cursor)
    await expire_stale_flows(session, rows)
    return rows


async def get_connector(session: AsyncSession, actor: Actor, connector_id: uuid.UUID) -> Connector:
    row = await Connector.get_for(session, connector_id, actor)
    await expire_stale_flows(session, [row])
    return row


async def update_connector(
    session: AsyncSession,
    actor: Actor,
    connector_id: uuid.UUID,
    *,
    name: str | None = None,
    auto_attach: bool | None = None,
) -> Connector:
    """Display name and/or the intent-attach dial - the slug (tool prefix) is fixed, because
    prompts and records already wear it."""
    row = await Connector.get_for(session, connector_id, actor)
    if name is not None:
        row.name = name.strip()[:80]
    if auto_attach is not None:
        row.auto_attach = auto_attach
    await session.commit()
    return row


async def set_tool_policies(
    session: AsyncSession, actor: Actor, connector_id: uuid.UUID, *, policies: dict[str, str]
) -> Connector:
    """REPLACE the overrides map: the permissions screen holds the whole picture, so
    what it sends is what the row stores. Values must be real policies; names must exist in
    the cached inventory - a typo'd tool gets a 422 naming it, never a silent dead key."""
    row = await Connector.get_for(session, connector_id, actor)
    known = set(row.tool_names())
    bad: dict[str, str] = {}
    for tool, value in policies.items():
        if value not in (p.value for p in ToolPolicy):
            bad[tool] = f"not a policy: {value!r} (allow | ask | off)"
        elif tool not in known:
            bad[tool] = "no such tool on this connector - probe first?"
    if bad:
        raise ValidationFailed("these policies cannot be set", fields=bad)
    row.tool_policies = dict(policies)
    await session.commit()
    logfire.info("connector {slug} policies set ({n} overrides)", slug=row.slug, n=len(policies))
    return row


async def disable_connector(session: AsyncSession, actor: Actor, connector_id: uuid.UUID) -> Connector:
    """Switch it off without forgetting it - the status every surface already renders and
    nothing could write until now. Also retires any in-flight consent claim, the same hygiene
    `expire_stale_flows` performs: a disabled row must not hold a live claim ticket."""
    row = await Connector.get_for(session, connector_id, actor)
    row.status = ConnectorStatus.DISABLED
    row.oauth_state_hash = None
    row.flow_expires_at = None
    row.last_error = None
    await session.commit()
    return row


async def enable_connector(session: AsyncSession, actor: Actor, connector_id: uuid.UUID) -> Connector:
    """Back on: with a credential (or none needed) the probe decides live/needs_reauth/error;
    an OAuth row without tokens goes back to `pending_auth` awaiting consent."""
    row = await Connector.get_for(session, connector_id, actor)
    if row.status != ConnectorStatus.DISABLED:
        return row
    if row.tokens_enc is None and row.auth_kind == AuthKind.OAUTH:
        row.status = ConnectorStatus.PENDING_AUTH
        await session.commit()
        return row
    row.status = ConnectorStatus.PENDING_AUTH
    await session.commit()
    await probe_and_cache(session, row)
    return row


async def delete_connector(session: AsyncSession, actor: Actor, connector_id: uuid.UUID) -> None:
    """Hard delete: the tokens die with the row (RFC 7009 revocation is deferred).
    Tasks that referenced it keep their ids and drop it honestly at the next mount."""
    row = await Connector.get_for(session, connector_id, actor)
    await session.delete(row)
    await session.commit()
    logfire.info("connector {slug} deleted by {actor}", slug=row.slug, actor=str(actor.id))


async def reprobe_connector(session: AsyncSession, actor: Actor, connector_id: uuid.UUID) -> Connector:
    """Re-run the probe on demand - the recovery path for `error` without a re-consent."""
    row = await Connector.get_for(session, connector_id, actor)
    await session.commit()
    await probe_and_cache(session, row)
    return row


async def resolve_for_view(session: AsyncSession, task: Task) -> list[Connector]:
    """The read-only load for briefs and serializers: rows, no refresh, no probe, no network.
    Creator-scoped like the mount - a brief must not name another user's connectors."""
    ids = [uuid.UUID(s) for s in (task.connector_ids or [])]
    return await Connector.for_ids(session, ids, owner_id=task.creator_actor_id)


async def connector_hosts(session: AsyncSession, task: Task) -> set[str]:
    """The hosts of the task's attached connectors and the owner's LIVE ones - the operator's
    own services, reached through their tools, never the bare browser."""
    attached = await resolve_for_view(session, task)
    owner_rows = await Connector.for_owner(session, task.creator_actor_id, limit=200)
    return {
        urlsplit(r.server_url).hostname or ""
        for r in [*attached, *[r for r in owner_rows if r.status == ConnectorStatus.LIVE.value]]
    }


def covered_by(covered: set[str], host: str) -> bool:
    """True when `host` is a connector's full host or under it; never a sibling under a shared suffix."""
    return domain_allowed(host, covered - {""})
