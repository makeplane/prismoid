# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ONE Settings tree.

Every knob in the product is a field here and every URL in the product is a setting - the
`no URL literals` audit exempts exactly this file, `deploy/compose.yaml`, `.env`, and
the provider constructors. Groups carry their own `PRISMOID_<GROUP>_` prefix rather than
leaning on a nested delimiter, because `PRISMOID_API_DATABASE_URL` is unambiguous only when
`api` owns the prefix.

Secrets are `SecretStr` so that a stray `print(settings)` or a captured span cannot leak them
(no plaintext tokens at rest or in logs).

`conf_env.py` holds what every group shares, `conf_machines.py` the Machine and local groups and
`conf_auth.py` the auth group and the hosted boot's dev-credential check, `conf_workspaces.py`
the workspaces group and `conf_observability.py` the observability group;
the tree is composed here, and every caller reads `settings` from here.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from prismoid.conf_auth import AuthSettings, close_signup_outside_dev, refuse_dev_secrets
from prismoid.conf_env import cfg
from prismoid.conf_machines import LocalSettings, MachineSettings
from prismoid.conf_observability import ObservabilitySettings
from prismoid.conf_push import PushSettings
from prismoid.conf_workspaces import WorkspacesSettings


def _csv(v: object) -> object:
    """`a,b` → `["a", "b"]`. .env is a flat file; lists arrive comma-separated, not as JSON."""
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


def _flags(v: object) -> object:
    """`prismoid:fleet=off,prismoid:x=on` → `{"prismoid:fleet": False, ...}`."""
    if isinstance(v, str):
        out: dict[str, bool] = {}
        for item in v.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, state = item.partition("=")
            out[name.strip()] = state.strip().lower() in {"on", "true", "1", "yes"}
        return out
    return v


def _int_map(v: object) -> object:
    """`anthropic=12,default=4` → `{"anthropic": 12, "default": 4}`. Same flat-file grammar as
    `_flags`, integer-valued - the provider concurrency limits arrive this way."""
    if isinstance(v, str):
        out: dict[str, int] = {}
        for item in v.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, count = item.partition("=")
            out[name.strip()] = int(count.strip() or 0)
        return out
    return v


def _lower(v: object) -> object:
    """Case must never be the difference between behavior and no behavior in an env var."""
    return v.lower() if isinstance(v, str) else v


CsvList = Annotated[list[str], BeforeValidator(_csv)]
FlagMap = Annotated[dict[str, bool], BeforeValidator(_flags)]
IntMap = Annotated[dict[str, int], BeforeValidator(_int_map)]

# The flags this build ships with, and their values. One line per flag; this IS the list.
SHIPPED_FLAGS: dict[str, bool] = {
    "prismoid:parallel-fanout": True,  # the frontier runs its branches at once
    "prismoid:fleet": False,  # ships off: every seat is a real model run
}

# A screen key's id: it rides every ticket's claims and sandbar's SANDBAR_DISPLAY_KEYS.
_SCREEN_KID = re.compile(r"[A-Za-z0-9._-]{1,32}")


