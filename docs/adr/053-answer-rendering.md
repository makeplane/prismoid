# ADR-053 - The answer render: rich blocks, code cards, and the em-dash law

**Status:** accepted · 2026-08-29 (owner, same session as proposed) · W0-W1 landing.
**The owner's answers, recorded (AskUserQuestion, 2026-08-29):** renderer stays in-house
(react-markdown v2, no streamdown); blocks v1 is ALL FOUR (email · message · csv · mermaid,
mermaid taken against the builder's defer lean, mitigations binding); email actions are
Copy + "Use this draft" (composer prefill; real send arrives with mail connectors, in the
same spot); the mono face is JetBrains Mono. Added at plan review: remove every em dash
from the platform, search deep, keep them out.
**Grounding:** the 2026-08-29 render-path audit (three explore passes over web/backend) and
a landscape pass over streamdown/remend internals, AI Elements, llm-ui, Claude artifacts,
MCP-UI/AG-UI, and the Gemini/Copilot email-draft UX.

## Context

Answers stream as append-only markdown (HANDOFF §8.6) into one renderer,
`web/app/components/kit/markdown.tsx`: react-markdown + remark-gfm, one `table` override,
a token-driven `.markdown` CSS layer. The audit found the renderer honest but thin: fenced
code landed at ~10.8px with no highlighting and no copy affordance while a fully built
shiki CodeBlock kit (`ui/code-block.tsx`, vendored AI elements) sat product-unreachable;
h4-h6 rendered as paragraphs; task lists double-marked; external links navigated the SPA
away; the one live segment re-parsed its whole accumulated markdown ~30x/s under the
smooth-reveal hook; a second feed row carried a dead `prose-sm` class; no mono webfont
existed. Meanwhile the owner wants content blocks (email drafts first) styled and testable
BEFORE connectors land, so integrations arrive on ready-made UI.

## Decision

**One renderer, upgraded in place.** `kit/markdown.tsx` keeps react-markdown and gains
`components` overrides: `pre` is the dispatch point (fence language + raw source read from
the hast node); a fence whose language is registered renders its special block, any other
fence renders the lazy `AnswerCodeBlock` (composing the vendored shiki kit: header with
language label left and copy right, scroll-not-wrap, no line numbers, unknown languages
sanitized to text); `a` sends external links to a new tab; `img` auto-renders only
same-origin/API/relative sources and renders any other URL as a quiet link chip (the
ADR-046 exfiltration shape, honored client-side); inline code stays CSS.

**The block registry.** `kit/special-blocks.tsx` maps fence language to a component taking
`{raw, streaming, ordinal, total, renderBody}`. `renderBody` is injected by markdown.tsx
and renders a plain variant (no special dispatch), which kills the module cycle and makes
nesting structurally safe. Adding a block is one file + one registry line, the artifact-
renderer grammar. v1 ships `email` (To/Cc/Bcc/Subject header lines, markdown body, letter
card, dual-MIME copy, "Use this draft" via an optional `MarkdownActions` context: no
context, no button, the capability law), `message` (same draft grammar, bubble card),
`csv` (parsed preview capped at 50 rows, copy-as-CSV), `mermaid` (lazy import, strict
security level, closed-fences-only, theme-aware, code-block fallback on any error).

**The constitution holds.** Fences are PRESENTATION only; a fence never executes anything.
Actions are user gestures: copy, or a composer prefill the user reviews and sends as the
task's next turn. When a mail connector is attached, the same context grows a send
affordance that still rides the ordinary follow-up lane. Rich interactions keep arriving
from typed events (§8.6); the fence convention is the same class of thing as syntax
highlighting, applied to model-authored content.

**Streaming.** A pure `splitBlocks` (fence-aware, conservative: when in doubt it does not
split) lets the live segment render as memoized per-block fragments under the ONE
`.markdown` div, so only the tail block re-parses per frame; a pure `healTail` closes
unterminated emphasis/inline-code, strips a partial trailing fence-opener line, and closes
an open fence that has body. Settled segments render as one parse, exactly as before
(footnotes and reference links heal at settle).

**Type.** `--font-mono` joins the theme (JetBrains Mono Variable, self-hosted via
fontsource like Inter); ADR-017's one-face law governs the heading seam and is untouched.
The `.markdown` layer gains h4-h6, task-list, loose/nested-list, image, del, and footnote
rules; the fenced fallback moves to text-sm so the async shiki upgrade never reflows; the
size-compounding that broke the 17px root's ~12.7px floor is gone.

**The em-dash law (W0).** Every em dash is out of the platform's source: UI copy
(copy-edited: middot for meta joins, comma/colon/period/parentheses for clauses), backend
user-facing strings, CLI help, prompts, skills, comments and docstrings (mechanical
hyphen). Sixteen prompt voices carry an explicit style rule so model output stops emitting
them; the gather seat was exempt while it was its own agent; it is now the researcher's
gather mode (ADR-091) and carries the identity's rule, and its excerpts are checked through the
same normalizers that eat em dashes. Three em dashes survived as DATA, respelled by escape (`\u2014`) so the gates stay
blind to them: the figure-caption regex (`plane_house/text.py`), the quote strip-set
(`verifier/suites/quotes.py`), and `EDGE_PUNCTUATION` (`suites/document.py`), each a
normalizer that must keep eating em dashes precisely because models and sources emit them.
Two permanent gates: `pnpm lint:dash` in web-check and `tests/unit/test_no_em_dash.py` in
the backend suite; both honor a deliberate `em-dash-ok` fixture marker. Excluded with
reasons: `docs/**` and the applied migrations (the frozen record), generated files, en
dashes in numeric ranges.

## Adopt / decline

| Candidate | Verdict | Why |
|---|---|---|
| `@fontsource-variable/jetbrains-mono` | ADOPT (W1) | the mono role's face; same self-hosted pattern as Inter |
| `mermaid` | ADOPT (W4, owner call) | diagrams in answers; the buggiest surface in the landscape, so: lazy dynamic import only, closed fences only, strict securityLevel, error falls back to the code block, static render is a placeholder |
| `streamdown` (+ `marked`) | DECLINE | not a react-markdown wrapper but a second pipeline; Tailwind-coupled to its own tokens; ships its own code-block UI beside our vendored kit; open copy-in-nested-scroll bug lands exactly in our peek panes |
| `remend` | DECLINE | healTail is ~50 pure testable lines against our exact stream shape; a dep for that inverts boring-beats-clever |
| `rehype-raw` / `rehype-sanitize` | DECLINE | §8.6 says never HTML and react-markdown v10 already drops raw HTML by default; nothing to sanitize that we permit |
| `katex` / `remark-math` | DECLINE (revisit on demand) | no math lane exists; weight without a user |
| `@tailwindcss/typography` | DECLINE (again) | the .markdown layer is the deliberate, restrained voice; prose-* was a dead class and is now gone |
| `jsdom` + `@testing-library/*` | DECLINE | the house idiom (pure cores + renderToStaticMarkup) covers every new surface; interactions are proven live in the browser |

## Consequences

- Every Markdown consumer (thread, peeks, inbox, artifact reader, routines, admin bots)
  upgrades at once; the deliberately-plain surfaces stay plain (user bubbles are pinned by
  test; progress-tree rows, epilogue lines, gate prompts and qa rows keep their row
  grammar).
- shiki and mermaid live only in lazy chunks; the eager kit chunk gains no highlighter.
- The composer grows an imperative insert handle (`apiRef`), the ScrollBridge idiom, so
  "Use this draft" can prefill without lifting composer state.
- The `pre` fallback (before the lazy chunk arrives, and in static tests) is the CSS-styled
  `<pre>`: degradation is legible, never raw backticks.
- Prompts teach the ```` ```email ```` / ```` ```message ```` convention (W7) so drafts
  arrive structured; other clients render the fences as readable plain code blocks.
