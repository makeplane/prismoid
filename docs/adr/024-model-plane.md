# ADR-024 — The gateway grows a model plane: media lane, provider discipline, quality routing

**Status:** accepted · 2026-08-08
**Context:** the ~70-model world (Perplexity-Computer-class orchestration: text from one
family, images from another, routed per requirement), and a gateway that had none of the
discipline that world demands: no per-provider concurrency, no 429 handling beyond
FallbackModel silently *changing the answer's author*, a fresh httpx client per chain
candidate, no image generation anywhere, and no user lever on cost/quality but fleet seats.

## Decisions

1. **The path stays `ai/gateway/`** (user decision: no rename, no branding). Rule 7 is
   constitutional and ~20 import sites deep; identity lives in this ADR.
2. **The media lane** (`ai/tools/providers/images.py` — the second sanctioned model-string
   home): image models are CATALOG ROWS (class `image-gen`, protocols `openai_images` /
   `google_images`, priced in `Unit.IMAGE`) served by the warden-granted `generate_image`
   TOOL, never as chat seats — `build_model` refuses media protocols by name. The tool runs in
   the worker process and bypasses frame egress BY DESIGN (the ADR-020 connector precedent:
   explicit grant + keys-only journal are the policy; the PROMPT is content and never reaches
   a label). Bytes land via the new `Frame.write_bytes`; candidates try in chain order; a dead
   chain returns an instructive placeholder string — an image failure costs the figure, never
   the step. Seeded: `openai:gpt-image-1.5` + `xai:grok-imagine` enabled (xAI speaks the
   OpenAI images wire — a base_url row, zero code), `google:imagen-4-fast` disabled (user
   decision: ChatGPT-image + Grok lead). The engine contract makes `tools` and `gateway`
   siblings, so `build()` binds candidates + per-candidate usage emitters
   (`meter.units_emitter`) as plain values — Rule 8 holds with no new wiring.
3. **Per-provider discipline** (`ai/gateway/throttle.py`): one shared `ConcurrencyLimiter`
   per provider (conf `provider_limits`, per process, ÷ `replica_count`) wrapped
   `ConcurrencyLimited(Metered(model))` inside the fallback — limiter outside the meter so
   queue wait is never billed, per-candidate so a fallback to B never holds A's slot, meter
   innermost for attribution; pins are not exempt. One shared keep-alive httpx client per
   provider whose transport retries 429/502/503/504 on the SAME model honouring Retry-After
   (≤3 attempts) — only then may FallbackModel change models. Per-class request timeouts
   (`CLASS_TIMEOUT_S`: reasoner/long-context/code 300 s, fast 180 s, judge/classifier 90 s).
   The `retries` extra rides the existing pydantic-ai dependency (tenacity was already locked
   transitively).
   *Amended 2026-11-14 (one retry policy):* the retryable statuses are 429/500/502/503/504/529
   (`throttle.RETRYABLE_STATUSES`); the SDKs' own retries are off through one constant; the chain
   fails over only on those statuses, a refused key (401/403) or no answer at all, never on a
   refusal of the request (400/413/422); and a run that failed over stays on the row that answered
   (`resolve.StickyFallback`), the rows it passed tried again only after the rest of the chain.
   *Amended 2026-11-15:* a prompt past one row's context window (pydantic-ai's
   `ModelContextWindowExceededError`, a 400) also fails over, since the next row may hold it; every
   other refusal still raises at once.
4. **The quality dial**: `tasks.quality ∈ {max, balanced, economy}` (NULL = balanced =
   pre-dial order, byte for byte) → `build(quality=)` on the STEP path only → `chain_for`
   re-ranks the UNCHAINED tail by a static price blend (input + 3×output; the image unit for
   media). Explicitly-chained refs never move (operator intent outranks the dial); unpriced
   rows sort LAST under both dials (a missing price is never rewarded as "free"); the judge
   lane never sees a quality — verification integrity is not a cost dial. The dead
   `prefer_family` hook (zero callers since M2.2) was removed for it.
5. **Catalog ergonomics at ~70**: `models import` (upsert that never touches `enabled` and
   refuses a silently changed price — `--reprice` writes book N+1), `models doctor`
   (enabled-but-unpriced · key-missing · class-unserved · dead routing refs — exit 1 for CI),
   `PATCH /admin/models/{ref}` grew `classes` (the CLI's DB-direct debt paid), and `resolve()`
   caps built candidates at `max(4, explicitly-chained)` — 70 rows never build 70 clients.
   OpenRouter is a T2 recipe (openai-compatible + base_url), deliberately not a constructor:
   an aggregator is tail-of-chain breadth, never the head — sovereignty posture.

## Deferred, named
Conversational image lane (`openai_responses` constructor + ImageGenerationTool,
`gemini-3-pro-image`) · standalone image deliverables · `provider_policy` table (conf is the
v1 home) · measured routing (persist smoke scores into `eval_scores`; `duration_ms` on
usage_events) · catalog cache (version-stamp seam) · distributed provider limits · client
`aclose` on worker shutdown.