class ApiSettings(BaseSettings):
    """The HTTP face: its pool, its public identity, its CORS allowlist."""

    model_config = cfg("PRISMOID_API_")

    database_url: str = "postgresql+asyncpg://prismoid:prismoid@localhost:5433/prismoid"
    public_base_url: str = "http://localhost:8100"  # every link and stream URL is built from this
    # Where the WEB APP lives: the OAuth callback 302s the owner's browser to
    # `{web_base_url}/connectors?connected={slug}&ok=…`; empty (CLI-only) = a static HTML answer.
    web_base_url: str = ""
    # The signup page's and user menu's legal and support links (ADR-086); empty = not shown.
    terms_url: str = Field(default="", validation_alias="PRISMOID_WEB_TERMS_URL")
    privacy_url: str = Field(default="", validation_alias="PRISMOID_WEB_PRIVACY_URL")
    support_email: str = Field(default="", validation_alias="PRISMOID_SUPPORT_EMAIL")
    cors_origins: CsvList = Field(default_factory=lambda: ["http://localhost:5173"])
    host: str = "0.0.0.0"  # bound inside the container network, never the open internet
    port: int = 8100

    # Pool shape, tuned with numbers: at 20 + 10 connections the 50-user load test queued ~20 requests
    # at peak (capture p95 98 ms against a 50 ms budget); 32 + 32 serves all 50 with headroom. Not
    # unbounded: Postgres has its own ceiling, and the worker has its own DBOS pool, sized separately.
    db_pool_size: int = 32
    db_max_overflow: int = 32
    db_pool_pre_ping: bool = True
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # A coarse per-actor global backstop: one actor cannot monopolise the fleet. Deliberately
    # generous - 200 req/s sustained is a runaway loop or abuse, not a person, and it sits above the
    # 50-user load test (which shares one token, so ~145 req/s as a single actor). Finer per-endpoint
    # limits are a later refinement; this is the "one client cannot take the system down" floor.
    per_actor_rate_limit_per_minute: int = 12000

    # How many API replicas share the traffic. The in-process throttles divide their limits by this,
    # so the FLEET-wide ceiling stays the configured number behind an evenly-distributing load
    # balancer; precision limiting belongs at the ingress once one exists.
    replica_count: int = 1

    # The largest body a route that carries files reads (bytes; any other: 1 MiB, api/middleware).
    # Sized for a task with ten 5 MiB attachments as base64 (about 67 MiB). Over it: 413 unparsed.
    max_body_bytes: int = 80 * 1024 * 1024
    # The longest authored text a request field takes (characters): a SKILL.md, a bot's
    # instructions, a cases.yaml. Far above what the authoring checks accept (5,000 tokens),
    # so it never refuses honest work; it only keeps a single field from taking the whole body.
    max_text_chars: int = 200_000

    # Reverse-proxy honesty: uvicorn trusts X-Forwarded-For only from
    # 127.0.0.1 by default, so behind nginx/an LB every request wears the PROXY's address and
    # every per-IP rate limit collapses into one shared bucket - an unauthenticated caller can
    # then 429 the whole fleet's password-reset door. Set this to the proxy's address(es), or
    # "*" when the proxy is the only route in, to make `request.client.host` the real client.
    forwarded_allow_ips: str = ""

    # How often a live stream re-proves its credential + task authorization.
    # A stream authenticates once at connect and can then outlive its credential - a password
    # reset flips rows, not sockets - so the generator re-checks on this cadence and severs on
    # the first failed proof. Two indexed reads per stream per interval: noise at the
    # 50-user target, and the bound on how long a revoked attacker keeps receiving.
    stream_reauth_seconds: float = 10.0

    # The screen door's Ed25519 key: "<kid>:<base64 of the 32-byte seed>". It signs the
    # browser's `st_` screen tickets and sandbar's `dt_` display tickets; sandbar holds only the
    # public half, by kid (SANDBAR_DISPLAY_KEYS). Empty in dev: `operations/tickets` mints a key
    # per process (dev runs one API process). Staging and production refuse to boot without it
    # while computer use is on (`Settings._screen_key_outside_dev`).
    screen_signing_key: SecretStr = SecretStr("")
    # Where the API reaches sandbar's display route for a sandbar Machine's screen (e.g.
    # http://sandbard:7070). Empty falls back to
    # `PRISMOID_WORKER_SANDBAR_URL`. The API never holds sandbar's master token: a display ticket
    # signed with `screen_signing_key` is its only credential there.
    sandbar_url: str = ""

    def screen_key(self) -> tuple[str, bytes] | None:
        """`screen_signing_key` parsed: (kid, seed), or None when it is unset. A malformed value
        is a ValueError that names the setting and never echoes it - which is why the boot check
        lives on the root `Settings`: a field validator's error prints the field's raw input."""
        raw = self.screen_signing_key.get_secret_value().strip()
        if not raw:
            return None
        kid, sep, encoded = raw.partition(":")
        try:
            seed = base64.b64decode(encoded, validate=True)
        except ValueError:  # binascii.Error is a ValueError; so is a non-ASCII str
            seed = b""
        if not sep or not _SCREEN_KID.fullmatch(kid) or len(seed) != 32:
            raise ValueError(
                "PRISMOID_API_SCREEN_SIGNING_KEY is not <kid>:<base64 of a 32-byte Ed25519 seed> "
                "(a kid is 1-32 of A-Z a-z 0-9 . _ -)"
            )
        return kid, seed


