# ADR-087 · A default task cap, and no work for a creator who is gone

**Status:** accepted · 2026-11-15.

## Context

A task created without a dollar cap spends on the deployment's model keys until it ends: nothing
stops one runaway task short of a person cancelling it. An operator who opens a deployment to many
people needs a cap that applies when the person set none, without taking the person's own choice
away.

Suspending a user and removing a member stop their tasks at the door, but the worker rebuilds every
step and turn from the task row. Work queued or parked before the brake, or minted between a cancel
and the membership removal, could still resume and spend for a person who is no longer there.

## Decision

### 1. A default cap for a task created without one

`PRISMOID_TASK_DEFAULT_BUDGET_USD` (`settings.workspaces.task_default_budget_usd`, owner decision D3)
gives a task created with no dollar cap that cap, so one task cannot spend without limit. A deployment
sets it (for example to 2); the person can raise it on the task (`PUT /v1/tasks/{id}/budget`); left
unset, the default, a task stays uncapped as before. `create_task` applies it through
`task_door.with_default_cap` when `budget_of(budget)` is None, storing `{"usd": "2.00", "source":
"default"}`: the shape the budget endpoint writes, marked as the deployment's (`usd_ceiling` reads only
`usd`). An explicit cap wins; routines, chat and channels pass their own caps; a follow-up keeps the
task's budget.

Nobody chose a defaulted cap, so it is never consent to a cost. The long-form cost confirmation
(`confirmation_due`) treats only a cap a person set as consent: a defaulted one still asks. Accepting
the confirmation consents to its estimate, so a defaulted cap rises to the estimate's high end
(`gate_budget.consent_to_estimate`, recorded as `task.budget_set` by the person who accepted), and the
document is not stopped again by "Budget reached", at a wave boundary or mid-section. A cap a person sets
(`PUT /v1/tasks/{id}/budget`) drops the marker: from then on it is theirs.

### 2. The worker refuses work whose creator is gone

Suspending a user and removing a member stop their tasks at the door (a suspension cancels the user's
tasks; a departure cancels the member's tasks in that workspace, and the tasks there whose current turn
is theirs). The worker still rebuilds every step
and turn from the task row, so work queued or parked before the brake, or minted between a cancel and
the membership removal, could resume. `operations/standing` (a leaf; `Standing` is
`ACTIVE | SUSPENDED | LEFT` in `schemas/standing.py`) answers two questions about two people:

- `turn_standing`: the person the CURRENT turn spends for, `task.turn_actor_id`, the creator when
  unset. SUSPENDED when their actor is missing or not active, LEFT when they hold no membership in the
  task's workspace. A project teammate's follow-up on a task whose creator left therefore runs
  (ADR-062; spend follows the turn's actor). Asked at a step's `triage` (before any rung is bought, once
  a replay of the step's workflow has caught up: the cancel's wakes are sends, and a send at a replayed
  position takes the function id its journal holds for a recv or sleep) and at the turn's start.
- `creator_standing`: the creator alone, asked once in `equip.mount_connectors`, which mounts nothing
  (task-level attaches and a bot's declared slugs alike) for a creator who is not ACTIVE, and journals
  each as a dropped connector (`tool.called` ok=false, naming why): a connector mounts the creator's
  credential whoever's turn it is.

The turn's start rides `mark_dequeued`, which now returns a verdict (True: refused; a gate id: held on
a spent allowance's gate, ADR-103) and refuses before it boots a Machine or runs intake, so a gone
person's fresh turn makes no model call, namer and clarifier included, and opens no gate to knock their
inbox. An intake park's resume is re-checked in `apply_clarification`, which already returns
`"cancelled"`. No effect was added, removed or reordered: `run_task`'s journal is unchanged, and a
journal that recorded `None` for `mark_dequeued` carries on.

A refusal cancels through `steering.cancel_task_row(..., attribute=False)`: the events carry no actor,
because the account's state decided, not a person. The record already names who suspended or removed
them.

## Alternatives rejected

- **A defaulted cap as consent.** The long-form confirmation would pass on a number nobody chose;
  only a cap a person sets is consent (section 1).
- **Stop a gone person's work only at the door.** The worker rebuilds every step and turn from the
  task row, so work queued or parked before the brake would resume (section 2).

## Consequences

- With `PRISMOID_TASK_DEFAULT_BUDGET_USD` unset nothing changes: a task created without a cap stays
  uncapped.
- A task under a defaulted cap still asks for the long-form cost confirmation, and accepting it raises
  the cap to the estimate's high end.
- A suspended or departed person's queued, parked or fresh turn makes no model call and opens no
  gate; a teammate's follow-up on a task whose creator left still runs.
- A creator who is not active mounts no connector on any turn of their tasks, and each is journaled
  as a dropped connector.
