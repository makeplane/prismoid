# ADR-078 · The device Machine

**Status:** accepted · 2026-10-31 · the owner's ask of 2026-10-28 and the decisions of 2026-10-29
(in the device-machine design of 2026-10-29, unpublished).
**Amends** ADR-063: a Machine's sleep may be `none`, and a kept frame is destroyed only by
Reset or the end of its task. **Amends** ADR-070: the early boot's rule reads the task's driver,
never the worker's. **Builds on** ADR-077 (`prismoid up`) and ADR-063 (the browser the worker drives, amended 2026-10-26). The word is *device*: code, API and CLI say it, and copy names the computer ("Waiting
for MacBook Pro").

## Context

Prismoid's Machines are borrowed computers, disposable by design: a bad command costs a sandbox. The
device driver points a task at the person's own Mac: their folders, their toolchains, a Chrome
window they can watch and take over. Three things stop being true. `/work` cannot exist (macOS has
a read-only system volume, and Seatbelt cannot remap paths). A uid no longer separates the agent
from the system, because everything runs as one user. And a recovered step's fresh attempt, harmless
in a sandbox, could redo real edits on real files.

The threat is a prompt-injected agent, not a compromised Prismoid. In v1 the worker, API and helper are
one person's processes on one computer.

## Decision

1. **The helper is a separate process, and the helper enforces.** `prismoid device helper`, started by
   `prismoid up`, is the one process that touches the computer. It listens on a Unix socket
   (`<home>/run/helper.sock`) and requires `X-Prismoid-Helper-Key` (`<home>/helper.key`, 0600) on every
   route, `/healthz` included. It imports only `prismoid.schemas`, `prismoid.conf` and
   `prismoid.egress_relay` (an import contract holds it). A command's env is built from an allowlist,
   never inherited from the helper's. The worker dials it and never the reverse. The helper judges
   each path, grant and Docker call itself, and does not trust the server.
2. **`sandbox-exec` is the fence.** Every agent command runs under a generated Seatbelt profile
   (`device/seatbelt.py`), starting from `(deny default)`: reads everywhere minus a deny list of
   secrets and Prismoid's home, writes only in the frame's roots and a per-frame cache
   (`~/Library/Caches/Prismoid/<ref>`), network only to loopback minus Prismoid's ports. Paths that run code
   later outside the fence (`.git/hooks`, `.envrc`, rc files, editor tasks) stay write-protected
   inside a granted folder, and a folder granted read-only stays so inside one granted read-write.
   The profile is written again for every command, so a root is held where
   it is: Seatbelt refuses to unlink or rename one, a grant's real path is resolved once and pinned
   in `frame.json`, and a write root that is a link at the next command refuses the fence. A pre-existing repository's `.git` is read-only apart from an allowlist
   of state files; new repositories are made only in Prismoid-owned roots. `launchctl` does not run:
   launchd starts nothing for the fence, but it answers `disable`, and that is kept past a reboot.
   The lists are data
   (`device/fence_data.py`). The file door is not sandboxed, so it mirrors these rules itself and
   runs no shell.
   Apple deprecated `sandbox-exec`. It is the accepted risk, shared with Claude Code, Codex and
   Chrome; if it goes, the fence has to be rebuilt on another mechanism.
