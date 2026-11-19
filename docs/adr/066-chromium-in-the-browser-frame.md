# ADR-066 · Chromium in the browser frame: one pinned build, and the container as its sandbox

**Status:** accepted · 2026-10-04 · records the dependency computer use added without an
ADR or a HANDOFF §3 row (the `browser` frame profile). Changing the sandbox
posture below is an owner decision; this ADR records the posture, it does not choose a new one.

## Context

The `browser` profile (`ai/frames/image_browser/Dockerfile`) layers Debian bookworm's `chromium`
package, two font packages and the stdlib sidecar onto the base frame. The navigator renders
arbitrary, possibly hostile pages in it: Chromium is the one component in a frame that parses
third-party content, and the default identity-provider allowlist already lets a page an attacker
authored load without an ask.

Frame images are content-addressed: `image_digest` hashes the build context, and `ensure_image`
builds only when that tag is absent. With `apt-get install chromium` unpinned, each worker froze
whatever build its first browser frame resolved, under a tag that never changed. Two workers
built on different days ran different Chromium under one tag, and no Debian security update
reached any worker until someone edited a file in `image_browser/`. Found on 2026-10-04: a local
image built five days earlier held `154.0.8037.57-1~deb12u1`, bookworm-security already shipped
`154.0.8037.92-1~deb12u1`, and the older build was already gone from the archive.

## Decision

**1. One exact build, pinned in the Dockerfile.** `chromium=<version>` names a bookworm-security
build. The tag hashes the Dockerfile, so the same tag means the same Chromium on every worker,
and a bump is a new tag that every worker builds on its next browser frame (and that `just
sandbar-browser-image` names for sandbar). `chromium-common` follows the pin through Debian's
exact-version dependency. The fonts stay unpinned: they parse no page content, and their drift is
cosmetic. `tests/unit/test_frame_image_tags.py` fails if the pin is removed.

**2. Bumped on purpose.** The bump follows Debian's Chromium security updates (the DSA feed, or
the candidate `docker run --rm python:3.12-slim-bookworm sh -c 'apt-get update -qq && apt-cache
policy chromium'` prints): edit the one version, build, run
`tests/integration/test_browser_frame.py`. Debian removes a superseded build from the archive, so
a stale pin stops building on a fresh worker at the apt step. That failure is the backstop alarm,
accepted knowingly: a browser that cannot be built at its pinned, recorded version is refused
rather than silently built at another one.

Rejected: an unpinned install with the resolved version folded into the tag (the digest is
computed offline, before any build, and cannot know what apt will resolve); a
`snapshot.debian.org` date pin (always resolvable, so a stale pin would never fail, which is
exactly the silent-drift defect, and the snapshot mirror's throughput makes a worker's first
browser frame slow).

