# ADR-097 · The code frame profile: git, ripgrep and node for a code step

**Status:** accepted · 2026-11-15 · adds three tools to a new Docker frame image; the profile choice is the
format skill's (ADR-091), and other drivers' templates are their owners' to follow.

## Context

A code step's skill told it to build and test in the sandbox, and leading harnesses search with `rg` and
diff with `git`, but the frame image had python3 and nothing else a code step reaches for: no git, no
ripgrep, no node. Adding them to the base image would put them under every memo and deck step too.

## Decision

- `ai/frames/image_code/Dockerfile` is the `code` profile, built `FROM` the base tag. It adds git and
  ripgrep from Debian bookworm at one exact build (ADR-066's pin rule) and node with its bundled npm,
  copied from the official `node:<version>-bookworm-slim` image pinned by version and digest (the current
  LTS, the binary nodejs.org verified, for either arch). The browser image is now built `FROM` the code
  tag, so the profiles are a line, base < code < browser, and `schemas/machines.widen` takes the wider:
  a Machine never re-images to give a later step fewer tools.
- A format skill names its image in frontmatter (`frame-profile: code`, stored as `prismoid-frame-profile`).
  `frame_profile_for` widens a format maker's image to the one its deliverable's skill names, off the
  device only; `seat_profile(task, spec)` is the one answer every step path asks. A code artifact is
  verified on the same image (`skills.deliverables.verify_profile`), so a `test.sh` that calls node re-runs.
- Egress is unchanged: no registry is reachable unless the step's contract allows it.

## Alternatives rejected

- The tools in the base image: every step pays for them, and the base image's size is ADR-023's budget.
- Node from Debian (`nodejs` 18, past end of life) or a per-arch tarball with hand-kept checksums.
- A browser image beside, not above, the code one: a code step after a browser step would lose its tools.
- D17's wider kit (fd, tree, jq, build-essential, uv, pytest): not asked for yet; build-essential alone is
  about 250 MB.

## Consequences

Measured: the code image adds 84 MB compressed and 350 MB on disk to base (1.45 to 1.80 GB; node is
150 MB of it, npm 19 MB, git with perl and ripgrep the rest); the browser image grows by the same layers
(2.36 to 2.71 GB). A first build of the code layer takes about four minutes. Boot time is the
container's and did not move. The database's `machines.profile` CHECK gains the word (a migration). Sandbar and
other drivers have no code template yet: there a `code` step boots their base image (no git or node), and is
priced as base unless the price list names `code`.