3. **Virtual paths are canonical; `FramePaths` renders them.** A frame exposes its roots as
   `FramePaths`. Cloud drivers keep the standard roots, so a render is the identity and their
   prompts stay byte-identical. On the device `/work` is the granted folder (or the frame's own
   fresh `work/`) and the others sit under `<home>/tasks/<task_id>/`. What Prismoid itself writes -
   skills, manifests, stored checks, and every path an event names (a `tool.called` summary, a
   live label, a snapshot's path) - speaks virtual paths, and translation happens at the driver:
   the verbs accept either spelling, and a refusal, a journal head or a label goes back virtual
   (`virtual_text`, `shown`), folded before any clamp. The model is told real paths, because a path
   it cannot use helps nobody, so what the model itself says can hold one; "The rest" names where.
   The other exception is deliberate: the person's own input, the `device_grants` row and the
   task's folder.
4. **Sleep is `none`.** A third `sleep_kind` beside `archive` and `native`: nothing is archived,
   because `/work` is the person's folder and there is no checkpoint to take. A sleep stops what
   the task left running (`StopsServices`) and keeps the frame. Idle stays ADR-063's 10 minutes.
   Warm time is metered as usual and priced 0 (`Unit.SECOND`, provider `device`). A device frame
   is destroyed only when no row exists or by an explicit Reset: the helper reuses
   `device-<task_id>`, so "this wake's frame" is the kept one, and the reconciler never destroys a
   persistent frame while any row exists. Destroy removes Prismoid's own folder and never a grant.
5. **No blind re-run.** `exec` is not a durable effect, so a device step that was running when the
   worker or helper went away never starts a fresh attempt on its own. It parks on a question-less
   gate ("Waiting for <device>", `device_away`). That holds for a worker crash, a helper dying alone,
   and a step's wall-clock stop, even while the command may still be finishing. A helper that merely
   does not answer is waited for through a grace (`DEVICE_GRACE_S`, 90 s), so a Mac sleeping briefly
   is not a park. The helper's local journal names the command that was cut off; it is acknowledged
   only after the park and the record commit in Postgres, so a crash between them re-parks and loses
   nothing. The person resumes, and the new attempt is told what was cut off and to inspect the
   folder first. Past `PRISMOID_LOCAL_AWAY_LIMIT_H` (24) the task ends failed, with its reason.
   Before any fresh attempt the ladder asks the same journal, because an attempt can also end under
   a command that runs on: a sibling's refusal in a parallel batch cancels only the worker's side
   of the call, and the helper's handler is not cancelled when its client goes. A command still
   running parks the step the same way, unless a person already resumed past it. A cloud task
   never asks.
   Away gates knock the inbox and are exempt from the pause-park cap: the owner did not cause them,
   and a step whose Machine is away cannot hold at a frontier.
   A helper asked to stop (Ctrl-C on `prismoid up`, `prismoid down`, SIGTERM, a closing terminal) stops what it
   runs before it exits, in this order and inside the supervisor's 15 s. Every process of every frame
   on the Mac, the whole tree, SIGTERM then SIGKILL inside 5 s (live run 2026-10-31: uvicorn runs an
   app's shutdown only after its requests end, a request here is a command, so the supervisor killed
   the helper outright and the tree ran on). Then the frames' containers (owner, 2026-10-31): those its
   proxy made that still wear the frame's label, stopped and never removed, volumes and images
   untouched, `t=1` and 4 s for Docker in all, so a daemon that is down or slow costs that wait and
   nothing else, a paused one included. From the first line of the stop no Chrome starts. Then the
   plant walk the normal end runs, what it moved told with the next life's
   first command. A second Ctrl-C does not cut this short: the commands live in sessions a kill of the
   helper never reaches. A command stopped this way has no result and no `finish`, even if it exits 0
   on the SIGTERM: the worker hears a 503 (`FrameUnreachable`) and the journal still owes the cut.
   A helper killed outright stops nothing itself. The next one's start kills the groups `groups.json`
   names and what still descends from them, and stops the frames' containers before its door opens.
   A command that ends after its worker has gone (the worker stops first) is recorded as finished and
   unseen, so the resumed attempt is told it finished, with its exit code, and never "cut off".
   That line is the last command only until it is not: a later unseen is named first, a result somebody
   heard closes it, an ack ends it, and a command the worker acted on while it ran is never told
   again. None but a finish or an ack ends a cut. A note to the agent (a plant moved, a server stopped)
   is on disk from the moment it is known and leaves only when a result carrying it was heard, so it
   survives a killed life, a walk that outran the stop and an answer into the void. A command the
   journal cannot hold never runs: it answers 126 and leaves no entry.
6. **Private-only, and a task never moves.** A device task cannot enter a Project with members
   besides its owner, and such a Project cannot gain one (`device_task_private`, a 409, also on
   transfer and `si_` accept). Only the device's owner drives. A task never moves between the device
   and a cloud Machine: a rebuild uses the task's own driver. Parallel siblings run serially,
   one live task holds a folder (`folder_busy`, by a realpath + NFC + casefold key, ancestors and
   descendants included), and a fork copies no folder. A deployment's device is one computer, so the
   rule and its one advisory lock hold across workspaces: `DeviceGrant.folder_holder` is the one tenant
   read that crosses them. It still needs a bound workspace, and it returns a value, never another
   workspace's row. Only a holder's own creator is told which task it is.
7. **A device in a cloud deployment is unsupported in v1.** The API stamps `tasks.machine_driver`
   only in local mode, so a NULL driver means a cloud task, and private-only reads it that way.
   The cloud topology is designed for and not built, and it needs more than this ADR: grants
   confirmed on the device, since a server must not be able to grant itself.
8. **Browser egress has two layers.** One Prismoid Chrome per device, its own profile under
   `<home>/chrome`, one window per task, so logins are shared and lists are not. A proxy cannot tell
   windows apart, so the inner layer is CDP interception (`Fetch.enable`) vetting each request
   against that task's allowlist, with popups held to their window (fencing is by window, not
   opener). The outer layer is the proxy, enforcing the union of live tasks' lists. It covers what
   Chrome sends outside a tab, service workers included, and is the floor if interception misses.
   An opener-less page in no task's window is held to the empty base list: the person's own new
   window in the Prismoid Chrome reaches nothing. A loopback port is reachable from a window only if
   that task's commands listen on that address and port. Downloads are blocked in v1. The Prismoid
   Chrome runs no extension: extensions and component extensions with background pages are off,
   because their requests sit outside the inner layer. The PDF viewer has no background page and
   stays. A window behind another one keeps rendering: a hidden page runs no animation frame, and
   a pointer move, which the agent's click sends first, waits on one. Rejected: a browser context or a
   Chrome per task, which loses shared logins. One Chrome means one DevTools pipe, and Chrome writes a
   request's whole body into `Fetch.requestPaused`, about 2.3 times its size as text and 7.3 times
   as zero bytes, on each of the two sessions that pause it. `Network.enable`'s `maxPostDataSize`
   takes it out of `requestWillBeSent`; `Fetch` has no such parameter (Chrome 156's own schema). So
   the helper keeps a message up to 64 MiB and lets a larger one pass by, keeping only its two ends,
   which are Chrome's own fields, read by depth in whatever order Chrome writes them (a page names the
   keys of `request.headers`, so a name counts only at its own depth). A request is refused on the
   session it paused on, a denial like
   any other, its host only; a proxy challenge is cancelled; a call fails; any other event goes
   unheard, which leaves something unknown, and an unknown refuses. The reading loop ends only with
   the pipe. Nothing else reads such a message, so nothing else need bound it: 1 GB passed in under
   10 s, with the helper's loop never held past 2 ms.
9. **The egress relay moved to `prismoid/egress_relay.py`.** The helper's proxy and the frame proxy
   share one forward relay, and `prismoid.device` may not import `prismoid.ai`. Like `prismoid/storage.py`, it
   is a neutral top-level module, mounted beside `egress.py` in the proxy container. Both proxies
   give a client 15 s for its request's whole head, and neither lets an upstream outlive a client
   that went. Nothing times a tunnel while both ends are there. A FIN is the one thing that is
   ambiguous, and only the device's proxy waits on it: a tunnel carries it across, and the other
   way runs on until it ends, and on a forwarded request too, a client's FIN ends the upstream
   only after 30 s of silence (`FIN_GRACE_S`), since a client that has finished sending may still be
   listening. A reset ends things at once. The Docker proxy's FIN closes the tunnel, as it always has.
10. **Docker is a grant, through a proxy, off by default.** The real socket is root-equivalent, so a
    fenced command reaches only the helper's per-frame socket, a filtering proxy
    (`device/docker_policy.py`, `device/docker_proxy.py`) that refuses what it does not recognise.
    Binds stay inside grants; privileged mode, host namespaces, devices, the Docker socket and a
    runtime other than the default are refused; bind-backed volumes are refused outright; published
    ports are forced onto loopback. The
    proxy acts only on this frame's objects, known by a per-frame journal rather than by labels
    alone, since an image can carry labels. It re-encodes every vetted body instead of forwarding
    the client's bytes. Containers stop on sleep and are removed by destroy; a cleanup that failed
    stays journaled and is retried. Images are shared and survive. A rw bind holding a protected
    `.git` gets a read-only bind of it inside the container.