**3. `--no-sandbox` stays, with the container as the boundary.** Chromium's own sandbox needs
either unprivileged user namespaces or its setuid helper. The frame denies both by design: on
both Docker backends (the dev driver and sandbar's) every frame runs with `cap_drop=ALL` and
`no-new-privileges` (which voids a setuid binary), and Docker's default seccomp profile refuses
the namespace `clone`/`unshare` calls without `CAP_SYS_ADMIN`. So the sidecar starts Chromium with
`--no-sandbox` on every backend (`sidecar/browser.py`). The
compensating controls:

- Chromium runs as its own uid (1001, `browser`), never the agent's (1000). Its profile, cookie
  jar, DevTools pipe and the sidecar socket are 0700 and browser-owned; the agent's shell cannot
  read them (`test_the_agent_user_cannot_reach_the_profile_or_the_socket`).
- DevTools rides a pipe (`--remote-debugging-pipe`), never a port; nothing in the frame listens.
- The frame network has no route out. The only way out is the egress proxy, which allows only
  the task's allowlist and resolves each host, refuses any non-global answer and pins the dial.
  A compromised renderer reaches only hosts the task may already reach.
- Memory and CPU caps (and, on the dev driver, a 512-process limit) bound what a runaway page
  costs.
- On sandbar's Firecracker backend the frame is its own microVM kernel (ADR-013); the Docker
  backends share the host kernel.
- The pinned, bumped build above keeps the renderer at Debian's patched release.

What it still costs: a renderer exploit runs as uid 1001 inside the container. That uid owns the
cookie jar and the sidecar socket, so it can read the session's cookies and drive the logged-in
browser, with whatever network the allowlist allows; on the Docker backends a kernel exploit is
one step further. The alternatives for the owner, each a change to the frame's security options
on both backends: a seccomp profile that admits user namespaces for browser frames only
(Chromium's namespace sandbox, the hardening the 2026-08-24 tool-substrate research recommends),
the setuid `chromium-sandbox` helper (a setuid-root binary in the frame, and `no-new-privileges`
must go), or browser frames on Firecracker only.

## Consequences

- Same tag, same Chromium, on every worker; a bump reaches all of them through the tag.
- A stale pin fails fresh builds until someone bumps it. Workers that already hold the image
  keep running the older build until the bump changes the tag, so the bump cadence is a human
  process, and the failed build is its backstop.
- HANDOFF §3 gains the `chromium` row.

## Amendment 2026-10-15 - one `computer` agent (agent loop, round 1)

The navigator and the builder are one agent, `computer`: browser, shell, files, `frame.publish`,
`web.fetch` and `web.search` in one frame. The names `navigator` and `builder` stay as aliases
that resolve to `computer`, so old plans, bots and journals still compile. It does NOT hold
`connector.request` this round: a computer step cannot call a connector mid-browse,
which keeps the found-live never-connect-first rule; the operator covers services.

The page-to-shell path is the new boundary and is held by what already stood: the step's egress
allowlist, no credentials in the frame environment, page text fenced as data, and every tool
call through the warden. Loopback is open for every computer step (the builder's use
case folded in). Cost: a page could steer the browser to a Machine-local port. **Owner item.**

The computer keeps `verifier_suite=RECORDING`, not "none" as first designed: an agent holding
`frame.publish` must name how its output is checked
(`test_an_agent_that_can_publish_must_say_how_its_output_is_checked`). The recording's
structural receipt is signed after delivery (ADR-019). The prompt carries the coder's
`test.sh` code contract, since code without one fails verification. Navigate-door steps now
hold shell and loopback too.

The frame image pin (Debian chromium 154.0.8037.92-1~deb12u1) can no longer be rebuilt: the
package left Debian's repo. A fresh host or Branch Build fails until the pin is bumped. **Owner
item.**

## Amendment 2026-11-12 - the headed user agent, and the endings a bot wall gets

`PRISMOID_WORKER_BROWSER_HEADED_UA` (off by default) launches Chromium with the user agent it sends
with a window: Linux, the same major version, no other browser or OS claimed. Its purpose, said
plainly, is to pass a site's headless filter (an Akamai-class wall resets the stream on the
`HeadlessChrome` token alone). It is the operator's call because it is a bot-filter bypass. Nothing
else hides automation: `navigator.webdriver` stays true, no CAPTCHA is solved, and no other
fingerprint is changed. It reaches the docker and sandbar drivers only; a driver whose computer
launches its own browser (the device) ignores it.

A stream reset (`ERR_HTTP2_PROTOCOL_ERROR`) on an open is a blip once and the wall twice, for the
same host or its `www.`/bare twin: the second ends the step "the site turns away automated
browsers", and the failure is final (another attempt meets the same wall). A flaky origin that
resets twice gets the same ending: the cost of not retrying a real wall for ever.

A page that comes back empty (no title, next to no text) names the hosts the egress refused for it
(a bot check whose solver is off the allowlist never reloads into the page). Only the refusals since
that page was opened are named, only allowlist refusals, and only hostnames; the note rides after
the page fence, and what makes such an ending final is read from the tool's own words, never from
page text.
