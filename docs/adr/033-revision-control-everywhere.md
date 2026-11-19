# ADR-033 — Revision control everywhere: restore, text editing, named versions — and `.html` joins the document kind

*Status: accepted · 2026-08-17 · completes the ADR-015 M3 spine*

## Context

The revision spine existed and was load-bearing (ADR-015 M3): append-only content-addressed
lineages (`lineage_id`/`revision`/`supersedes_id`, unique per lineage), AI follow-ups chaining
same-basename re-publishes at harvest, per-revision verification, the durable
`artifact.revised` event, version chips in the reader. But the USER's half was one kind wide:
`revise` was collection-only, there was no way to make an old version current again, and no
way to name a version. Separately, one wire kind still wore two web names ("Page" for .md/.txt
documents — flattened to **Document** in the same workstream), and a static HTML page had no
honest home: the `page` skill shipped it as a `webapp`-typed directory with a bundled
`test.sh`, while the interactive App lane it borrowed stays a dead letter.

User decisions (2026-08-17): restore any version (all kinds including binaries) + edit text
artifacts in-app + named versions; no diff view. HTML becomes a document format; interactive
apps remain the separate App kind.

## Decision

**Every mutation is a mint; nothing is ever overwritten.**

- **Restore-by-minting** (`operations/artifacts.restore`, `POST /artifacts/{id}/restore`):
  the path names the revision to bring back; the body carries the CURRENT HEAD's sha (the
  revise 409 shape — two racing restores cannot interleave; `uq(lineage, revision)`
  backstops). The new head copies the target's `sha256/storage_key` (content-addressed — no
  re-upload) and **carries its `preview/rendition` sidecars forward** (identical bytes keep
  them honest — the deliberate inverse of revise, whose changed content drops them);
  `supersedes_id` is the HEAD it replaces, never the row it copies; provenance rides with
  `agent="user"`. Restoring bytes the head already carries is an idempotent no-op (harvest's
  by-digest posture). Works for every kind, binaries included. Restores stay UI-derivable —
  a row whose bytes equal an earlier revision's — so no provenance schema grew.
- **Text revise**: the collection-only wall came down for `.md`/`.txt` documents.
  `ReviseIn.content` is `dict | str` — the shape pairs with the kind and the operation
  refuses the wrong pairing naming the right one; text validates UTF-8, non-empty, ≤5 MB.
  Same base-sha 409, same event, same dropped sidecars. (`storage.put` moved off the event
  loop via `to_thread`, harvest's law — a pre-existing wart.) `.html` is viewable, not yet
  editable — recorded here. Code (a tar) and datasets are restore-only.
- **Named versions**: `artifacts.label` `String(80)` nullable + `POST /artifacts/{id}/label`
  (null clears; revise's authz). Presentation metadata like the sidecar pointers — the
  content stays immutable; no event (surfaces fetch the lineage fresh; the UI is optimistic
  and self-correcting). POST-verb over PATCH deliberately: artifact rows are immutable by
  doctrine, and the precedented mutations are verb-suffixed actions — a `PATCH /artifacts`
  would advertise general mutability the resource does not have.
- **The web history** (`components/artifact/revision-history.tsx`): the bare chip row grew
  into the lineage's story inside ArtifactView — the one component all four hosts mount —
  as a QuietDisclosure: per revision `v{n}` · label · actor ("you" for user provenance) ·
  derived "restored from v{k}" · relative time · verdict badge, with hover actions Label /
  Download-this-version (on-demand presign) / Restore (popover confirm, non-head only).
  A new head fires `onArtifactChanged` up through BOTH reader hosts so the task page
  repoints `openArtifact` — fixing the pre-existing wart where the reducer's in-place
  `artifact.revised` repoint blanked the band after collection saves.
- **The text editor** (`renderers/document-edit/`): renderer-owned (the collection
  precedent — the shell stays fetch/boundary/floor-only). Edit appears only when
  `type === "document"` and the host can follow a new head (a `.csv` dataset rides the same
  text hint and must never grow an editor); the server enforces rights at save. Markdown
  edits as SOURCE (mono, no live preview); a 409 keeps the draft, re-bases the NEXT save on
  the head, and says so — no auto-rebase, the person is the merge engine. Drafts are
  in-memory only (a persisted draft would resurrect stale text over newer revisions).
- **`.html` is a document** (`schemas/outputs.py`): accepted at the door, deliberately NOT
  in `text_extensions` (raw markup through the text renderer would be the mojibake path's
  cousin — `render_for` says NONE). The presentation is the existing sandboxed page cage:
  `document_preview` emits the same `{kind:"page", html}` sidecar a code tar's root
  `index.html` always rode. Verification is real: stdlib `extract_html` (text nodes, title,
  script/style skipped) feeds the prose pipeline — a `width="600"` can never become an
  ungrounded figure — while `check_links` reads the RAW markup, where the hrefs live.
  No LibreOffice rendition (the page preview IS the presentation, the webapp posture).
- **The `page` skill reshaped** (same commit): one `/work/page.html` published as
  `type="document"`, pre-publish self-check via its bundled `check_page.py`; the
  directory-plus-`test.sh` webapp shape retired (its two directory gotchas replaced with the
  reshape note — recorded, not thrown away). The typesetter's description gained
  self-contained `.html` pages — it holds the shell the skill's checker needs; the writer
  does not. Router mapping unchanged; routing evals re-run green at the 95% gate (96%; the
  two rotating misroutes are model variance in cases untouched by this change).

## Consequences

- GC needed nothing: restore adds no `*_sha256` column and reuses existing digests —
  `any_with_digest` already covers content + both sidecars (pinned by test anyway).
- `revisions_of`'s 200-cap/no-cursor stance stands; heavy per-turn churn reaching it is the
  bug report, as its docstring says.
- Migration `c9d1e3f5a7b9` (label). Contract regenerated (ReviseIn union, RestoreIn,
  LabelIn, `ArtifactOut.label`). CLI grew `artifacts revisions/restore/label`. Zero new
  dependencies (the editor is a ui `Textarea`; the history composes kit).
- Flagged for later, deliberately out of scope: diff view; a viewer-role field on the wire
  (true edit pre-disable — today Edit shows and a forbidden save keeps the draft and names
  the reason); `.html` in-app editing; `ArtifactOut.role`; renaming the `page` skill or the
  `preview.kind === "page"` sidecar string; STORAGE-backed drafts.