11. **The folder is watched as well as fenced.** Seatbelt cannot see what a renamed folder holds, so
    after each command and at stop and destroy the helper scans user roots for what is new since the
    last scan: unlisted `.git` entries, bare-repository shapes, links pointing out. Those move to
    `<home>/quarantine/<ref>/` and the agent is told. What was there at grant time is the person's,
    and Prismoid never moves it: a link remade at its path with its target is still theirs, and a folder
    closed then (`chmod 000`) stays closed and unread. While a frame has something left running and
    no command, the ports watch walks too, every 30 s. Deleting a task deletes its quarantine, but
    for a slot that holds a repository. A listener on anything but loopback is killed and recorded.
12. **A coding task delivers a `change`.** The work stays in the folder; what crosses is a directory
    from `/out/change`: `patch.diff`, `report.md`, `test-output.txt`, `screenshots/*.png`. The repo
    is never tarred. A `change` publishes only from `out/` and refuses links; `code` and `webapp` are
    refused from a grant. Whether a step is a coding seat with browser tools, and that its
    deliverable is `change`, is decided in code from the task's driver and never by prompt. Verify
    judges what was captured during the step and re-runs nothing in the folder, since the person may
    be editing it by then.
    **The receipt line is self-reported:** `patch.diff` and `test-output.txt` are the agent's own
    evidence and the receipt says so. An engine-side `git diff` at harvest would be a new engine
    exec in the person's folder. That is an open owner decision.
13. **Prismoid's own runtime.** What a frame image bakes in becomes a venv at `<home>/runtime/`, pinned
    line for line to the image (`local/runtime_requirements.txt`, a test holds them equal), with
    `plane_house` in its site-packages. `prismoid up` builds it with uv when uv is on PATH, else with the
    Python Prismoid runs under and pip, from wheels only. A failed build never stops `prismoid up`: a typed
    publish then fails with a sentence saying how to retry. No `PYTHONPATH`: system commands run the absolute
    interpreter with `-I`, because `-I` ignores `PYTHONPATH` and the cwd (`out/`) is agent-writable.
    Agents read the runtime and nothing writes it. It holds only public packages and the house style.
    In a frame's own fresh folder it is `python3`; in a granted folder `python3` stays the person's
    (O4) and the runtime is `$PRISMOID_PYTHON`.
14. **Verify suites, QA prechecks and previews run on the runtime,** inside the fence, because
    without them every non-`change` receipt fails. Output is stored virtual, with the runtime as
    `/runtime`.
15. **LibreOffice is off on the device** (`OFFICE_RUNS_FENCED = False`), installed or not. It is an
    AppKit app even when headless: in the fence it hangs waiting on the window server, and it exits
    0 having done nothing without a socket in `/tmp`. Word, Excel and PowerPoint files get extracted
    previews and QA gets no page images from them. Flipping it needs the owner's word first, then
    the openings listed at the constant: the window server, dock, LaunchServices, cfprefsd, fonts and
    SecurityServer for system commands only, plus a short socket folder and a rendition folder
    (`local/runtime.py`). That would open all of it to a parser of agent-written files. poppler is
    the person's to install; without it, the page images for review are off and `prismoid up` says so.
16. **A root that needs quoting goes by an alias.** Prismoid's home has a space in it, and so may a
    granted folder. Quoting roots in a command would need a parser of bash, and that lexer kept
    growing edge cases whose failures silently changed file contents, so nothing parses bash. A root
    whose real path needs quoting gets a helper-made, space-free symlink outside the home that the
    agent cannot write, and exec text replaces the root with it by plain substitution. A bare root
    goes out as `alias/.`, so tools that do not follow a command-line symlink still reach the folder
    (`find`, `grep`, `tar`, `du`, `cp`). Output folds back to the real path, `alias/.` first; the
    journal stays virtual. A space in a root may come backslash-escaped, as a model fluent in shell
    types it (`Application\ Support`); that is one more spelling in the substitution, not a parse.
17. **A grant changes under a running step.** A person changes Postgres, and the worker tells the
    helper at ADR-064's before-tool-dispatch safe point, on a device Machine's frame only. One indexed
    read compares the task's grants with the last push (how many ever, how many revoked: no clock),
    and a change is pushed before the tool runs (`machines.grant_fence`, `PUT /frames/{ref}/grants`).
    The helper rebuilds the fence and serves the Docker proxy for the new roots in the same call. So
    a revoked folder refuses the next tool, in the same step and attempt. A push that fails stops
    the tool: the step parks as for any helper that did not answer. A file-door refusal is a tool
    result, as a missing file is (ADR-027), never a retry spent from `retries=3`, so an old path
    costs one turn and not the step, however many times the model tries it.
18. **A task on the device never boots early** (ADR-070). `machines.boots_early` is the one rule, and
    it reads the task's own driver (`driver_of`), as a frame does, never the worker's. ADR-070's boot
    runs ahead of a step: on a device it would make the frame in a person's folder before the first
    step's checks, pin the `browser` profile, take a warm slot and start the meter on a guess, and a
    helper that was away or a folder it refused would leave the row `gone`, so the first step would
    wake through the Reset path. Local mode's worker is never on a driver that boots early, but the
    task's driver is the one that decides where its frame lands, so the rule says so. A device
    task's first step starts its Machine, as every task did before ADR-070, and a cloud task is unchanged.
