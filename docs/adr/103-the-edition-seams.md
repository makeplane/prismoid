# ADR-103 · The edition seams: where a distribution adds its own features

**Status:** accepted · 2026-11-18. **Amends** ADR-070: only a driver in `frames.EARLY_BOOT_DRIVERS`
boots early.

## Context

This repository is the open-source edition of Prismoid, under `AGPL-3.0-only`. A distribution builds
on it from its own repository and adds features of its own. A feature only a distribution runs does
not belong inline in the core: every self-hoster would carry code they never run. But such a feature
reaches into core code (a door of new spend, a Machine driver, a screen door), so it needs a named
place to plug in, with a default that does the right thing here.

## Decision

1. **One direction.** Features land here first. A distribution keeps its own features in its own
   repository, adds them through the seams below and merges this repository's `main`; core code
   names no distribution's feature. A core change a distribution makes first comes back here as a
   port.
2. **The seams**, each with this edition's default:
   - `operations/allowance.py`, a workspace's spend allowance: `left` (None: no allowance applies),
     `exhausted`, `require` (the door of new spend; raises `AllowanceSpent`, a 402
     `allowance_spent`), `wake_parks` (the gate sweep's arm), `address_proved` (a proven address,
     inside the transaction that records it), and the park gate's words. Every door of new spend,
     the Bursar's floor, the usage brake, routine fires, channels and the gate sweep ask it, and the
     Bursar's `workspace` budget scope parks on it. The park's lock and queries are
     `Gate.lock_allowance_parks`, `open_allowance_parks` and `workspaces_with_allowance_parks`.
   - `ai/frames`: a driver is one row of `DRIVERS`; `EARLY_BOOT_DRIVERS` (empty) names the drivers
     whose first Machine boots when the turn is picked up (ADR-070); `CLIENT_MODULES` (empty) names
     the driver modules whose per-loop client `close_clients` closes. `ai/frames/screen.DOORS` holds
     one screen door per driver; a door may serve a control stream and a hand-back (ADR-065).
   - `schemas/machines.FrameDriverName` lists this edition's drivers (`docker`, `sandbar`,
     `device`); a distribution adds its own.
3. **The generic machinery stays.** What the seams feed is core and works with this edition's
   defaults: the allowance park and its gate; the early boot (ADR-070), gated on
   `EARLY_BOOT_DRIVERS`; the screen's control stream (VNC) for the lease holder, the web's VNC pane
   and the relay's cursor messages (ADR-065); wake retries on `FrameUnreachable`, `HostFull` and
   native sleep (`sleep_kind`, ADR-063 and ADR-076); and email verification by an emailed `ev_`
   code, which proves an address, so that only a proven address sends invites (ADR-086).
4. **The data stays compatible.** A revision id that has shipped never changes, because revision
   ids are stored in every database. A distribution creates its own tables in its own revisions,
   adopting tables a database already has. Stored words stay stable.

## Alternatives rejected

- **A distribution's features behind flags in this tree.** Every self-hoster would carry clients,
  products and third-party services they never run, behind switches nobody here exercises.
- **Remove the generic machinery with the features.** The park, the control stream and the early
  boot sit in the busiest files (the gates, the tasks, the screen relay); removing them would make
  every merge into a distribution conflict there.
- **A plugin system with entry points.** More machinery than a handful of seams need; a plain module
  with defaults is read in one place and checked by the type checker.

## Consequences

- A self-hosted deployment runs every seam at its default: no allowance (every door opens, nothing
  parks, nothing wakes), no driver boots early, and the drivers listed here.
- A distribution's feature that reaches into core code needs a seam here first: the seam lands here
  with an open-source default, then the distribution fills it.
- Code, docs and records in this repository name no distribution's feature.
