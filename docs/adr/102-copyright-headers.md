# ADR-102 · Copyright headers on every source file

**Status:** accepted · 2026-11-18 · before the repository goes public.

## Context

Prismoid is published under `AGPL-3.0-only`, and the license said so only in `LICENSE` and the
package metadata. A file copied out of the tree carried nothing that said whose it was or under
which terms. Plane's own repositories open every source file with a three-line notice and check it
on pull requests; Prismoid follows the same notice.

## Decision

1. **The notice is `COPYRIGHT.txt`** at the root, three lines: the copyright line
   (`2026-present Plane Software, Inc. and contributors`, the year the project began), the SPDX
   identifier and a pointer to `LICENSE`. It is the one place the text lives.
2. **Every tracked source file opens with it**, in its own comment syntax: `#` lines for Python and
   shell, `//` lines for Go (so a `//go:build` line below it stays a valid constraint), a `/** */`
   block for TypeScript, JavaScript and CSS. It comes after a shebang when there is one, then a
   blank line. Not headed: `backend/vendor/` (vendored code keeps its own notices), generated code
   (`*.gen.ts`, which `pnpm gen` rewrites), type declarations (`*.d.ts`) and empty files. The Alembic
   template carries the notice, so a new migration is born headed.
3. **One tool, three uses.** `backend/tools/copyright_header.py` (standard library only) names every
   tracked source file without the notice, or writes it in with `--fix` (`just copyright`,
   `just copyright-fix`). It never writes over or above a copyright notice already near a file's top:
   someone else's notice is not ours to cover, so the file is named for a person to decide.
   - `tests/laws/test_law_copyright_header.py` holds the rule in `just check`.
   - `.github/workflows/copyright.yml` runs the tool on every pull request and push to main as its
     own status, with an annotation on each file it names. It installs nothing.
4. **The ratchet counts a file's length after its header** (`quality.code_lines`): a legal notice
   is not code, so heading the tree moved no baseline entry and pushed no file over its cap.

## Alternatives rejected

- **`google/addlicense`, as Plane runs it.** A new dependency and a Go toolchain in the job, and its
  check accepts any file that mentions a copyright, so a wrong year or holder passes. The tool here
  checks the exact text and runs with the Python every runner and laptop already has.
- **A check of the files a pull request changes, only.** The whole tree takes under a second, and
  checking all of it also catches a file that lost its header in a move.
- **Headers on generated and vendored files.** A generator would strip them on the next run, and a
  vendored file's notice belongs to its authors.

## Consequences

- A new source file needs the notice before `just check` passes; `just copyright-fix` writes it
  (stage a new file first: the tool reads `git ls-files`).
- Changing the notice is one edit to `COPYRIGHT.txt`, then a rewrite of every header; the check
  names every file whose header no longer matches, and `--fix` does not rewrite an old header (it
  refuses a file with a notice already near its top), so a holder or year change needs its own
  script.