class WorkerSettings(BaseSettings):
    """The machine half: DBOS's database, object storage, frames, provider keys."""

    model_config = cfg("PRISMOID_WORKER_")

    # DBOS speaks sync libpq, not asyncpg - hence a second URL for the same database.
    database_url: str = "postgresql://prismoid:prismoid@localhost:5433/prismoid"

    # Unique per instance before running more than one worker: DBOS identifies
    # applications instances by executor id in distributed environments, and recovery adopts
    # pending workflows by that id. Empty = DBOS's single-instance default.
    executor_id: str = ""

    # **The application version DBOS gates recovery on.** DBOS's default hashes the
    # code, so EVERY restart on changed code orphans all in-flight workflows: never adopted,
    # never cancelled, their queued rows counting against GLOBAL queue concurrency forever -
    # eight such ghosts froze the entire steps queue. Pinning the version
    # ("dev") makes restarts ADOPT in-flight work, which is safe HERE by construction: workflow
    # bodies re-execute from the DB state machine (attempt counters, step states), not from
    # code-shape-sensitive journals. Production deploys SET this per release (the git sha) and
    # rely on the boot ghost sweep for the cross-version leftovers. Empty = DBOS's hash (the
    # old behaviour, kept reachable).
    app_version: str = "dev"

    # **Boot-time version-ghost sweep, gated.** `_sweep_version_ghosts` clears
    # ENQUEUED/DELAYED/PENDING workflows from OTHER application versions (deletes the never-started,
    # so the sweeper can re-enqueue their ids; cancels the started) on the assumption that no such work
    # can legitimately be in flight - TRUE for a single worker (dev), FALSE during a prod ROLLING
    # DEPLOY, where the previous version's worker is a LIVE PEER still draining its queue. There
    # is no peer-liveness check, so leave this True only for single-worker deployments; rolling
    # deploys MUST set PRISMOID_WORKER_REAP_VERSION_GHOSTS=false and drain the old version before
    # retiring its workers (deploy/DEPLOY.md).
    reap_version_ghosts: bool = True

    # **A step's total wall clock.** CLASS_TIMEOUT_S bounds one model REQUEST; this bounds the run,
    # so a wedged tool loop cannot hold a queue slot forever. Generous: a 110-page assembly is
    # minutes. Timeout = one infra attempt (capped by `max_infra_attempts`), never a dead task.
    agent_wall_clock_s: int = 1800

    # **A guarded run's request ceiling.** pydantic-ai's default 50 is spent honestly by a long
    # loop; the guard's budget and stall brakes are the real stops, this is the last one.
    request_ceiling: int = 200

    # Infra attempts beside the model's three (a failed provider, a dead sandbox, a wall-clock stop),
    # and a provider death's durable backoff, doubling and short (the gateway honoured Retry-After).
    max_infra_attempts: int = 3
    provider_backoff_s: float = 5.0
    provider_backoff_max_s: float = 60.0

    s3_endpoint: str = "http://localhost:9000"  # where the worker *uploads*
    s3_access_key: str = "prismoid"
    s3_secret_key: SecretStr = SecretStr("prismoid1234")  # compose's MINIO_ROOT_PASSWORD (MinIO wants >= 8)
    artifact_bucket: str = "artifacts"
    snapshot_bucket: str = "snapshots"
    skill_bucket: str = "skills"

    egress_proxy_url: str = "http://egress-proxy:3128"  # the frame's ONLY route out
    # The proxy's control plane, as seen from the WORKER (which runs on the host in dev and
    # in-network in prod). Two URLs for one service, for the same reason MinIO has two.
    egress_control_url: str = "http://localhost:3129"
    signing_key_path: str = ".keys/verifier.ed25519"
    # 12: the long-form lane's section waves run at full queue width - 12 × 2 GB
    # frames = 24 GB, sized for a ≥32 GB worker host. Smaller hosts set 8; ≥48 GB may set 16.
    # The DB math holds to ~16 (held connection per running step against the 32+32 pool).
    step_concurrency: int = 12

    # **The dequeue cadence.** DBOS queue workers sleep-THEN-poll: at the 1.0 s default three
    # polls (task queue, step queue, the result join) cost every task ~1.5 s p50. 50 ms costs ~20
    # dequeue queries/s/queue idle; raise it where that query rate matters more than latency.
    queue_poll_s: float = 0.05

    # The Lead's deciding wall (its express step and its replan seat): its web tools stop here, so a
    # turn that needs pages read is a plan, not a race. A reply the Lead is writing runs on to a reply's
    # wall (`chat_timeout_s`); cut there, the person reads what it had and that it ran out of time.
    express_timeout_s: int = 45
    # A conversation reply's wall clock and its lane's LEASE: a reply past the
    # wall is stopped and a SYSTEM row says so; a claim older than the lease is a dead worker's
    # and the next job takes the lane. The lease must exceed the wall - a live reply must never
    # be re-claimed under itself - so `chat_lease_s` is read as max(lease, wall + 30).
    chat_timeout_s: int = 150
    chat_lease_s: int = 180
    # The scatter lane. Width and rounds are the two non-money bounds on the
    # wide-and-deep loop; the third is every seat honestly reporting `exhausted`, and the
    # backstop is the Bursar at every frontier. `swarm_min_count` is the least `target_count`
    # a scatter recipe may carry - fewer items were always a single researcher's job.
    swarm_max_width: int = 8
    swarm_max_rounds: int = 2
    swarm_min_count: int = 8
    # The Bursar's per-seat projection price: LIMIT is checked against SPEND + this × the
    # coming wave at every boundary (the long-form lane's wave law, one lane over).
    # Conservative against the live runs' ~$0.03–0.06/seat - retries climb the ladder.
    swarm_seat_est_usd: float = 0.25

    # The catalog/routing read cache (the deferred "catalog cache").
    # `execute_step` re-read the catalog (3 queries) + routing table (1) on EVERY attempt for
    # data that changes on `prismoid models add`. A tiny TTL bounds staleness to the same
    # order as a hot-reload; admin mutations clear it in-process immediately. 0 disables.
    catalog_cache_s: float = 3.0

    # The shell tool's exec wall clock (was hardcoded 120 in the driver): a 110-page assembly
    # build - python-docx over 22 mounted sections plus embedded figures - is minutes of real
    # work, not a hang.
    shell_timeout_s: int = 300

    # The web door - `web_fetch`'s dials. One fetch's wall clock; the byte cap (over it: "attach the
    # file as an input instead"); the redirect walk's depth (every hop re-vetted and re-pinned); the
    # per-ATTEMPT call budget (a retry rung is a fresh `build()`, so a fresh toolset); the slice a long
    # page returns in (a cut slice says what remains); the conversion sidecar's wall clock; the context
    # anchor (fetch only URLs the task's context mentions, against exfiltration; off only for self-hosters
    # who accept free-range fetching); the dev-only loopback door; the User-Agent every fetch announces.
    fetch_timeout_s: float = 30.0
    fetch_max_bytes: int = 20_000_000
    fetch_max_redirects: int = 5
    fetch_max_per_step: int = 12
    fetch_page_chars: int = 24_000
    fetch_convert_timeout_s: float = 60.0
    fetch_context_anchor: bool = True
    fetch_allow_insecure_localhost: bool = False
    fetch_user_agent: str = "Prismoid/1.0 (+https://plane.so; agent fetch, single user-directed requests)"

    # The long-form cost checkpoint: the confirmation gate
    # opens only when the outline's honest HIGH estimate exceeds this; small runs flow.
    longform_confirm_usd: float = 2.0

    # Frames. `docker` is the in-process default; `sandbar` runs each frame in a container on
    # the sandbar daemon's Docker backend (repo root `sandbar/`), sharing the host kernel; its
    # Firecracker backend is experimental. The driver is a config choice, never a code fork.
    frame_driver: str = "docker"  # docker | sandbar | device
    # Where objects live: `s3` (MinIO/S3, hosted) or `fs` (files under the local data home,
    # served to the browser by the signed blob door). Local mode requires `fs`.
    storage_backend: Literal["s3", "fs"] = "s3"
    # The dev instance this worker belongs to (one per worktree: PRISMOID_WORKER_INSTANCE=i<n>). Every
    # frame wears it (`plane.instance`), and the reconciler judges only its own instance's frames,
    # so two worktrees' workers never destroy each other's Machines. Empty = the default instance.
    instance: str = ""
    sandbar_url: str = "http://localhost:7070"
    sandbar_token: SecretStr = SecretStr("")
    sandbar_backend: str = "docker"  # sandbar's backend for OUR frames; firecracker is experimental
    sandbar_image: str = "prismoid-frame"  # sandbar rootfs template / docker image name
    # The browser profile's own sandbar rootfs/image - deliberately unset by default (empty):
    # `just sandbar-browser-image` builds it and the self-host stack sets it. A frame silently
    # falling back to `sandbar_image` would boot a browser profile into a sandbox with no
    # Chromium in it, so `SandbarFrame.create()` refuses a browser profile without it.
    sandbar_browser_image: str = ""

    # Forwarded to the browser sidecar as `SIDECAR_*` - it is persistence-free and never reads `conf`.
    browser_snapshot_chars: int = 12_000  # the accessibility-tree snapshot cap, in chars
    browser_headed_ua: bool = False  # passes sites' headless filters: the operator's call (.env.example)

    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")


