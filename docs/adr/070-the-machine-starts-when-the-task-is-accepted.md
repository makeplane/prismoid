# ADR-070 · The Machine starts when the task is accepted

**Status:** accepted · 2026-10-29 · the owner's ask of 2026-10-29 ("when a task requires a machine
it's taking a lot of time for initial bootup ... start the machine when the task is accepted").
**Amends** ADR-063's first need (the task's first step creates its Machine) on
a driver whose every profile is one computer template only. **Amended 2026-11-06** (below): the boot
waits until the turn holds computer work. **Amended by ADR-103** (2026-11-18): only a driver named in
`frames.EARLY_BOOT_DRIVERS` boots early.

## Context

On a driver whose computers start from a snapshot, a task's first Machine is a create: a snapshot
restore under nested virtualization, then the guest's ready check, which takes tens of seconds
(20-57 s to `ready` was measured in a nested Lima VM). The first step starts it, so the person waits
for the intake round, then the planner, then the boot, one after the other. A provider's warm pool
would make the create itself fast, but none is built.

## Decision

1. **The pick-up boots the Machine.** `mark_dequeued`, the effect that records a turn was picked up,
   enqueues `machine_boot` (`machine-boot-{task}`, on `machine_queue`) after its commit, when the
   turn is turn 1 and not express (the express rung is frameless, ADR-049 §3). The boot runs under
   the intake round and the planner. It rides that existing effect rather than adding one, so
   `run_task`'s journal is unchanged and in-flight turns recover (ADR-027 §1). Best-effort: a boot
   that cannot be enqueued is logged, and the first step starts the Machine as before.
2. **Only on a driver whose every profile is one computer template** (`machines.boots_early`), so
   the boot cannot guess a frame the first step must re-make. Docker's profiles are different
   images, and a sandbar sandbox born with no list is airtight for good: a wrong guess there costs a
   re-create, more than it saves.
   `PRISMOID_MACHINES_BOOT_ON_ACCEPT=false` turns it off.
3. **The boot's frame.** The profile is `browser` when computer use is on (the widest profile, which
   binds every step: `widen`), else `base`. The frame is born with no list, because an idle Machine
   reaches nothing; the first step's boundary pushes its own, as a bind of any idle Machine does.
   The row is born `waking` under its own hold, `boot:<task>`, and the boot leaves it `idle`,
   holding nothing, metered from then on (§7.6).