19. **A page's assets load from any public host, and nothing else does** (owner, 2026-10-31). A live
    run opened goodreads.com: Chrome's fence refused every stylesheet and image from gr-assets.com,
    145 of the run's 158 denials, with no gate, and the page drew as bare text. A person is meant to watch that window
    and sign in on it, and a page missing its assets is the hand-off's real risk. So, on the device
    only, on a site already allowed, a request `Fetch` types Stylesheet, Image, Font, Media or Script
    may go to any public host. "Already allowed" means the top-level URL of the page that asked is
    http(s) and on its task's list, for an iframe's request too. It is never about:blank, never a
    loopback page, and never a page in no known state: a fenced command cannot reach an off-list
    host, but it can serve a page on its own port for the agent to open, and that page must not
    carry the command's data out. The URL is the one `Page.frameNavigated` reports. A main-frame
    Document request forgets it until that navigation commits, so the page being left cannot excuse
    the first assets of the page replacing it, and a commit that is not where the newest navigation
    is going is not believed. A navigation Chrome cancels (a 204, a download, a `window.stop()`: that
    navigation's `Network.loadingFailed`, `canceled`, on the fence's session) commits nothing and
    leaves the page where it was, so the URL it last committed comes back, and nothing else changes:
    its documents, and so the worker and document rules below, are as they were. No commit the
    newest navigation failed to explain may have landed meanwhile, or nothing comes back, and a
    navigation that failed rather than was cancelled commits an error page and tells that itself.
    Under load a new page's first asset can beat word of its commit (about
    1 in 200 at eight parallel Chromes), so an asset paused mid-swap waits up to 2 s for the commit
    and no excuse exists while it waits. It is then excused only if its `Referer` is the origin
    that landed: the page being left can still be running, a request of its would otherwise borrow
    its successor's URL, and a page cannot write another origin's `Referer`. That covers the wait
    only, and a `Referer` proves nothing past it: what outlives a commit is a matter of seconds and
    not of a few ms. A dedicated worker with a busy loop survives its page's navigation, and a
    review had one from a fenced command's page reach an off-list host hundreds of times, excused by
    the listed page that replaced its own. So every excuse is tied to a document. `Fetch` cannot do
    it, since a dedicated worker's request pauses on its page's session under its page's frame, so
    `Network` is on in every task's session, and an excuse needs a request announced by a document
    that is in the page now: its frame and `loaderId` as `Page.frameNavigated` last gave them, under
    a main frame that is still the one committed. A worker's script is therefore never excused, of
    any kind, whoever made the worker: its session has no document, and that comes from the session
    rather than from a header, which the worker's own server shapes. A request nothing announced
    gets no excuse either. Every http(s) document between the top and the one that asked must pass
    the same test as the top, so a listed page framing a task's loopback server excuses nothing in
    that frame, and a document with no origin of its own (about:blank, srcdoc, blob:) rides on what
    holds it.
    Document (top-level and iframes), XHR, Fetch, WebSocket,
    EventSource, Ping, Other and any type not named stay fenced as before. The inner layer lifts
    only the list's refusal (`Refused.OFF_LIST`, `NO_LIST`): loopback, private, link-local and
    metadata addresses, a URL with credentials and a loopback port not the task's own stay refused,
    and each redirect hop is judged again. A page in no task's window, a service worker and a dead
    task's page get no excuse. It is no denial and no log line, and it reaches no `frame.allow`,
    grant or journal. The outer layer needed more than a wider list, because `Fetch` never pauses a
    WebSocket: a host the proxy admits for Chrome is a host a page's socket reaches. So the inner
    layer gives the proxy a pass for that host and port, 60 s from the last asset, read only for the
    `chrome` principal, never a frame's, and it still meets the resolve-time private-address
    refusal. A check at CONNECT is not enough either. Chrome sends a `wss:` down an open HTTP/2
    session to its host, inside a tunnel made for an image, when the server advertises extended
    CONNECT (RFC 8441, probed on Chrome 156), and there is no switch to turn that off. So the proxy
    has two doors, chosen by URL scheme: `https:` to one, `http:` and every WebSocket to the other,
    where no tunnel takes a pass. Chrome keys a session by its proxy, so a `wss:` has no session to
    share. `http:` is at that door because a WebSocket's 407 reaches no CDP event, nor does a
    cross-origin image's: Chrome needs each door's credential cached before a page runs, and one
    plain request to each from the first blank window puts it there. A tunnel on a pass must also
    open with a TLS ClientHello offering `h2`, which Chrome's https always does and its WebSocket
    and a TURN relay never do. That tells a browser's https from other protocols. It does not prove a
    request met the fence: what the browser or its network service fetches for itself skips `Fetch`
    and opens as https. On Chrome 156 a Reporting upload, Background Fetch, FedCM's config and a
    payment method's manifest each reached a host holding a pass, so those are off by flag
    (`Reporting`, `NetworkErrorLogging`, `FedCm`, `WebPayments`, and Blink's `BackgroundFetch`). NEL
    was not seen to send, and shares Reporting's uploader. Attribution Reporting sent nothing and
    its API is absent in a fresh profile, so no flag names it. Rejected: the helper
    fetching assets and answering with `Fetch.fulfillRequest`, which buffers every file through CDP
    and makes the helper a web client; widening the union, which hands the host to every page and
    to sockets; `Network.setBlockedURLs`, which does not stop a WebSocket (probed).
    The audit's "no URL literals" check names three exclusions for `device/egress.py`, all addresses
    nothing dials out to: the frame's own loopback proxy URL (`{frame.token}@127.0.0.1:{self.port}`,
    and the Chrome credential's), the second door's `http://127.0.0.1:{self.socket_port}`, and
    `SEED_URLS`, built on the `.invalid` host `SEED_HOST`, which the proxy refuses. The last two match
    whole lines, and a check of its own pins `SEED_HOST`.
20. **Research reaches the web through the computer.** With no search engine `web_search` does not
    exist, and the same live run's "find the five best-reviewed dungeon crawler books" went answerer,
    hatch, writer, and came back written from memory. That exact request is the
    browser's use case. So on a device task, with an empty search chain (`search_chain`, the one
    `build` uses), the computer in the roster, a catalog row this host can build for its class
    (`served_here`) and Chrome at `PRISMOID_LOCAL_CHROME_PATH` (`_check_chrome`'s own question, and the
    worker and the helper read that setting on the same Mac), the planner's brief says reading the
    web is the computer's: research goes to a `computer` step, whatever its roster entry says about
    research, a document follows from it, a researcher is not seated to find things out, and a writer
    does not answer from memory. A researcher stays seatable for what is attached and for URLs the
    request names, because `web.fetch` still reads those. It is a fact in one prompt and not a rule
    in the validator: the planner is the judge of what a request needs, and the hatch escalates as
    before. If the seat cannot run, the fact would be false and a `computer` step could only fail, so
    the brief stays the one every other deployment gets and a research goal delivers from what the
    model knows, as it did before. A cloud task's brief is byte-identical.

    *Amended 2026-11-11: the cloud Machine hears its own rendering.* Found live on a cloud Machine
    with no search engine: "use current on-road prices ... and cite sources" was planned as one
    modeler step, which has no web tool and estimated every price. The fact is now one answer,
    `planning_brief.web_reach` (`search | machine | device | none`), and its paragraph is one template
    rendered for where the browser runs: the device's words are unchanged (pinned word for word by
    `test_web_reach.py`), and a cloud task whose `computer` seat can run hears `WEB_ON_MACHINE` (the
    browser "on this task's Machine", never "the user's own computer", and a `modeler` after it for
    a spreadsheet). The guards hold: no engine, the computer in the roster, a row this host can
    build, and Chrome only on the device. The sentence above that a cloud task's brief is
    byte-identical held until this amendment; where no seat can run, it still is. ADR-047's
    amendment of the same day says what an engine changes.

    *Amended 2026-11-15: the computer is the operator's browser seat (ADR-091).* The paragraph names
    the `operator` where it named the `computer`, and lists no outcome agent among the readers of
    named pages; otherwise the device's words are unchanged. The seat runs while computer use is on
    and a browsing agent is in the roster. The device law holds for every operator step on the
    device, on the browser or off it: no blind re-run (a lost step parks, "Waiting for <device>"),
    private-only, and no move between the device and a cloud Machine.