class EmailSettings(BaseSettings):
    """Outbound product email - reset codes and security notices, nothing else yet.

    `console` (the dev default) prints the rendered message to the API process stdout - locally
    there is no relay, and the reset code has to land somewhere a developer can actually read.
    `smtp` is stdlib smtplib over STARTTLS for anything shared. Switching is one env var."""

    model_config = cfg("PRISMOID_EMAIL_")

    # Literal, not str: a typo'd or wrong-case backend ("SMTP", "sendgrid") must die at boot,
    # not boot cleanly and silently drop every reset email while the API keeps answering 202.
    backend: Annotated[Literal["console", "smtp"], BeforeValidator(_lower)] = "console"
    from_address: str = "Prismoid <no-reply@plane.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 10.0


class GatewaySettings(BaseSettings):
    """Tiers 2-3: providers whose keys are *not* first-party. Model strings never live here."""

    model_config = cfg("PRISMOID_GATEWAY_")

    deepseek_api_key: SecretStr = SecretStr("")
    xai_api_key: SecretStr = SecretStr("")
    openai_compat_base_url: str = ""

    # Per-provider concurrent-call ceilings, PER PROCESS (ai/gateway/throttle.py). "default"
    # covers any provider not named. Sized for the width-12 long-form wave: the two lanes that
    # carry sections (anthropic) and images (openai/xai) get real width; local Ollama gets 2
    # because a laptop model under eight streams answers none of them (the fleet learned this
    # live). Divided by `replica_count` so the fleet-wide ceiling survives horizontal workers -
    # the same convention as the API throttles above.
    provider_limits: IntMap = Field(
        default_factory=lambda: {
            "default": 4,
            "anthropic": 12,
            "openai": 12,
            "google": 8,
            "xai": 8,
            "local": 2,
        }
    )
    replica_count: int = 1