4. **It never takes a Machine from anyone.** A task that already has one, a full workspace (the boot
   never evicts for a guess, unlike a step's claim), or an ended task is left alone. A boot that
   fails gives the row back `gone`, holding nothing, and the first step starts it (`_give_up`).
5. **A step that meets the boot.** If the row is `waking` under a boot, the step waits ("the Machine
   is starting"), asking every second rather than backing off, and says "Starting the Machine".
   A boot's hold on a resting row is a boot that died (the stuck rule kept its hold, as it keeps a
   step's): the step takes it over and wakes it at the next epoch, instead of waiting out the
   dead-hold rule.

## Consequences

- The person's wait for a first Machine overlaps the intake round and the planner, rather than
  following them. A boot that outlasts both still saves the time they took.
- A task is billed for its Machine from the boot, not from its first step: the boot's minutes run at
  the `browser` rate when computer use is on. An unclaimed boot (a clarifying question nobody
  answers, a plan with only seats) is an idle Machine. The sweep sleeps it after `idle_s`, like any
  other, and it costs a slot in the workspace's warm cap meanwhile.
- A turn the express door sends to the chat tier and that later escalates to a plan gets no early
  boot: its first step starts the Machine, as before.
- Recovery: a re-run of the boot (DBOS recovery) finds its own `waking` row and creates again under
  the same frame name, which the provider's create is keyed by. A worker that dies mid-boot leaves the
  row `waking`. The stuck rule puts it to rest after `stuck_s`, as it would a step's, and a step
  waiting on it meanwhile stays inside `wait_s` (MachineSettings' own check).
- Follow-up turns are unchanged. A Machine that slept between turns still wakes at the turn's
  first step.

## Amendment 2026-11-06 - the boot waits for computer work

The owner's ask of 2026-11-06: several prompts started a Machine that nothing in the task needed. On
such a driver every booted Machine holds a host slot, against the target of 30 parallel sessions per
host. The pick-up booted every turn-1 task the express door did not claim, before the talk router and
the planner had spoken: a turn One answered with "no work", a clarifying question nobody answered, a
plan of seats, and a plan of text steps each started a `browser` Machine.

1. **What a step needs is read from its agent's tools** (`schemas/machine_need.py`,
   `operations/machines/need.py`), never from a list of agent names. A step that takes the task's
   Machine (a serial agent step: not the express answer, not a seat) needs the computer when its
   agent holds a tool that runs in the frame (a shell, the browser, a file write, a publish the
   model calls, an image rendered into `/work`, or any tool the frameless table does not name) or
   boots an image other than `base`. Reading what the system mounted, the web door, the connector
   door and the task's own verbs do not. A bot the registry does not know needs it, as before.
2. **The pick-up boots only computer work it can name without a model** (`early_boot.at_pickup`):
   the user's deliverable dial (`dial_lane`) or the computer door (`navigate_lane`, unless the
   action door claims the ask), and only when that lane's step needs the computer. These asks keep
   the whole overlap with the intake round and the planner.
3. **The plan boots the rest** (`early_boot.for_plan`), after the plan commits: the mint
   (`plan_task`) and the answerer's hatch (the express step's completion), when the task has no
   Machine and an unstarted step needs the computer. The boot's id is per turn
   (`machine-boot-{task}`, then `-t{n}`), so the plan's boot dedupes against the pick-up's, and a
   later turn whose task never had a Machine gets one. A boot here overlaps only the step's own
   dispatch (queue hop 2), so a planner-routed computer task waits for its boot after the planner,
   as it did before this ADR: the intake round and the planner (typically 10-30 s) no longer hide
   it.
4. **The record says it.** `task.planned` carries `machine` (`none`: no step takes the task's
   Machine, the turn runs without one; `files`: its steps take it only for their files, so nothing
   starts ahead; `computer`: a step's work needs it, so it starts ahead) and `machine_why`, the agents
   that decided it - agent names, never the prompt.
5. `PRISMOID_MACHINES_BOOT_ON_ACCEPT=false` (and `PRISMOID_MACHINES_ENABLED=false`) still stop every boot.
6. **The boot never actually ran before this amendment.** `mark_dequeued` is an effect, and the
   engine (DBOS) refuses to start a workflow from inside a step: the enqueue raised, the
   best-effort `except` logged "the early boot was not enqueued", and every first step started its
   Machine cold. Found in the 2026-11-06 live run. `durable.start` now starts
   a job from inside an effect as its own root workflow, under the same deterministic id (a re-run
   effect starts the same job). Measured live after the fix: the Machine is created about 1 s
   after the pick-up, before the plan, and the first step binds it warm (epoch 1, no wake).

A `files` turn runs its steps on paper and makes no Machine (ADR-063, amended the same day); on the
device, where a Machine holds no slot, its first step still starts one.

## Amendment 2026-11-15 - a prewarm on a committed plan, never a guess at the pick-up

A step takes its Machine on its first tool that needs the computer (ADR-063, amended the same day),
so the boot ahead of it is a prewarm: `early_boot.for_plan` alone, after a plan commits with an
unstarted step that may need the computer (its agent holds such a tool or boots another image).
The pick-up's boot (`at_pickup`, the dial and computer-door guess) is deleted: nothing starts before
a plan is committed. Nobody waits on a prewarm; the step binds it warm or starts the Machine itself.
`PRISMOID_MACHINES_BOOT_ON_ACCEPT` keeps its meaning.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** A Machine boots after a recipe's plan or a Lead's plan commits; the answerer's hatch is deleted.
