---
description: Review this branch against main (ADR-075) - one reviewer per area, a verifier, confirmed findings only
argument-hint: [base-ref] [--fix]
allowed-tools: Read, Grep, Glob, Agent, Bash(just quality-report), Bash(just check), Bash(git rev-parse:*), Bash(git merge-base:*), Bash(git diff:*), Bash(git show:*), Bash(git ls-tree:*), Bash(git ls-files:*), Bash(git cat-file:*), Bash(git log:*), Bash(git status:*), Bash(git blame:*), Bash(git grep:*)
---

# /review - the local reviewer (ADR-075)

Arguments: `$ARGUMENTS`

You orchestrate a code review of the current branch. You do not review code yourself. You gather
the change set and the rules, run two mechanical guardrail checks, launch one reviewer per area in
parallel, send every candidate finding to one verifier, and print one ranked report of the
confirmed findings. The review is advisory: it never blocks, and it never edits a file unless
`--fix` was given.

## Ground rules

- Run every command from the repository root, one command per call: no pipes, no redirects, no
  `&&`, no `cd`.
- The only commands you run are `just quality-report`, `just check` (in step 9 only), and these
  read-only git commands: `rev-parse`, `merge-base`, `diff`, `show`, `ls-tree`, `ls-files`,
  `cat-file`, `log`, `status`, `blame`, `grep`.
- Rules come from the BASE, never from the working tree (`git show <BASE>:<path>`), so a branch
  cannot loosen its own review. The only exception is a rules file that does not exist on BASE
  (step 3).