class SigningSettings(BaseSettings):
    """Ed25519 over the canonical verification report.

    **The key must be the same in every process, and it must survive a restart.** The *worker*
    signs (verification runs there) and the *API* verifies - so a key generated per-process would
    make every signature fail to check in the one place anybody reads it. And a key regenerated on
    restart would invalidate every report ever signed, which is the opposite of what a signature is
    for.

    So: set `PRISMOID_SIGNING_PRIVATE_KEY` in production. Left empty, the signer generates one
    **once**, writes it to `key_path` (0600, gitignored), and every process on the machine reads the
    same file - zero-config in dev, explicit in production, and never a predictable key, which would
    be worse than no signature at all.
    """

    model_config = cfg("PRISMOID_SIGNING_")

    private_key: SecretStr = SecretStr("")  # base64 of the 32-byte Ed25519 seed
    key_path: str = ".signing-key"


def _pairs(text: str) -> dict[str, str]:
    """The flat name=value grammar (the throttle precedent): "brave=abc,exa=xyz" → a dict.
    Values may contain '=' (base64-ish keys survive); names are case-folded; empty entries
    are skipped rather than refused - config noise is not a boot failure."""
    out: dict[str, str] = {}
    for entry in text.split(","):
        name, _, value = entry.strip().partition("=")
        if name and value:
            out[name.strip().lower()] = value.strip()
    return out


class SearchSettings(BaseSettings):
    """The web-search seam: engines as CONFIG, never code - the Bing-retirement
    lesson, held all the way down. `engines` is the ordered chain; everything an engine needs
    arrives through FOUR flat `name=value` grammars (the throttle precedent), so **enabling
    any registered engine - Exa, Perplexity, Parallel, a provider-native one - is pure env**:
    no per-engine settings field to add, ever. The registry of what each engine needs lives
    with its wire (`ai/tools/providers/search.py: WIRES`); an entry missing its requirement is
    skipped at build time; an empty chain means the `web_search` tool does not exist - absent
    beats broken. Prices are per-1k-queries, written to the ledger at spend
    time (`price_version=0` = conf-priced). URLs here are OPERATOR infrastructure - like
    `s3_endpoint`, they may be private/localhost; the egress guard governs model-authored
    URLs, not configured endpoints."""

    model_config = cfg("PRISMOID_SEARCH_")

    engines: str = ""  # ordered CSV chain, e.g. "brave,anthropic,searxng"; empty = no tool
    keys: SecretStr = SecretStr("")  # "brave=BSA…,exa=…" - one masked string, every engine key
    urls: str = ""  # "searxng=http://localhost:8888,…" - self-host bases + endpoint overrides
    prices_per_1k: str = ""  # "brave=5,exa=7" - overrides the wire's default; searxng is 0
    models: str = ""  # "anthropic=claude-haiku-4-5,…" - provider-native engines only

    timeout_s: float = 15.0
    results: int = 8  # default result count; the tool clamps requests to 1..20
    max_per_step: int = 10

    def chain(self) -> list[str]:
        return [e.strip().lower() for e in self.engines.split(",") if e.strip()]

    def key_for(self, engine: str) -> str:
        return _pairs(self.keys.get_secret_value()).get(engine, "")

    def url_for(self, engine: str) -> str:
        return _pairs(self.urls).get(engine, "")

    def price_for(self, engine: str, default: float) -> float:
        raw = _pairs(self.prices_per_1k).get(engine, "")
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    def model_for(self, engine: str) -> str:
        return _pairs(self.models).get(engine, "")


class RoutinesSettings(BaseSettings):
    """Standing work. `enabled` is the deployment-wide kill switch - the sweeps check it every
    tick and the fire door refuses, so flipping it stops dispatch IMMEDIATELY (the Claude
    workspace-toggle semantics; Copilot's schedules-keep-running-after-disable hole is the named
    anti-pattern). Money defaults are the owner's confirmed numbers: unattended work is always
    capped - a routine's per-run budget is REQUIRED and defaults here, never unset."""

    model_config = cfg("PRISMOID_ROUTINES_")

    enabled: bool = True
    default_budget_usd: float = 2.0  # per run; rides create_task's budget and the Bursar
    min_interval_s: int = 300  # the throttle floor between fires, all sources (launchd's rule)
    daily_fire_cap: int = 24  # per routine; one-shots exempt (the Claude rule)
    pause_after_failures: int = 3  # consecutive settled-failed runs → auto-pause (Make's number)
    max_active_per_user: int = 50  # the Spark cap; a 409 at create, never a silent drop
    max_chain_depth: int = 3  # routine→task→event→routine chains (the Exchange hop budget)
    miss_grace_s: int = 300  # a fire older than this is a MISS - the catch-up policy applies
    queue_buffer_max_s: int = 86400  # overlap=queue holds its ONE buffered fire at most this long
    event_lag_s: int = 120  # W4: the watermark trails now() by this (commit-visibility skew)
    jitter_max_s: int = 120  # W2: deterministic per-routine offset (FixedRandomDelay semantics)
    fire_payload_max_bytes: int = 65536  # W3: the inbound door's cap (the GH number); 413 over


