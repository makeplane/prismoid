# ADR-063: The Machine, a task's remembered computer

*Status: accepted, 2026-09-30 · Phase 1b · Amended 2026-10-26, recorded here 2026-11-18 (the row's driver, a native sleep, and a wake that destroys nothing, below) · Amended by ADR-078 (a Machine's sleep may be `none`, and a kept frame is destroyed only by Reset or the end of its task) · Amended 2026-11-06 (a step that needs no computer runs on paper, below)*

## Context

A frame lived for one attempt of one step. Everything a step built and did not publish was thrown
away between steps and turns, which is wrong for a team working one task for hours. The
screen (ADR-065) also needs a row to hang its control lease on. Other computer providers will follow,
so the design must fit behind `FrameDriver` with the sleep / wake / checkpoint vocabulary.

## Decision

- Every task that needs a frame gets one Machine, lazily; its serial steps and turns run in it.
  Seats (fleet seats, lane sections, scatter seats, anything dispatched with siblings),
  verification, QA and figures keep throwaway frames.
- The truth is a `machines` row (tenant root). `operations/machines/` owns the lifecycle; the
  driver is a dumb, persistence-free mechanism. A driver declares `sleep_kind` `archive` (Docker,
  sandbar) or `native` (a provider that keeps a sleeping computer's memory).
- The layering rule: operations get a sandbox only through `machines.acquire` or `new_frame`;
  `ai/` sees frames only; only `operations/machines/` calls lifecycle verbs on a Machine's frame.
- Sleep after 10 idle minutes (30 when parked on a gate); keep the latest `/work` checkpoint for
  14 days after last activity; the `/browser` archive is separate and encrypted with
  `operations/cipher`; a fork seeds from `/work` only. Checkpoints are cache (ADR-012): a lost
  one is a rebuild, never a failure.
- A retry runs in the same Machine and is shown what the failed attempt changed.
- Egress is per step under a monotonic version kept on the row.
- Warm time is metered per profile per minute (`Unit.SECOND`, family `machine`).
  A Machine's segments are timed by Postgres `now()` on its row; a throwaway frame has no row and
  is timed by the worker's monotonic clock, the truer measure of a life that short.
- An epoch-labelled reconciler replaces the boot reap-all; it destroys only what Postgres disowns.

Rejected: the lifecycle inside sandbar (the investment the handoff stops, and every provider would
need it again); one durable workflow per Machine (a parked DBOS workflow holds a queue slot).

## Consequences

- The Docker driver is single-host: a Machine's frame on one host cannot be reached by a worker
  on another. Production uses sandbar, which any worker reaches over HTTP; a multi-host deployment
  on the Docker driver is unsupported.
- A step whose Machine cannot be had (the org's warm cap, a Machine that fails at once) retries as
  an infra rung every `wait_s`, uncapped, and holds its step-queue slot while it
  waits, until the DART port: one org at its warm cap can hold every slot of the shared queue.
  *Amended 2026-11-11:* the infra cap (`plan_steps.infra_failures` under
  `PRISMOID_WORKER_MAX_INFRA_ATTEMPTS`, ADR-027 section 4) covers provider and tool-transport deaths,
  not this wait, so it is not the cap this bullet asked for. The risk is accepted for the
  single-box self-host stack, which ships with the lever on: its one org holds the one queue, so
  a warm-cap wait can stall only its own steps. A multi-org deployment should keep the lever off
  until that cap or the port lands.
- Warm time is billed while the frame exists, whether or not a sleep succeeded: a failed sleep
  bills its span at the next sleep or Reset (the frame was using resources), and so does a dead
  sandbox on sandbar until an editor resets it (accepted for the single-box self-host stack,
  amended 2026-11-11; a multi-org deployment should keep the lever off until an editor reset or
  a cap closes it).
- `COMPUTER_MACHINES_ENABLED=false` rolls back to a throwaway frame per step.
- GDPR erasure, when it lands, must call `machines.forget` for the person's tasks.

## Operating it (amended 2026-10-01, at landing; 2026-11-11)

- **The self-host stack ships with the lever on** (amended 2026-11-11: `PRISMOID_MACHINES_ENABLED:
  "true"` in `deploy/compose.prod.yaml`, with the browser image and the screen keys wired and
  required there). It shipped off at landing; sandbar's four gates were met on 2026-10-06 and are
  closed in code
  (the fourth amended 2026-10-06: the step boundary takes the last step's mounts away on sandbar
  too - `POST /v1/sandboxes/{id}/remove` removes paths below `/task` as root, their owner, with one
  fixed script whose arguments are the paths, refusing anything else; `/task` and `/task/inputs`
  stay 0555 and root's - so `reset_mounts` matches Docker's and no name stays in the agent's view;
  there is still no root exec). Sandbar's three gaps that kept it off are closed in code
  (amended 2026-10-02, with a real run as their
  proof): a sandbox with domains gets an internal network of its own (amended 2026-10-06:
  every org's sandboxes once shared one, and one reached another's server live) - a /28 of
  `SANDBAR_DOCKER_EGRESS_POOL` that sandbard creates, checks and removes with it - and reaches the
  proxy only through `sandbar-egress` (`sandbard relay`), which sandbard joins to that network, so
  a process that ignores `HTTP_PROXY` reaches nothing and no sandbox reaches another; each such
  network is created with `com.docker.network.bridge.inhibit_ipv4=true`, so the host has no
  address on it (found by a probe during the build: an internal
  bridge's gateway otherwise reaches host services), a sandbox with domains has IPv6 off, and
  sandbard refuses a network that is gone, not internal, or has a gateway, at create and at boot
  (S2); each record reports `state`
  (`running|stopped|missing|unknown`): a sandbox stopped under a live sandbard still holds its
  files, so `bind` starts it again (`POST /v1/sandboxes/{id}/start`) and the reconciler judges it
  as a running one, a missing one is gone, and an unreadable state costs one infra attempt, never
  a rebuild; and the egress 409 carries the held version as JSON (`error.version`; the driver tolerates the
  old text for one release). Still open:
  - the relay's pipes are shared by every sandbox: 4096, one sandbox bounded at 64 (the per-peer
    cap, `SANDBAR_RELAY_MAX_PER_PEER`, keyed by its address; amended 2026-10-06, was 256 of 1024,
    which four sandboxes filled), and a CONNECT tunnel that carries no byte either way for 5
    minutes is closed by the proxy, so a parked tunnel gives its pipe back. Accepted as designed:
    64 sandboxes each keeping 64 tunnels busy can still fill it - past the relay's own sockets
    that is the host's limit, and the relay knows no org to share by;
  - behind the relay the proxy sees one source address, the relay's: the per-sandbox token is
    what names a sandbox, so a backend must never register an IP principal (it would name every
    sandbox at once);
  - every list probes each sandbox's state, eight at a time with 2 s each, so on a hung Engine a
    list costs ceil(N/8)*2 s and passes the driver's 30 s control timeout past ~120 sandboxes;
  - the worker's dev `computer_frame_net` (backend/deploy/compose.yaml) is internal without
    inhibit_ipv4, so its gateway still exposes host-published dev services to dev frames.
- **The Docker Engine floor.** A sandbox's network needs an engine whose `--internal` +
  inhibit_ipv4 networks report no gateway (verified on OrbStack's Engine 29.4). Below it an
  egress-on sandbard refuses to boot (it proves the floor on a throwaway network from its pool),
  and the worker, which waits for a healthy
  sandbard, never starts: every task stops, not only frames. `deploy/deploy.sh` proves the floor
  on a throwaway network before it pins or stops anything and refuses the deploy otherwise
  (`deploy/DEPLOY.md`, "Upgrading to the S2 frame network").
- **The reconciler judges every row against one provider listing.** It is correct for sandbar and
  single-host Docker; multi-host Docker needs a host column on the row first.
- **Never change `COMPUTER_WORKER_INSTANCE` or `frame_driver` while Machines are warm.** Pull the
  lever and let them sleep first: otherwise the reconciler sees their frames as missing and the
  unsaved work since their last checkpoint is lost.
- **A sandbard restart keeps every Docker sandbox** (amended 2026-10-06, C13-14: it forgot them,
  and its exit removed them all, so every deploy cost each warm Machine its work since the last
  checkpoint): containers and networks carry their sandbox's id, a boot adopts them before the API
  answers (the record and spec, the network checked again and the relay joined again, the
  credential with an empty allowlist until the worker's next versioned push), an exit keeps them,
  and a network left with no sandbox is removed. A recreated relay has left every network: compose
  restarts sandbard with it, and the boot joins it again. Firecracker is not a shipping backend;
  its microVMs are still torn down at exit.
- **On AWS S3, grant `s3:ListBucket` on the snapshots bucket**, or a missing key answers 403
  instead of 404 and a lost checkpoint reads as an outage.

## Amendment 2026-10-26 (recorded here 2026-11-18) - the row's driver, a native sleep, and a wake that destroys nothing

What a Machine asks of every driver behind `FrameDriver`, whichever provider runs it (decided
2026-10-14 to 2026-10-26, with the first driver that keeps a sleeping computer's memory):

1. **The row's driver.** The Machine row's `driver` picks the driver of an existing Machine;
   `PRISMOID_WORKER_FRAME_DRIVER` picks only that of a new one. The screen's door, the meter and the
   next bind follow the row's driver, never this process's setting, and a frame ref on two drivers is
   two frames. A Machine whose driver is not the task's (`driver_of`: its own, else the configured
   one) is rebuilt on the task's at its next wake.
2. **A native sleep.** A `native` driver (`NativeSleep`: `suspend`, `resume`) stores `/work` first,
   then suspends the frame with its memory, its processes and its open pages: the asleep row keeps
   naming it, and the next wake resumes it when nothing about it must change (the same profile, the
   same driver), else rebuilds from the checkpoint and destroys the old computer once the row names
   the new frame. Over the checkpoint cap (nothing stored) it sleeps the archive way. A natively
   asleep computer keeps its first epoch's labels, so the reconciler counts a frame as the row's when
   the row names it on the row's driver, warm or asleep.
3. **A wake destroys nothing its provider has not said is gone.** Only a gone answer (a
   `RuntimeError` from `resume` or `bind`) takes the rebuild from the checkpoint. A wake the provider
   does not answer (`FrameUnreachable`: an unreachable API, a provider restarting) is asked again
   with backoff (1 s doubling to 15 s) inside `PRISMOID_MACHINES_WAKE_RETRY_S` (default 180 s), each
   wake cut at what is left; the step's progress row says once that the Machine is taking longer to
   wake. When the budget runs out, and on any other failure (a refusal, a cancellation, the phase
   publisher), the attempt fails as infra with its reason ("... - the Machine is still asleep, and
   the next attempt wakes it") and the row goes back exactly as the claim found it: asleep, naming
   its computer, its memory, logins and unsaved `/work` kept. A wake that replaces a native computer
   without asking its provider (another profile, another driver) and fails before its new frame is
   named puts the row back naming the old computer with the profile it was made with, so the
   reconciler never reads it as disowned. Archive drivers are unchanged: an asleep row names no
   frame, and a failed wake destroys only the frame it was making. Why 180 s: restored-guest wakes
   took 9-15 s under nested virtualization, and an API or host-service restart answers within a
   couple of minutes; three minutes covers both more than ten times over, stays under `stuck_s` and
   `wait_s`, and bounds how long a person waits for a reason.
4. **A browser the worker drives.** A frame with no browser sidecar of its own has its browser verbs
   run in the worker (`RemoteBrowser`: `BrowserVerbs` and the `Recorder`) over a CDP connection the
   driver dials (`WsCdp`, on `websockets`: HANDOFF §3), one session a step: opened on its first
   browser verb, closed when the step releases the Machine (`FrameDriver.close`). The step's
   recording goes into the frame through its own file door (`FrameSink`), every file the system's,
   so the agent reads it and never rewrites it. The device's Prismoid Chrome is driven this way
   (ADR-078).

Residual: a wake cut at the budget (or by a cancellation) that the provider finishes anyway leaves
the computer running while the row says asleep, unmetered until the next wake binds it or retention
expires it.

## Amendment 2026-11-06 - a step that needs no computer runs on paper

The owner's ruling of 2026-11-06: every serial step took the task's Machine, so a memo
held a host slot for minutes while its writer only read its inputs and handed back text.
The Machine was never where a deliverable lives: harvest stores the published bytes in the object
store, and the frame only staged them.

1. **Paper.** A serial step whose agent needs no computer (`machines.agent_needs_computer`, read
   from its tools and image: ADR-070's amendment) runs on a paper frame (`ai/frames/paper.py`):
   the step's mounted files, its deliverable and its manifest in the worker's memory for the length
   of the step. It satisfies `FrameDriver`, so the tools, the system publish and harvest are
   unchanged, and what it does not have it answers honestly: `exec` returns exit 126 ("no
   shell"), `browser` its own refusal, no egress path (`web.*` and connectors are the worker's
   doors already). Today that is the writer, the organizer and the operator. An agent that writes
   a file of its own (`frame.write_file`) keeps the Machine, however small the file: any step can
   park (a question, a connector approval, a pause), a resume gets a fresh paper frame, and a
   draft in `/work` must survive the park. So the researcher runs on the Machine;
   paper holding `/work` across a park would be its own amendment, with its own storage.
2. **The one door holds.** A file becomes an artifact only through `publish` into `/artifacts`
   with a manifest entry, after the same `validate_publish` every driver runs; the target and the
   entry come from `ai/frames/product.py`, the fields the in-frame runner writes. Harvest reads the
   manifest and stores the bytes as before. Previews are skipped: paper holds markdown and JSON,
   which no renderer reads.
3. **Who keeps the Machine.** A seat keeps its throwaway frame; a task on the device keeps its
   Machine (it holds no host slot); a step whose mounts pass half of `PRISMOID_MACHINES_PAPER_MAX_BYTES`
   (64 MiB) keeps the Machine, sized from the same list `equip` mounts (`mounts.mounted_artifacts`:
   upstream, superseded, carried, the thread's earlier deliverables, a fork's inherited ones); a
   write past the cap fails that rung as infra, and every later rung of the step takes the Machine
   (`Ladder.outgrew_paper`), so an overflow costs one rung, never a loop; a resume its Machine
   still holds binds the Machine. Any other resume gets a fresh paper frame:
   its inputs are mounted again, as on every frame, and a paper agent never wrote a file of its own.
4. **Nothing is billed**: no computer ran (`release` returns no segment for paper). `task.planned`'s
   `machine: files` now means the turn runs with no Machine on a cloud driver.
5. `PRISMOID_MACHINES_PAPER=false` puts every serial step back on the Machine.

Consequence: a later step of the same turn that needs the computer starts the Machine then, with no
`/work` from the paper steps; it reads their output as their artifacts, mounted like any upstream.

## Amendment 2026-11-15 - the Machine starts on the first tool that needs it

Every serial step on the base image starts on paper, whatever its agent holds; the agent's grant no
longer decides. The first call that changes the Machine itself (`SideEffect.LOCAL`: a shell, an
image rendered into `/work`; `machine_acquire.needs_machine`) acquires it through the run's port
(`TaskDeps.machine`, `capabilities/machine_acquire.py`) on the step's own idle session, carries the
paper's files (the system's read-only) and manifest entries onto it, and every later call lands
there; the attempt then releases it as any Machine. A file the model writes stays on paper, and a
park holding one moves the step to the Machine first, so the resume binds it (a step the Machine
holds always goes on there). An agent on another image, a seat and a task on the device take their
frame up front, as before. A step already holding a Machine keeps it. `PRISMOID_MACHINES_ENABLED` and
`PRISMOID_MACHINES_PAPER` keep their meaning.
