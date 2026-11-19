# ADR-012 — Deployment posture: context, storage, and frames, sovereignty-first

*Status: **proposed**, revised 2026-07-30 to be **provider-neutral** (the original draft carried
AWS-specific vocabulary — CloudFront/OAC, S3 Express — from AWS-heavy research; this revision
removes it). Companion to ADR-009 (scale posture), ADR-011 (memory retrieval), ADR-013 (sandbar
frames). Sources: three research sweeps over frontier products' published engineering — URLs
inline. Landed since the draft: C0 prompt-cache discipline, C0 batched-NOTIFY hub, and sandbar
(the microVM substrate that replaces "buy E2B" below).*

## Context

The platform must run at the class of ChatGPT / Claude / Perplexity — fast, reliable, scalable —
**and** it must be **sovereign**: deployable on any cloud, a bare Linux machine, a Railway-style
PaaS, or a private/on-prem cloud, with no dependency on a service the operator cannot self-host.
Those two goals are not in tension, because the frontier stack the research surfaced is itself
built from commodity parts.

**The headline finding: our shape is already the convergent one, and it is already portable.**
Nothing durable lives on disk — Postgres is the record (tasks, append-only events, memories,
frozen context packs), artifacts and inputs live in content-addressed **S3-protocol** object
storage (MinIO in dev = boto3 = any S3-compatible store in prod, by config), frames are ephemeral
materializations destroyed after use, and DBOS-on-Postgres owns durable execution. Point for
point, that is the published pattern: Anthropic runs ephemeral destroyed-after-session sandboxes
([containment post](https://www.anthropic.com/engineering/how-we-contain-claude)); OpenAI serves
millions of QPS from **one unsharded Postgres primary + read replicas**
([PGConf.dev 2025](https://pigsty.io/blog/db/openai-pg/)); sha256 content-addressing is how OCI
registries and git scale object storage; two-lane streaming with `Last-Event-ID` replay is the
2026 consensus for resumable AI streams. The move is targeted upgrades, not a redesign.

## The portability contract — four boxes and one rule

The whole system reaches the outside world through exactly **four ports**, and each is a
commodity with many interchangeable implementations:

| Port | What it is | Bare machine | PaaS | Sovereign / on-prem | Hyperscale |
|---|---|---|---|---|---|
| **SQL database** | the brain — all state, the event log, *and* the job queue (DBOS is on Postgres, no separate queue) | local Postgres | managed Postgres | your Postgres / Patroni | managed Postgres + read replicas |
| **Object store (S3 API)** | the filing cabinet — artifacts & inputs, keyed by content hash | MinIO | R2 / provider bucket | MinIO / Ceph RadosGW | S3 / GCS |
| **Sandbox host (Linux)** | the workshop — sandboxes for agent code | Docker or sandbar | their container runtime | your Linux fleet + sandbar | same |
| **Model endpoints** | the muscle — LLM inference over HTTP | vendor key **or** local Ollama/vLLM | ditto | self-hosted vLLM on your GPUs | vendor or self-hosted |

**The rule that makes it sovereign: the app reaches all four only through a URL and a credential
in config** (one env-driven `Settings` tree — §2, §14). Swap the URL, you have swapped the
provider; no code moves. This is already true and exercised — MinIO→S3 and Docker→sandbar are both
config changes, not forks. **Nothing else is required.** CDN, Redis, a dedicated broker, managed
anything — every one is an *optional accelerator*, added only when a specific meter turns red, and
each has a self-hostable substitute (below). We never adopt a proprietary service we cannot run
ourselves.

## The decisions

### 1 · Artifacts & inputs — presigned URLs from the object store; a CDN is optional

- **Any S3-compatible store is the source of truth** — MinIO, Ceph/RadosGW, R2, S3, GCS. Our
  sha256 keys are perfectly entropy-distributed, so a store's per-prefix request limits (S3's
  ~3,500 PUT / ~5,500 GET per prefix, for instance) effectively never bind — the key scheme *is*
  the scaling strategy, on any store.
- **Uploads**: presigned PUT with the S3 API's `x-amz-checksum-sha256` header — the store verifies
  the client's claimed hash server-side (MinIO, Ceph and S3 all honour it), so the
  content-addressed invariant holds at the door.
- **Downloads are presigned URLs from the store's public endpoint. Full stop.** The API hands the
  browser a time-limited signed URL and the browser fetches the object directly from the store.
  Immutable, content-addressed objects get `Cache-Control: public, max-age=31536000, immutable`,
  so *any* cache in front of them is safe — but a cache is not needed for correctness or for this
  product's access pattern (below).
- **A CDN is an optional accelerator, provider-neutral.** A CDN earns its keep on *popular,
  widely-shared, geographically-dispersed* content. Our artifacts are the opposite — **personal
  work products** (a memo, a spreadsheet) downloaded a handful of times by the few people on that
  task. That pattern rarely wants a CDN at all. If download egress or latency ever demands one,
  the substitutes are all self-hostable or portable: a **self-hosted nginx/Varnish cache** in
  front of the store, **R2** (zero egress fee) as the store itself, or the deploying cloud's own
  CDN. None of these is a design element; each is a knob.
- **Small-file outputs**: bundle many-small-file harvests into one tar/zip blob with manifest
  entries pointing inside (the GitLab Pages pattern) — object-per-tiny-file is the known
  cost/latency trap, on every store.

### 2 · The injected-context hot path — the model's prompt cache is the lever

Frontier memory at request time is a **cheap read of a precomputed artifact injected into a
cache-friendly prompt**, not live retrieval (ChatGPT injects a precomputed dossier wholesale;
consolidation runs offline). Our design already matches — `assemble()` runs once at planning,
freezes the pack, the archivist consolidates after delivery. **Landed (C0):** cache discipline —
prompt order is a cache layout (frozen system + tools prefix → stable per-task context → volatile
turn content), with cache breakpoints on the two provider paths and staggered fleet fan-out.

- Vendor caches (Anthropic exact-prefix, 0.1× reads; OpenAI/Gemini automatic prefix discount) are
  inherent to using a vendor model and cost nothing sovereignty-wise — they are the vendor's, over
  the wire. **The sovereign path keeps the same win**: a self-hosted **vLLM** with automatic
  prefix caching gives the identical TTFT collapse at the KV-cache layer, because the same
  byte-stable-prefix discipline drives it.
- DB side: the pack read is one indexed Postgres row via a pooler (PgBouncer/PgCat — both
  open-source). A read-through cache (Valkey — open-source) belongs here **only on measured p95
  evidence**; a ≤200-row FTS scan is not the bottleneck.

### 3 · Memory retrieval — Postgres-first stands; the escape hatch is self-hostable

ADR-011's ladder holds: pgvector's comfortable envelope (~1–10M vectors/instance, 50M+ with
pgvectorscale) is thousands of times above our capped per-scope corpora; hybrid FTS+vector fused
with RRF in one SQL query is the proven next rung — **all inside the same Postgres**, no new port.
The at-scale escape hatch, if per-user corpora ever outgrow Postgres, is a vector store that keeps
the sovereignty contract: **Turbopuffer** (S3-resident, self-hostable-adjacent) or a self-hosted
**Qdrant/LanceDB** behind the same `Memory.search` seam — call sites unchanged. External SaaS-only
memory services (mem0/Zep/Letta) stay rejected: the record must live in *our* Postgres and object
store, inside our event stream and role model, or the provenance story is fiction.

### 4 · Event log + live fan-out — Postgres record, batched wake-ups, open-source bus at scale

- **Record**: `task_events` stays in Postgres — range-partitioned by time (pg_partman), BRIN on
  `at`, retention by partition drop, keyset pagination. OpenAI's own doctrine: keep Postgres,
  control the writes. All open-source, runs anywhere.
- **Wake-ups (landed, C0)**: the batched-NOTIFY hub — payloads buffered and flushed in one
  transaction per ~20 ms, so Postgres's commit-time NOTIFY lock is taken once per flush, not per
  frame (lifts the documented ~2.9K/s convoy ceiling to ~60K/s). No new infra; pure Postgres.
- **At fan-out scale** (browsers past what one process holds): an **open-source pub/sub bus** —
  **Valkey** (BSD, the Redis fork) or **NATS** (Apache-2) — channel-per-task, SSE gateways at
  ~10–50K connections each, with `since`/`Last-Event-ID` replay **always served from Postgres**
  (our Lane-1-only `id:` rule already guarantees the resume contract). Both are self-hostable;
  Kafka is the wrong shape for per-task streams. ADR-009's trigger stands, with a sovereign
  successor named.

### 5 · Frames — sandbar is the substrate (we built it), managed providers are optional adapters

*(This decision superseded the original "buy E2B" — sovereignty made building the right call, and
ADR-013 delivered it.)*

- **The isolation gap is closed by our own code.** Docker frames share the host kernel;
  **sandbar** (ADR-013) runs each frame in a **Firecracker microVM** — own kernel per sandbox —
  self-hosted, no Plane imports, behind the existing `FrameDriver` seam. It runs on any Linux/KVM
  host: a bare machine, your own fleet, a sovereign cloud. The isolation the frontier universally
  requires, without renting it.
- **Managed sandbox providers (E2B, Modal) remain *optional adapters*** behind the same
  `FrameDriver` protocol, for an operator who would rather buy than run — but they are never
  required, and the default is ours.
- **Hydration is the real cold start** (image pull ≈ 76% of container start): bake one base image
  per toolchain as a sandbar rootfs/template; hydrate inputs by **parallel GETs from the object
  store to local disk — never FUSE for the working directory** (object-FUSE is 10–100 ms/op,
  impractical for many small files); a node-local content-addressed blob cache so repeat artifacts
  never cross the network twice. All host-local, no proprietary service.
- **Harvest stays manifest-shaped, made diff-cheap**: hash outputs, skip blobs already in the
  store, bundle small-file swarms. Whole-filesystem snapshots are a *resume optimization*, never
  the artifact mechanism.
- **Gates park free**: at a human gate, kill the frame; DBOS parks at zero compute; the next step
  re-materializes from durable sources. Any provider snapshot is *cache* — **expiry must degrade
  to rebuild, never to failure**; Postgres + the object store are the only truth, which is exactly
  what keeps DBOS replay sound.
- **Durable execution**: stay **DBOS** — a library on the Postgres we already run, no cluster, no
  extra port. Temporal earns its own cluster only at polyglot/high-fan-out scale ("ship with DBOS,
  revisit if you hit the wall").

## What we already match vs. the gaps

| Layer | Already there | Gap to close |
|---|---|---|
| Object storage | CAS sha256 on the S3 API, presigned split, two endpoints | checksum-verified uploads; small-file bundling; a lifecycle policy |
| Context injection | frozen pack, offline archivist, scope walls, caps · **C0 cache discipline landed** | (none — landed) |
| Retrieval | FTS + budgets, ADR-011 ladder | (none yet — pgvector, then a self-hostable vector store, on triggers) |
| Event streaming | append-only, seq replay, two lanes, Lane-1 `id:` · **C0 batched NOTIFY landed** | partitioning; open-source bus at fan-out |
| Frames | ephemeral, rebuildable, driver seam · **sandbar microVM substrate built (ADR-013)** | production rollout: baked templates, CAS node cache, diff harvest, warm pool |
| Execution | DBOS replay, deterministic ids, gates park | (none — validated) |

## Sequenced plan

1. **Phase C0 — portability hygiene (no new infra). Landed:** prompt-cache discipline; the
   batched-NOTIFY hub. **Remaining:** bundle small-file harvests.
2. **Phase C1 — production storage, provider-neutral**: checksum-verified presigned uploads; a
   store-agnostic download path (presigned URLs from the configured public endpoint — MinIO on a
   bare box, R2/S3/Ceph in prod, all by config); a lifecycle/retention policy; an *optional* cache
   knob (self-hosted nginx/Varnish or the deploying cloud's CDN) documented but off by default.
3. **Phase C2 — isolated frames in production**: roll out the **sandbar** driver (built, ADR-013)
   as `frame_driver`; baked templates; parallel object-store hydration + node blob cache; diff
   harvest; a warm pool. (E2B/Modal adapters remain available for buy-not-run operators.)
4. **Phase C3 — scale valves, on triggers**: pg_partman partitioning of `task_events`; a
   Valkey/NATS pub/sub bus at fan-out; read replicas + pooler posture; pgvector, then a
   self-hostable vector store, per ADR-011 triggers.

## Risks

1. **Isolation rollout** — sandbar exists and is proven; its production edges (egress hardening,
   warm pool, rootfs pipeline on a Linux host) are the remaining work, not a purchase.
2. **Hydration latency at scale** — without baked templates + node caches + diff harvests,
   per-step frames blow latency SLOs and multiply object-store request costs. Host-local fixes.
3. **Accidental provider lock-in** — the discipline is the portability contract: if a change makes
   the system *require* a service the operator cannot self-host, it is a design smell, not a
   feature. Every accelerator ships with a self-hostable substitute or it does not ship.
4. **Vendor-number optimism** — cold-start and latency figures from managed-sandbox and vector
   vendors are vendor-published; re-benchmark on the target substrate before committing. Our own
   substrate (sandbar) we measure directly.