class SteeringSettings(BaseSettings):
    """Safe-point steering. A mid-step pause parks its step on a gate, and a park holds
    a DBOS queue slot until the DART port: `max_pause_parks` caps how many PAUSE gates may be open
    across the deployment at once. At the cap a paused step finishes instead and the frontier holds
    the pause (`pause.applied{boundary}`), so pauses never starve the queue."""

    model_config = cfg("PRISMOID_STEERING_")

    max_pause_parks: int = Field(default=4, ge=0)


class ConnectorsSettings(BaseSettings):
    """Connector secrets at rest: OAuth tokens, client registrations, flow state.

    **The key must be the same in every process, and it must survive a restart.** The *API*
    encrypts (the OAuth callback lands there) and the *worker* decrypts (tools mount there) -
    so a key generated per-process would make every mount fail in the one place the credential
    is actually used. And a key regenerated on restart would orphan every connector ever
    authorized, which is the opposite of what "connected" means.

    So: set `PRISMOID_CONNECTORS_ENCRYPTION_KEY` in production - a Fernet key (urlsafe-base64,
    32 bytes); a comma-separated list rotates (the first encrypts, all of them decrypt). Left
    empty, the cipher generates one **once**, writes it to `key_path` (0600, gitignored), and
    every process on the machine reads the same file - zero-config in dev, explicit in
    production. The SigningSettings law, applied to a cipher.
    """

    model_config = cfg("PRISMOID_CONNECTORS_")

    encryption_key: SecretStr = SecretStr("")  # Fernet key(s), comma-separated, newest first
    key_path: str = ".connectors-key"
    key_id: str = "v1"  # stamped beside every ciphertext; bump when rotating
    # Prod-safe default: an http:// MCP server (plaintext, incl. loopback) is refused. Dev sets
    # PRISMOID_CONNECTORS_ALLOW_INSECURE_LOCALHOST=true via env to reach a local MCP server.
    allow_insecure_localhost: bool = False
    probe_timeout_s: float = 10.0  # the initialize + list_tools budget at create/callback/mount


class ComputerUseSettings(BaseSettings):
    """Computer use - the browser's own dials. `enabled=False` is the kill switch: no step drives
    the browser, and the Lead plans every site ask without it."""

    model_config = cfg("PRISMOID_COMPUTER_USE_")

    enabled: bool = False
    command_timeout_s: float = 30.0
    # Full pages kept when a non-Anthropic seat's client-side collapse fires (an Anthropic
    # seat's history is cleared server-side and never edited - `CONTEXT_EDITS`).
    snapshot_window: int = 2

    # The screen. Frame rates per socket: a thumbnail, an open pane, the
    # lease holder's own pane - the sidecar acks no faster than 30 fps, so none may ask for more.
    screen_fps_thumb: int = Field(default=1, ge=1, le=30)
    screen_fps_pane: int = Field(default=5, ge=1, le=30)
    screen_fps_holder: int = Field(default=30, ge=1, le=30)
    screen_quality: int = Field(default=60, ge=1, le=100)  # the screencast's JPEG quality, at most
    # ...and at most this while the lease holder's connection watches: smaller frames, more of them
    screen_holder_quality: int = Field(default=45, ge=1, le=100)
    screen_viewer_bps: int = Field(default=300_000, gt=0)  # each viewer's byte budget, 1 s deep
    screen_holder_bps: int = Field(default=3_000_000, gt=0)  # the lease holder's own connection's
    screen_linger_s: int = Field(default=5, ge=0)  # an unwatched upstream closes after this
    screen_write_timeout_s: int = Field(default=5, gt=0)  # a write slower than this closes 4408
    screen_input_per_s: int = Field(default=120, gt=0)  # input messages per socket per second

    # The control lease and the CONTROL gate.
    control_silence_s: int = Field(default=60, gt=0)  # a non-private lease ends after this with no input
    control_private_cap_s: int = Field(default=600, gt=0)  # a private lease's hard cap
    control_grace_s: int = Field(default=30, gt=0)  # after the holder's socket closes
    control_renew_every_s: int = Field(default=5, gt=0)  # the relay renews at most this often
    control_wait_s: int = Field(default=1200, gt=0)  # a CONTROL gate's TTL (< the park hold, below)
    mask_settle_ms: int = Field(default=300, ge=0)  # input dropped after a private claim
    # A ticket's lifetime. The protocol's ceiling is 30 s - sandbar refuses a longer display
    # ticket - so the dial only shortens it.
    ticket_ttl_s: int = Field(default=30, ge=1, le=30)
    # sandbar's side of the screen is configured: sandbard holds
    # SANDBAR_DISPLAY_KEYS (the public half of PRISMOID_API_SCREEN_SIGNING_KEY) and the API reaches
    # it (PRISMOID_API_SANDBAR_URL). Off, a sandbar Machine has no screen - the 409 "no live screen
    # yet" - never a screen that keeps "starting" and a lease nobody can see. The API and the
    # worker both read it (`machines.screen.has_screen`), so set it for both.
    sandbar_screen: bool = False

    @model_validator(mode="after")
    def _renewal_inside_the_silence(self) -> ComputerUseSettings:
        if self.control_renew_every_s >= self.control_silence_s:
            raise ValueError(
                f"control_renew_every_s ({self.control_renew_every_s}) must be under control_silence_s "
                f"({self.control_silence_s}): a driver's input could not keep their lease alive"
            )
        return self