21. **In a hand-off the window goes where the person goes** (owner, 2026-11-01). Step 4 of the live
    check found it: "Sign in with Amazon" on a listed Goodreads page redirected to `www.amazon.com`,
    every list refused it, and no gate could ask, because the agent was parked and nothing was
    running to ask. A sign-in leaves for whoever the site trusts (a company's SSO, a host no list
    could name), and the window is the person's during a hand-off, so a table of
    identity providers would only move the failure. So `browser_handoff`, as its last act, tells the
    helper (`POST /frames/{ref}/hand-over`), and each of the task's pages then on a listed site may
    navigate anywhere public, with every request of a document in it now. A popup a handed page
    opens is handed too, since a sign-in is often one. It lifts the list's refusal only, by the same
    `past_list` the asset excuse uses, and the proxy gets the same pass: loopback, private addresses,
    a credential in a URL and WebSockets stay as they were, and no list, grant or journal widens.
    It ends at the agent's first act, on the helper's own evidence and on no server's word: any
    command on the task's relay but the recording's, and any `exec` or file-door call, so a lost
    message cannot leave it open under a running agent. Nothing pushes a list under a parked step,
    so any push to the frame ends it too: a push is the resume's, ahead of the model's first turn, or
    the park given back with an empty list, which is how a task that ended or was cancelled and the
    kill switch reach the helper. So does a sleep's stop, and the frame going. Its ceiling is 30
    minutes (`PASS_S`), on the wall clock so that a Mac's sleep counts: 24 hours of gate is no reason
    for 24 hours of window with nobody at it, and a clock gone backwards is the end of it, since what
    the helper cannot tell is over. At the ceiling the agent stays parked and the gate stays open,
    and the window goes on under the list. A helper or Chrome that restarts forgets it. However it
    ends, a page left on an off-list site goes back to the last listed page it was on, or to
    about:blank, and the agent's first command waits for that (a page that will not leave is
    closed). A snapshot has no host check, and what a person's sign-in shows on a host no list names
    must not reach the model; if the agent needs that site it asks, as for any other. A page on a
    listed site stays, as does one a navigation is taking to one, so a sign-in that comes home keeps
    its page. The road it must not open is a page a
    fenced command writes, the one way a command's data reaches script in a browser (decision 19),
    so a page on loopback at the hand-off is never handed, a frame of such a page is not carried, and
    a worker never is. A page steers what it opened, and what opened it, through a window reference,
    with no person there (proved on real Chrome), so the rule holds for the whole task: while any of
    its pages holds a loopback, private, `data:`, `file:` or such a page's `blob:` document, nothing
    is handed, and one committing during the pass ends it for every page, an iframe's too. A
    navigation on its way to one counts already: a page's first assets can beat word of its commit.
    Nor does the rule end with the document. A popup's `about:blank` runs the script its opener wrote
    into it and shows nothing a URL could tell, and on Chrome 156 `Page.frameNavigated` says `://` for
    its origin, so a command's page could open one, leave for a listed site, and have the popup steer
    the handed tab off the list (proved). So a page that ever held such a document marks every page it
    opened with a window reference, before or after, and what those open, and nothing is handed while
    a marked page is open, whatever it shows by then. A `noopener` tab holds no reference
    (`canAccessOpener`), which is what the agent's own new tab is, and a page that opened nothing
    keeps no mark of its own: its document, and every reference in it, went when it left. A tab with
    no opener is not handed, whatever it is on: a page can conjure one from a synthetic Cmd-click. A
    listed page could already be sent anything the agent has, so handing it opens no new road to that
    host. It does open one past that host, by a redirect (Known limits).

    A sign-in frames hosts of its own (a captcha, an identity provider's session frame), so an
    iframe's Document request is its parent's: the document it asks for is not born until the request
    goes, and the chain above it is what is judged, every document between. Chrome names the parent
    in `Page.frameAttached`. A second call of the hand-off changes no ceiling and takes no page away.
    The pass's end looks at every page the task drives, not only the ones it held. A listed page
    stays, with what the person typed, and each iframe on it off the list goes to about:blank before
    the agent's next command: a screenshot reaches a frame in another process, so a session frame's
    name would reach the model. Not a reload, which would lose a solved captcha. A frame that will not
    go takes its page. A page off the
    list with a navigation to a listed site under way still holds what it left, so the agent's first
    command waits for the landing, up to 10 s (`_HOME_S`), and the page is judged again: a sign-in
    that lands keeps its page, and one Chrome cancels (a 204, a `window.stop()`, Esc) or that does not
    land goes back. On the way back, an `errorText` or a top that has not changed is a page that has
    not left, and it goes on to about:blank and then to closing. What stays in a page's history of a
    site off the list reaches the agent as its host and path (`Chrome.shown`, in the relay, so no
    cloud byte moves): `back` to such an entry lands on an error page, and the entry keeps the
    sign-in's URL whole, a code in its query and a token in its fragment (proved).

## Known limits

- During a hand-off a listed site's own scripts are not held to the list: a tracker it loads can
  call home, as it would in the person's own browser. Only until the agent's next act.
- A task with a preview of its own open on loopback gets no hand-off's pass: the sign-in is refused
  until the preview's tab is closed and the agent hands off again. A popup that tab opens, or opened,
  bars it just the same, whatever the tab shows by then: a sign-in that opens a popup, from a tab that
  once showed the preview, ends the pass as the popup opens. The agent's own new tab does neither.
- A URL the agent writes on a listed site can carry its data off the list during a hand-off.
  Outside one every redirect hop is judged, and in one a navigation hop passes, since that is what
  a sign-in is. So an open redirect or an OAuth `state` on a listed host (`github.com` is on every
  device frame's list) takes what the URL holds to a host no list names: after the person's consent
  there, or with no person if the page redirects on a timer and the agent hands off first. Nothing
  tells this from a real sign-in. Requiring a gesture on each off-list chain stops only the timer
  form, and breaks an SSO whose last hop is a script.
- A file a person later runs by hand is not a fence's to judge: `.pre-commit-config.yaml`,
  `lefthook.yml`, `.vscode/launch.json`, a `Makefile`, `package.json`'s scripts. They are ordinary
  project files, so an agent writes them in a read-write folder and the diff is what shows it.
- A folder the person had closed at the grant is theirs and stays closed, so the walk does not look
  inside it. An agent in a read-write folder can open one, leave a plant, and close it again. It
  fires only if the person opens that folder and runs git there.
- A walk that starts as a command starts, or a change of grants during a command, can move a
  build's short-lived outward link to quarantine. The agent is told, and the build runs again.
- A sign-in slower than the ceiling loses the page it was on, and the person starts again after the
  agent hands off anew. The way back is a plain GET of a URL already loaded, so a site with an action
  on a GET sees it twice.
- A hand-off's person cannot leave for a new site from a tab they opened themselves (Cmd-T): the
  sign-in must start from the page the agent left, or a popup of it. A WebSocket stays held to the
  proxy's rule. A click refused before the agent hands off is not retried: the page goes back to the
  listed page it left and the person clicks again, since the agent can write the refused URL.
- Every page and iframe bypasses service workers (`Network.setBypassServiceWorker`), so a site's
  offline mode does not work in the Prismoid Chrome. A worker in the profile (www.amazon.com's, from an
  earlier task) otherwise answered a page's navigation outside its fence. A worker's own requests
  stay held to the union of the lists. A tab or window with no opener starts its first navigation
  before anything can reach its session, so a worker can still answer that one, past `Fetch`, with
  no request to refuse (proved on Chrome 156). The helper finds such a page off its list when it
  lands, takes it to about:blank and keeps a refusal. Until then its script runs, under the fence,
  in a tab the agent cannot drive.
**The network.**
- Loopback is wide. A same-exec `server & curl localhost` must work, and Docker-published
  ports are not frame pids. So a proxy the person runs locally (ClashX and the like) or a Chrome with
  `--remote-debugging-port` is reachable from the fence, and that is an egress bypass.
- A bind to `0.0.0.0` is reachable from the LAN until the listener watch kills it, within about 2 s.
  SBPL cannot express the difference. The device law says to bind 127.0.0.1. UDP listeners are not
  watched.
- A re-parented daemon is not killed. One that `setsid`ed and went to launchd cannot be proved to be
  the frame's, and what cannot be proved is never killed, so it can keep a listener open. That holds
  at a stop too: a `setsid` child whose parent lives is found, one whose parents all died first is
  not, and a SIGTERM handler can arrange that on purpose.
- `https://localhost` is refused in the Prismoid Chrome: a TLS first request shows no `Origin`, and
  https and wss are indistinguishable at CONNECT. The device law says to serve plain http on
  loopback.

**Docker.**
- Container egress is not fenced. Pulls, a container's network, `ADD <url>`, `build ?remote=` and
  BuildKit's fetches go out through the Docker VM. The grant says so in plain words.
- BuildKit's `/grpc` is relayed unvetted: it is BuildKit's whole control API, so a build can prune the
  person's build cache or ask for entitlements (whether insecure ones are granted is the daemon's
  config). Classic `/build` query parameters are not vetted either.
- An agent's build or pull can overwrite local image tags. It is the same trust as editing the
  Dockerfile the owner then runs. Images are not scoped by task, and list and inspect are unfiltered.
- `NetworkMode=host`, `cgroupparent` and `extrahosts` on a build, and `HostConfig.CgroupParent` on a
  container, are not refused. Container egress is unfenced as above, "host" in the daemon's VM is the
  VM and not the Mac, and `CgroupParent` is confinement, not escape.
- A revoked folder leaves the proxy at once, but a container started before the revoke keeps its
  bind until it stops. A sleep or the helper's stop stops it, and Reset or the end of the task
  removes it.
- A daemon that does not answer inside the helper's 4 s keeps its containers running, unfenced, until
  a later sleep or helper start reaches it. A stopped container is not started again: a resumed
  attempt starts what it needs. What the proxy never journaled (`docker` run by the person, or an
  image's own `LABEL prismoid.frame`) is never stopped.
- A container has no fence, so its rw bind reaches what `PROTECT_WRITE` keeps from a command. What
  the person has there (`.envrc`, `.vscode`, `.idea`, a shell's rc file) is laid read-only over the
  bind, and with the Docker grant on, what is new there is moved like any plant. That cannot be
  enforced at the write: an overlay needs its path to exist, and Docker would make it in the
  person's folder. So a new one stands until the next walk, after a command, at a sleep or at the
  frame's end, and a person who opens the folder before that runs it. Nor can a walk tell who wrote
  it: a `.envrc`, `.vscode` or `.idea` the person adds during such a task goes to quarantine too,
  though their edits to what was there do not. A revoked Docker grant stops this while a container
  started before it keeps its bind.
- A bind source swapped for a link between the start re-check and the daemon's mount is a small
  residual race. "Host" namespaces here mean the Docker VM, which mounts `/Users`, and that is why
  binds are fenced. Docker through an alias is proven on OrbStack only.

**Git and the folder.**
- Repositories nested in a grant are tiered, because SBPL rules cost time per command (64 full rules
  took 19 s; 512 is `E2BIG`). The shallowest 16 get full rules, up to 512 more are `pinned_git`
  read-only with parents unrenamable, and past that they are known and never moved but unfenced.
  So `rm -rf node_modules` and `npm ci` fail where a nested repo lives there, until the owner removes
  it.
- `pinned_git` repositories get no Docker overlay, so a container can write those. Commits inside
  a container fail on the 16 full-rule repositories; the agent commits from the Mac side.
- Interactive rebase, git-lfs, `submodule update`, `bisect`, `worktree add` and `am` fail in a
  protected repository; `git init` is refused inside a grant. The agent says so, and the person does
  it.
- Bazel's convenience links and pnpm's global virtual store are quarantined: legitimate links
  pointing out look like plants. Only a venv's `bin/python*` is exempt. Bazel in a granted folder
  fails until the owner moves its output base.
- What a person adds to a granted folder while a task runs meets the same walk: nothing can tell who
  wrote it. A repository they start there (`git init`, `git clone`, or an editor's tool rewriting a
  `.git` pointer file) keeps its working files, and at the next walk its `.git`, or a bare
  repository's `HEAD`, `objects` and `refs`, moves to `<home>/quarantine/<ref>/<n>/item/`, beside a
  `manifest.json` naming where it was. A link they add that points out of the grants goes the same
  way, as does one they remake with another target; one remade with the same target stays. The agent
  is told, and nothing tells the person. Deleting the task deletes every other slot and keeps one
  that holds a repository, its own or at any depth of a folder it holds, and nothing but the person
  removes that. A `src/{head,objects,refs}/` of ordinary code takes the same road: names are all the
  walk reads.
- What the fence protects cannot be switched by git. In a repository that tracks `.envrc`, `.vscode`,
  `.idea`, `.cursor`, `.husky`, `.mcp.json`, `.mise.toml` or `.claude`, a `checkout`,
  `merge`, `stash pop` or `pull` that needs to change one prints `unable to unlink` and exits 0, with
  the file left as it was on the old branch, so it shows as modified on the new one. An agent's
  `git add -A && git commit` then reverts it there, with no error. The device law tells the agent.
- A plant made while no scan ran (the helper down) is baselined as the person's. After a helper killed
  outright, what the cut command planted stays in the folder until the frame's next command, sleep or
  end: only an orderly stop walks. A stop's walk gets 2 s of the budget, and one past that runs on
  until the helper exits or the supervisor kills it. What it found by then is saved for the agent.
- What a command left running can plant between walks. With no command running the watch walks every
  30 s (`ports.WALK_EVERY`), so that long. Under a running command nothing walks until it ends: a move
  there could fail a build on its own short-lived link. Containers are no process group, so they
  alone start no such walk.
- A folder the person closed before the grant (`chmod 000`) is never read, so nothing is known of
  what it holds. If they open it later, what it holds is new.
- A cross-disk (`/Volumes`) quarantine is a path-based copy and delete, a weaker no-follow guarantee,
  untested on a real external disk.

**The runtime and renditions.**
- Two system execs hold more than they need. `pipeline.run_suite` runs `suites/code.py`, and so the
  agent's `test.sh`, as a system exec, which can write that throwaway frame's `artifacts/`; nothing
  reads it back after the sha check, so there is no verdict gain. The harvest preview runner parses
  agent-written zips and XML with write access to the live Machine's `artifacts/`, manifest included,
  where cloud runs it as the agent. Now that agents read the runtime it could be an agent exec with
  `-I` and its output in `out/`, which is cloud's posture. Not urgent: the parsers are pure Python
  apart from lxml.
- A user toolchain's paths in a stored `actual` stay real; only frame roots and the runtime are mapped.
- QA precheck on the runtime still owes a device proof.

**Aliases.** Plain substitution reaches everything the command says, including what it writes.
- A file a command writes can hold the alias: heredocs, `echo`, a script recording `$PWD` after `cd`.
  So can a tool-written config: venv shebangs and `pyvenv.cfg`, `git clone`, `git worktree add`. In a
  grant that needs quoting these outlive the frame, and then point at nothing.
- A search pattern naming a real root is rewritten and silently finds nothing:
  `grep -rn '/Users/me/My App' .` searches for the alias. The same for `sed` and `rg`. Unlike a
  written config, this is a false negative.
- `cp -R /work existing-dest` copies contents, `basename /work` prints `.` and `dirname /work` prints
  the root itself. `rm -rf /work` fails loudly. Tar member names and `ln -s /work x` hold the alias.
- Brace expansion on a bare root (`/work{,.bak}`) gets a wrong second name.
- A folder whose name extends a root's by a space (`~/My App`, `~/My App 2`) cannot be told apart, and
  a command naming it fails loudly. If the home itself needs quoting there are no aliases, and such
  commands fail.

**The rest.**
- launchd. `launchctl` is refused as a binary, and a copy of it is killed by macOS. launchd itself
  refuses `submit`, `bootstrap`, `load`, `kill`, `kickstart`, `stop`, `bootout` and `remove` from
  inside the fence (`test_launchd_takes_no_job_from_the_fence`), but it did answer `disable`, and the
  profile has no rule for its protocol: a program that speaks it directly is not refused, and what
  launchd keeps for `disable` persists. The session's service list can be read that way. It is not
  otherwise secret.
- A Unix socket cannot be bound, even in the frame's own `tmp/`, so what needs one fails: tmux, a test
  Postgres. The Docker grant's socket is dialled, never bound.
- A sign-in on a private address is refused in a hand-off: a company's SSO reached over a VPN, which is
  part of why the pass exists. It lifts the list's refusal only, and the resolve-time refusal of a
  private address stands.
- What the model says can hold a real path, because it is told them. Measured on the real helper
  (2026-10-31): the `tool.called` rows and `artifact.snapshot` held none, while the model's reply
  (`step.narration`), its question (`gates`, `gate.opened`) and the conversation kept across a park
  (`plan_steps.resume_state`'s `history`, tool args and results whole) did. By reading: what is
  built from those (the thread a later turn reads, a delivered answer), a document it writes, and
  the durable engine's record of a step's answer. A shell summary folds this frame's roots only, so
  a path off the frame stays as the model typed it (`cat /Users/me/.ssh/id_ed25519`), as does a root
  escaped by more than a backslash before a space (decision 16); a `web_fetch` of a `file:` URL
  journals its path. Prismoid does not scrub the model's prose: it would rewrite what the person reads,
  and still miss a path the model reworded.
- A revoke reaches the next tool, not a command already running: a process keeps the Seatbelt
  profile it started under, and that covers what it left running. Sleep stops those. A revoked
  folder also stays busy (`folder_busy`) until its task settles, for this reason.
- With no extension in the Prismoid Chrome, a password manager's is not there at a hand-off's sign-in.
- Decision 19's cost. A script on an allowed site can leak what that page holds in an asset URL,
  and a script from any public host now runs on it. Ad and tracker scripts load. The agent has no
  JavaScript tool of its own, and what a page does there is what it would do in the person's own
  browser, but the list no longer says where a page's assets come from.
- An asset request carries the shared profile's cookies for its host (seen with `SameSite=None`). So
  a page in one task can make authenticated GETs to a service the person signed into through
  another task, or by hand, with that service off its list, and a script runs what comes back. It
  matches ordinary browsing, and the list does not stop it.
- A worker's script is held to the list (decision 19): a listed page's worker that takes
  `importScripts` from a CDN off the list loses it, and the refusal is a denial. Most sites' workers
  load from their own host. A module worker's imports pause as `Other`, no asset on Chrome 156, and
  were never excused.
- A page on the task's own loopback server is fenced to the list alone: a dev server's page that
  takes its stylesheet from a CDN loses it, and the refusal is a denial the agent can read. The list
  can name that host. That is the price of closing the road out of Seatbelt.
- A navigation that hangs (a server that never answers) keeps the page unknown while it hangs, so
  an off-list asset waits 2 s and is refused, a denial that reads like a list problem.
- A request whose body is past the pipe's limit is refused, on any host, listed or not: from 9 MiB
  of binary zeros to 27 MiB of text. A larger upload needs another road.
- A page can still crash the Prismoid Chrome, and every task's window with it, with a far larger body:
  Chrome 156 dies by SIGTRAP building an event of about 1 GiB, before a byte of it reaches the pipe
  (a `fetch` of a 130 MiB `ArrayBuffer` of zeros, 1 GB of event, is refused; 160 MiB kills it). `Fetch`
  offers no way to leave the body out, so nothing on this side of the pipe can stop that. The
  request never leaves, and the next dial relaunches Chrome, so it fails closed, and costs the
  tabs. `<input type=file>` uploads of files on disk were not tried.
- A half-closed client on a forwarded request, or on a tunnel, that waits past 30 s of the far side's
  silence is taken for gone. TCP gives no way to tell the two apart.
- A page's first asset that beats word of its commit and carries no `Referer` (`no-referrer`) is
  refused, which is a rare missing file on such a page.
- A path of the same class that the flags miss would pass with a live pass: the proxy can tell only
  that a tunnel is Chrome's https, not that the fence saw what rides it. Decision 19 lists what was
  probed on Chrome 156, and a new Chrome can add to it.
  `test_a_report_never_reaches_a_host_holding_a_pass` and its neighbour hold the known ones but
  NEL, which was never seen to send, even in a control with it on, so no test can see that flag
  (`test_nel_has_nothing_to_show_and_its_flag_stays` says so, and fails the day it sends).
- CI has no Mac, so nothing automatic runs the real Chrome, Seatbelt or stop tests, and Chrome
  updates itself. `just device-proof` runs the `test_device_chrome*` and `test_device_stop*` files by hand, a
  test that cannot run failing rather than skipping: run it before a release and on a new Chrome.
  Between runs a change in Chrome is unseen.
- The Prismoid Chrome has no `PaymentRequest` and no FedCM: a checkout falls back to its form, and a
  sign-in button built on FedCM to its popup, if the site has one.
- The two doors rest on Chrome's proxy rules: a WebSocket goes to the `socks` entry, and that entry
  may name an http proxy. A Chrome that changed that would send a WebSocket to the https door,
  where the ClientHello check still refuses a fresh tunnel but cannot see one on an open HTTP/2
  session. `test_a_websocket_cannot_ride_the_http2_session_an_asset_opened` is what would say so,
  when `just device-proof` runs it on that Chrome.
- An asset from a server that makes Chrome retry over HTTP/1.1 (`HTTP_1_1_REQUIRED`) does not load:
  the retry offers no `h2`, so the tunnel is refused, and the page misses that file.
- A person clicking in the Prismoid window while the agent acts is an accepted collision. Their tool is
  Pause.
- A coding task needs Chrome. A Mac without it fails a coding step with a sentence naming Chrome,
  because the flow O11 asks for needs a browser and a silent no-browser coder would claim less.
- Reset on a task with no folder grant loses its Prismoid-owned `work/`, which is what Reset means. A
  Reset waits for the helper, and a retrying one holds a machines-queue slot for up to about 3.7 h
  while the helper is away.

**The crash record.**
- A helper killed in the few milliseconds between a command's spawn and `groups.json` leaves a tree
  no later life can find, and the journal calls that command cut off while it runs.
- A `frame.json` is written whole. One torn all the same (a disk fault) is not a frame: its tree is
  stopped from `groups.json`, but the worker hears that nothing of the frame survives.
- `unseen`, and a note's going, rest on the helper seeing the worker's connection close. A worker that
  took the result and died before acting on it is not unseen, and its attempt is told nothing about
  that command or the notes it carried.
- A request too large to read whose id or session neither end holds (4 KiB each, past what Chrome 156
  writes) stays paused until its page goes, logged at ERROR. It is never excused, and no refusal goes
  to a session that is a guess.

## Consequences

- The cloud drivers are unchanged; tests hold their prompts byte-identical.
- Adding a tool's cache or a deny path is data, grown from live runs of the coding task.
- `prismoid up` needs no Docker. A Docker grant needs a running daemon.
- HANDOFF §3 names the runtime's packages, poppler and `uv`. Prismoid's own `pyproject` gains nothing
  from this ADR.
- The cloud topology, shared device tasks, per-task downloads and an engine-side `git diff` are
  later designs, each needing the owner's word.