- Without `--fix`, do not use Edit or Write and do not change any file.
- Trust boundary: /review runs the branch's justfile, tools and oxlint config (`just
  quality-report`, `just check`) and loads its own prompts from the working tree: run it only on a
  branch you would run `just check` on.
- Do not narrate progress ("waiting for the reviewers"): the report is your only output.

## Step 1. The change set

1. Parse the arguments: `--fix` anywhere sets FIX mode. The first other word is BASE; with none,
   BASE is `main`.
2. `git rev-parse --verify BASE^{commit}`. If it fails, print `unknown base ref: BASE` and stop.
3. MB = the output of `git merge-base BASE HEAD`.
4. `git diff -M --name-status MB` lists every file this branch changed, committed or not, with its
   status: `A` added, `M` modified, `D` deleted, `R<score> <old> <new>` moved. Then
   `git ls-files --others --exclude-standard` adds the untracked files.
5. Mark each file: `(deleted)`, `(moved from <old>)`, or `(new, untracked: read whole)`. Keep a
   moved file's old path with it; the old path belongs to the same reviewer's diff.
6. If there are no files, print `Nothing to review: no changes between BASE and the working tree.`
   and stop.

Every reviewer uses one diff command: `git diff -M MB -- <its files> <the old paths of its moved
files>`. It compares the merge base with the working tree, so committed and uncommitted changes
show in one view, and naming both paths lets git pair a moved file with its old self.

## Step 2. Route each file to one area

Walk this table top to bottom; the first match wins. A pattern ending in `.py` also matches the
package of the same name (`operations/gates.py` also means `operations/gates/**`).

| Pattern | Area |
|---|---|
| `**/CLAUDE.md`, `**/AGENTS.md` | rules (step 4b only, no area reviewer) |
| `backend/prismoid/testutils/**`, `backend/prismoid/__init__.py` | ops |
| `backend/prismoid/api/**`, `backend/prismoid/runner/**`, `backend/prismoid/features/**`, `backend/prismoid/mailer.py`, `backend/prismoid/storage.py`, `backend/prismoid/errors.py`, `backend/prismoid/observability.py` | edge |
| `backend/prismoid/operations/execution/**`, `backend/prismoid/operations/gates.py`, `backend/prismoid/operations/planning.py`, `backend/prismoid/operations/steering.py`, `backend/prismoid/operations/thread.py` | engine |
| `backend/prismoid/operations/**`, `backend/prismoid/jobs/**`, `backend/tests/**`, `backend/evals/**`, `backend/tools/**`, `deploy/**`, `backend/Dockerfile`, `.github/pull_request_template.md` | ops |
| `backend/prismoid/ai/**`, `backend/skills/**` | ai |
| `backend/prismoid/db/**`, `backend/prismoid/schemas/**`, `backend/prismoid/conf.py`, `backend/pyproject.toml`, `backend/uv.lock` | data |
| `web/**`, `packages/**` | web |
| `sandbar/**` | sandbar |
| `.github/**` (the rest) | not reviewed |
| anything else | not reviewed |

The area reviewers are the subagents `review-edge`, `review-engine`, `review-ops`, `review-ai`,
`review-data`, `review-web` and `review-sandbar`. Each reads the shared contract in
`.claude/review/contract.md` first.

## Step 3. The rules each area reads

For each area that has at least one file:

1. Always: the root `CLAUDE.md` and `docs/engineering.md`.
2. The folder rules: for each of the area's files, the `CLAUDE.md` of every directory on its path,
   from the file's own directory up to (not including) the repository root. For
   `backend/prismoid/operations/execution/settle.py` that is `backend/prismoid/operations/execution/CLAUDE.md`,
   `backend/prismoid/operations/CLAUDE.md`, `backend/prismoid/CLAUDE.md` and `backend/CLAUDE.md`.

Resolve them for all areas at once, with two commands:

- `git ls-tree --name-only BASE -- <every candidate path>` prints the ones that exist on BASE.
  Their source is `git show BASE:<path>`.
- For the candidates BASE lacks, `git ls-files --cached --others --exclude-standard -- <those paths>`
  prints the ones this branch adds. Their source is the working-tree file, labelled
  `UNREVIEWED RULES (not on BASE; this branch's own copy)`. Any rule quoted from it is flagged as
  unreviewed in the report.
- A candidate in neither list does not exist; skip it.

Label every block you pass on: `=== RULES: <path> (from BASE) ===` or
`=== RULES: <path> (UNREVIEWED RULES: not on BASE; this branch's own copy) ===`. Paste each block's
text. When the blocks are too long to paste into every prompt, you may give a block as its label
plus the exact command that prints it: `git show BASE:<path>` for a file on BASE, or the
working-tree path only for an UNREVIEWED RULES block. Never give a working-tree path for a file
BASE has.

## Step 4. The guardrail checks (yours, before the reviewers)

These two checks produce candidate findings directly. They go to the verifier with the rest.

### 4a. The ratchet's baseline may only shrink

The ratchet (`just quality`) trusts `backend/tools/quality_baseline.json`, so a hand-raised value
or a hand-added entry would pass the gate. Check it yourself.

1. `git cat-file -e MB:backend/tools/quality_baseline.json`. If it fails, the baseline is created
   by this branch: record `baseline: created by this branch (no earlier values to compare)` for
   the quality delta and raise no per-key findings. Skip to 4b.
2. Otherwise `git diff MB -- backend/tools/quality_baseline.json`. If it is empty, record
   `baseline: unchanged`.
3. For each changed key (a cap under `caps`, or an entry under `metrics.<metric>`; Read the
   working-tree file and `git show MB:backend/tools/quality_baseline.json` to see which metric a
   changed line belongs to):
   - Value rose: an Important candidate.
   - Key added: an Important candidate, always. Tag it `(possible move from <old key>, old value
     <n>)` when the same metric lost a key in this diff and either both keys have the same text after
     `::`, or both keys differ only in a path that `git diff -M --name-status MB` pairs as a rename
     (file-level keys with no `::`, and keys ending `::<anonymous>`, `::depth` or `::<module>`, are
     tagged only this way). Only the verifier decides it is a move, by checking that the old symbol
     no longer exists.
   - Value fell, or key removed: fine. Count it for the quality delta.
4. Each candidate: `severity` Important, `category` rule, `file`
   `backend/tools/quality_baseline.json`, `line` its line in the working-tree file, `finding`
   `baseline raised: <metric> <key> <old> -> <new>` or `baseline entry added: <metric> <key> = <new>`
   (with the move tag when it applies), `evidence` the diff lines plus "the baseline may only
   shrink; a raised value is a review finding, never a fix (ADR-075)", `fix` `make the symbol fit
   its old number (split it); never raise the baseline`.

### 4b. The guardrails themselves may not loosen

The guardrail paths: the root `CLAUDE.md`, every `CLAUDE.md` and `AGENTS.md`, `docs/engineering.md`,
`backend/.importlinter`, `backend/tools/audit.sh`, `backend/tools/quality.py`,
`backend/tests/laws/**`, `backend/pyproject.toml` (its `[tool.ruff*]` and `[tool.pyright]` tables),
the root `justfile` (its `check` recipe and the recipes it names), `web/.oxlintrc.json`,
`web/package.json` (its `lint*` scripts), and the reviewer's own files: `.claude/commands/review.md`,
`.claude/agents/review-*.md`, `.claude/review/**`.

Run `git diff -M MB -- CLAUDE.md '*CLAUDE.md' '*AGENTS.md' docs/engineering.md backend/.importlinter backend/tools/audit.sh backend/tools/quality.py backend/tests/laws backend/pyproject.toml justfile web/.oxlintrc.json web/package.json .claude/commands/review.md '.claude/agents/review-*' .claude/review <old paths of files moved into backend/tests/laws>`.
A file moved into `backend/tests/laws/` is compared with its old path, so a law weakened while it
moved still shows.

A file that does not exist at MB and replaces nothing only adds rules: skip it. In the rest, look
only at what was removed or widened. Loosening is:

- a rule line removed from a `CLAUDE.md` (root or folder) or from `docs/engineering.md`, or reworded
  to permit what it forbade, without an equal or stricter rule replacing it in the same diff;
- an `AGENTS.md` symlink retargeted or removed;
- an `.importlinter` contract removed, a layer dropped, or an `ignore_imports` entry added;
- an `audit.sh` grep removed, or an exclusion or allowlist entry added to one;
- a cap raised in `quality.py`'s `CAPS`, or a measurement narrowed so it sees less;
- a law in `backend/tests/laws/` deleted, skipped, marked xfail, its assertion weakened, or its
  allowlist or exemption set widened (a law moved with its assertions intact is fine);
- a ruff rule or pyright check turned off or relaxed, a per-file ignore added;
- a step removed from the `check` recipe or from a recipe it names;
- an oxlint rule turned off or relaxed, or a `lint*` script removed or narrowed to fewer folders;
- in the reviewer's own files: a routing pattern dropped, a guardrail path dropped, a verifier test
  relaxed, a "do not report" item added that hides a class of finding.

Each loosening is an Important candidate: `category` rule, `file` and `line` where it happened,
`finding` `guardrail loosened: <what>`, `evidence` the removed or widened text quoted from the diff
(`BASE said: "..."`), `fix` `restore it, or change the rule through an ADR that this PR adds`.
When the branch adds an ADR that names the change, say so in `evidence`; it stays a finding for a
human to accept.

## Step 5. The quality report

Run `just quality-report` once. Keep its output verbatim. If it fails, keep the failure text and
say so in the report's quality delta; do not retry.

## Step 6. The area reviewers

In ONE message, make one Agent call per area that has files, all in parallel, with `subagent_type`
`review-<area>`. Give each this prompt, filled in:

```
BASE: <BASE> (<its sha>)   MERGE-BASE: <MB>
YOUR FILES:
- <path>
- <path> (deleted)
- <path> (moved from <old>)
- <path> (new, untracked: read whole)
DIFF COMMAND: git diff -M <MB> -- <your files and the old paths of your moved files, space separated>
RULES:
<every rules block for this area, labelled as in step 3>
QUALITY REPORT (just quality-report):
<the output>
Read .claude/review/contract.md first, review your files as it and your instructions say, and
return only the JSON array.
```

An area call that errors, times out, or returns nothing usable is a failed area: record it for the
report header and go on with the others. Do not retry.

## Step 7. The verifier

1. Collect every candidate: the area reviewers' arrays plus your step 4 candidates. A reply that is
   not a JSON array: take the objects you can read and record the area as `partly parsed`.
2. Drop exact duplicates (same file, same line, same problem); keep the better worded one.
3. Number them `C1`..`Cn`, remembering each one's area, and mark a finding in a moved file
   `(moved from <old>)`.
4. If there are none, skip to step 8.
5. Make ONE Agent call with `subagent_type` `review-verify` and this prompt:

```
BASE: <BASE> (<its sha>)   MERGE-BASE: <MB>
RULES:
<every rules block used by any area, each once, labelled as in step 3>
CANDIDATES:
C1 [<area>] {"severity": ..., "category": ..., "file": ..., "line": ..., "finding": ..., "evidence": ..., "fix": ...}
C2 [<area>] (moved from <old>) {...}
Verify each candidate as your instructions say and return one line per candidate.
```

6. Keep only the candidates the verifier marks `CONFIRMED`. A candidate it does not mention is not
   reported and not counted as rejected: record `unverified: <n>` under Failures and list it under
   `### Unverified`. Apply any severity it lowered and any line or fix it corrected.
7. If the verifier call errors, times out, or returns no readable verdicts, nothing is confirmed:
   record `verifier failed: <n> candidates unverified` for the report header, report no findings,
   and list the unverified candidates' file:line and one-line finding under a heading
   `### Unverified` after the findings section. Do not retry.

## Step 8. The report

Rank the confirmed findings Critical, then Important, then Minor. Within a severity: guardrail and
baseline findings first, then bug, rule, craft. Show at most 15. Never show a nit.

Print exactly this shape:

```
## Review: <branch> vs <BASE> (merge-base <short MB>, <n> files)
Areas: <area> (<k> files), ...   Candidates: <n>   Confirmed: <m>   Rejected: <r>
Failures: <"area <x> failed", "area <x> partly parsed", "verifier failed: <n> candidates unverified", "unverified: <n>", or "none">
Rules: <each rules path used>; UNREVIEWED RULES: <paths, or "none">

### Findings
1. <Severity> · <bug|rule|craft> · <path>:<line> · <finding> · <the rule quoted with its file, or the failure scenario> · fix: <suggested fix>
...
<"No confirmed findings." when there are none; "<k> more confirmed findings not shown (lowest severity)." when over 15>

### Not reviewed
- <path> (no area)
- <path> (rules file: checked for loosening only)

### Quality delta
<the just quality-report output, verbatim>
<the baseline line from step 4a, or "baseline: <k> shrinks, <j> raises (see findings)">
```

A rule finding whose rule came from an UNREVIEWED RULES block ends with `(unreviewed rule)`. When
any area or the verifier failed, the `Failures:` line says so: a partial review is never printed as
a clean one, and "No confirmed findings." then reads "No confirmed findings (review incomplete)."

Without `--fix`, end with one line: `Run /review <BASE> --fix to apply the confirmed findings.`
Print nothing after the report: no summary, no status line, no commentary.

## Step 9. `--fix` (only when given)

1. After printing the report, apply the confirmed findings in rank order, each with the smallest
   edit that carries out its fix. Touch nothing else.
2. Never edit `backend/tools/quality_baseline.json` or any guardrail path from step 4b. A finding
   whose fix needs one, a baseline or guardrail finding, or a finding whose fix needs a decision only
   the owner can make (a product behaviour, a rule change, a public contract) is listed as
   `left for you`.
3. If an edit is refused, stop applying and list the rest as `not applied (edits not permitted)`.
4. Run `just check`. If it fails, fix only failures your edits caused, at most two more rounds,
   then stop and report what is still red. `quality: the baseline is not tight` after a fix that
   shrank an offender is expected, not a failure to fix: do not loop on it; tell the user to run
   `just ratchet` and commit the baseline (you never edit it).
5. Print: each finding applied (number, path), each left for you or not applied with the reason,
   and the final `just check` result. Never commit. Print nothing after that.