class ChannelsSettings(BaseSettings):
    """Bot channels - the reach doors. `enabled=False` is the kill switch with
    the routines semantics: the doors refuse and the drain sweep stands down this tick.
    `kinds` is a CSV allowlist (enabling a kind is pure env, like the search engines);
    the webhook kind ships on by default because it needs no operator infrastructure."""

    model_config = SettingsConfigDict(env_prefix="PRISMOID_CHANNELS_", extra="ignore")

    enabled: bool = True
    kinds: str = "webhook"  # CSV of ChannelKind values an operator has stood up
    email_domain: str = ""  # e.g. bots.plane.so - empty ⇒ the email kind refuses to mint
    email_inbound_secret: SecretStr = SecretStr("")  # the generic inbound webhook's header secret
    telegram_api_base: str = "https://api.telegram.org"  # operator infra, like the storage endpoint
    default_budget_usd_per_turn: float = 1.0
    daily_message_cap: int = 100
    inbound_max_bytes: int = 65536  # the fire door's number - one law for machine bodies


class CliSettings(BaseSettings):
    """The CLI is a thin API client - it never touches the database."""

    model_config = cfg("PRISMOID_CLI_")

    api_url: str = "http://localhost:8100"
    api_key: SecretStr = SecretStr("")  # PRISMOID_CLI_API_KEY, or `prismoid login` writes a session


class Settings(BaseSettings):
    """The one tree. `from prismoid.conf import settings` is the only way to read config."""

    model_config = cfg("PRISMOID_")

    # Both spellings, deliberately: deploy/compose.prod.yaml and `just dev` say PRISMOID_ENV,
    # the env registry documented PRISMOID_ENVIRONMENT - and the prod-safety gates below hang
    # off this field, so the spelling mismatch quietly left every deploy reading "dev" (the
    # shipped prod compose set a variable nothing read).
    environment: Literal["dev", "test", "staging", "prod", "local"] = Field(
        default="dev", validation_alias=AliasChoices("PRISMOID_ENV", "PRISMOID_ENVIRONMENT")
    )

    # Where a *browser* fetches artifacts from - deliberately a second endpoint: the
    # worker uploads over the internal network, the user downloads over the public one.
    s3_public_endpoint: str = "http://localhost:9000"

    question_ttl_hours: int = 24

    # **What we ship with, not what somebody remembered to insert.** A flag that is off because
    # nobody created a row is a feature that is off in production and on in every developer's head.
    #
    # `PRISMOID_FEATURE_DEFAULTS` **merges over** this - it does not replace it, and the difference
    # is the whole comment. Pydantic's default behaviour for a dict field is *replacement*: the
    # single line `PRISMOID_FEATURE_DEFAULTS=prismoid:fleet=off` in `.env` silently turned
    # off every flag it did not mention, including the parallel fan-out that had just been written,
    # tested, and shipped **on**. The live run then ran serially and looked, from the outside,
    # exactly like a fan-out that did not work.
    #
    # An env var that names one flag means "change this one". It has never once meant "and forget
    # everything else I ship with". A Project row overrides both.
    feature_defaults: FlagMap = Field(default_factory=dict)

    # The docker frames' egress control secret: the worker and the proxy read the environment
    # themselves (`ai/frames/egress`); a field here so the boot can refuse an empty or dev value.
    egress_control_secret: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _merge_shipped_flags(self) -> Settings:
        self.feature_defaults = {**SHIPPED_FLAGS, **self.feature_defaults}
        return self

    @model_validator(mode="after")
    def _console_mail_never_leaves_dev(self) -> Settings:
        """Anything shared MUST set smtp (ADR-031), made executable. The console email backend
        prints password-reset codes - full account-takeover credentials - to stdout: that IS the
        delivery mechanism on a laptop, and a credential leak into whoever reads the log pipeline
        anywhere shared. A staging/prod boot with console mail refuses to start rather than
        printing users' reset codes into `docker logs` while they receive nothing."""
        if self.environment in ("staging", "prod") and self.email.backend == "console":
            raise ValueError(
                f"PRISMOID_EMAIL_BACKEND=console is refused with PRISMOID_ENV={self.environment}: "
                "the console backend prints password-reset codes to stdout. Set "
                "PRISMOID_EMAIL_BACKEND=smtp and the PRISMOID_EMAIL_SMTP_* settings (ADR-031)."
            )
        return self

    @model_validator(mode="after")
    def _signup_closed_outside_dev(self) -> Settings:
        close_signup_outside_dev(self)  # a hosted box is invite-only unless its operator opens it
        return self

    @model_validator(mode="after")
    def _fs_storage_never_leaves_local(self) -> Settings:
        """The blob door (ADR-077) is the one unauthenticated file route, valid in local mode only, and
        the filesystem backend is what it serves. Staging and prod refuse `fs` at boot rather than
        keep a shared deployment's objects on one disk behind an HMAC link (the mailer's rule)."""
        if self.environment in ("staging", "prod") and self.worker.storage_backend == "fs":
            raise ValueError(
                f"PRISMOID_WORKER_STORAGE_BACKEND=fs is refused with PRISMOID_ENV={self.environment}: "
                "the filesystem store and its blob door are for local mode only (ADR-077). "
                "Set PRISMOID_WORKER_STORAGE_BACKEND=s3."
            )
        return self

    @model_validator(mode="after")
    def _local_needs_device_and_fs(self) -> Settings:
        """`PRISMOID_ENV=local` is Prismoid on one Mac: the Mac is the Machine and the disk is the
        object store. Any other driver or backend would reach for Docker or MinIO that are not there,
        so the boot refuses rather than fail on the first task. Local behaves like dev for the
        staging/prod refusals above (console mail): none of them name it."""
        if self.environment != "local":
            return self
        if self.worker.frame_driver != "device" or self.worker.storage_backend != "fs":
            raise ValueError(
                "PRISMOID_ENV=local requires PRISMOID_WORKER_FRAME_DRIVER=device and "
                "PRISMOID_WORKER_STORAGE_BACKEND=fs (ADR-078)."
            )
        return self

    @model_validator(mode="after")
    def _control_wait_under_park_hold(self) -> Settings:
        """A CONTROL gate nobody answers times out before the park hold could sleep its Machine:
        the step resumes on a warm browser instead of a rebuilt one."""
        if self.computer_use.control_wait_s >= self.machines.park_hold_s:
            raise ValueError(
                f"PRISMOID_COMPUTER_USE_CONTROL_WAIT_S ({self.computer_use.control_wait_s}) must be under "
                f"PRISMOID_MACHINES_PARK_HOLD_S ({self.machines.park_hold_s}): a CONTROL gate must time "
                "out before the park hold sleeps the Machine its step is parked on (ADR-065)."
            )
        return self

    @model_validator(mode="after")
    def _screen_key_outside_dev(self) -> Settings:
        """The screen key, the mailer's precedent: a malformed key fails the
        boot everywhere, and staging or production refuse to boot without one while computer use
        is on - a per-process key there would make every other replica, and sandbar, refuse the
        tickets this one mints."""
        if (
            self.api.screen_key() is None
            and self.computer_use.enabled
            and self.environment in ("staging", "prod")
        ):
            raise ValueError(
                f"PRISMOID_API_SCREEN_SIGNING_KEY is required with PRISMOID_ENV={self.environment} "
                "while PRISMOID_COMPUTER_USE_ENABLED=true: the API signs screen and display tickets "
                "with it, and sandbar trusts its public half (SANDBAR_DISPLAY_KEYS). Set it to "
                "<kid>:<base64 of 32 random bytes> (ADR-065)."
            )
        return self

    @model_validator(mode="after")
    def _no_dev_secrets_outside_dev(self) -> Settings:
        """Last of the boot checks, so a more specific refusal above speaks first."""
        refuse_dev_secrets(self)
        return self

    # resolved from backend/ - the backend always runs from there (ADR-007)
    perf_baseline_path: str = "tests/perf/baseline.json"

    api: ApiSettings = Field(default_factory=ApiSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    signing: SigningSettings = Field(default_factory=SigningSettings)
    connectors: ConnectorsSettings = Field(default_factory=ConnectorsSettings)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    computer_use: ComputerUseSettings = Field(default_factory=ComputerUseSettings)
    machines: MachineSettings = Field(default_factory=MachineSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    routines: RoutinesSettings = Field(default_factory=RoutinesSettings)
    steering: SteeringSettings = Field(default_factory=SteeringSettings)
    workspaces: WorkspacesSettings = Field(default_factory=WorkspacesSettings)
    local: LocalSettings = Field(default_factory=LocalSettings)
    push: PushSettings = Field(default_factory=PushSettings)
    cli: CliSettings = Field(default_factory=CliSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
