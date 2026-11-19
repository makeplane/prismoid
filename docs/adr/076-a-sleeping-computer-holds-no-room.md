# ADR-076 · A sleeping computer holds no room

**Status:** accepted · 2026-11-01 · found in the beta's logged-in check of 2026-10-31. **Amends**
ADR-063 (retention, and how long a natively asleep Machine keeps its computer), ADR-070 (an unclaimed
boot) and ADR-019 (a receipt that cannot be signed).

## Context

A natively asleep Machine keeps its computer for 14 days, and its provider counted every computer
that was not deleted against a host's capacity. Eight tasks filled the dev host for good: the next
create was refused for room, the step waited on "waiting for signal...", and the receipt's own frame
failed the same way. `_sign_receipt` swallowed that, so a delivered task said Pending for
ever. ADR-070 made it worse: a boot no step used slept, was billed, and held a slot the same way.

## Decision

1. **A wake needs room.** A provider that counts awake computers only answers a wake on a full
   host with a refusal, and the computer stays asleep. Prismoid reads that as the wait it already
   has for a provider that does not answer: `resume` raises `FrameUnreachable`, asked again inside
   `wake_retry_s`, then one infra attempt.
2. **A full host is a named refusal.** A driver raises a host's refusal for room as `HostFull`, a
   `PolicyDenied`, so every caller behaves as before unless it can wait.
3. **A receipt waits, then says so.** The `verify_artifact` job waits up to
   `PRISMOID_MACHINES_VERIFY_WAIT_S` (900 s, past `idle_s` plus a sweep, so an idle computer has slept)
   for its frame, then signs with one structural SKIP, `suite:frame`, which is UNVERIFIED with the
   reason. No judge reads it: `final_verdict` lets a judge's PASS stand in for a suite with nothing
   to examine, and this suite never looked. The in-line fleet passes no wait and raises as it did. A Pending now means a receipt is
   coming or the job hit some other failure, which stays ADR-019's rule.
4. **The asleep cap.** A workspace keeps `PRISMOID_MACHINES_WORKSPACE_ASLEEP_CAP` (4) natively asleep
   computers. The sweep's `RELEASE` arm destroys the oldest past it, by `last_active_at`, and the
   row stays `asleep` on its checkpoint with no `frame_ref`, so the next wake rebuilds: slower, and
   open pages and processes are gone, while `/work` and the browser's saved logins come back. Only
   a row with a checkpoint is released. A rebuild waits for room inside `wake_retry_s` as a wake does,
   so a released computer and a step's first start hold a follow-up no worse than a sleeper does;
   the early boot does not wait. Per workspace, as `workspace_warm_cap` is, since a Machine
   carries a workspace and no user. This also bounds the host's NVMe use, which the host's count of
   computers no longer does.
5. **A boot no step used is not a Machine.** A first-epoch Machine with nothing stored, on a task
   none of whose steps has taken an attempt and whose browser nobody has driven, goes `gone` instead
   of to sleep: destroyed and unbilled. This covers the clarify nobody answered and the task cancelled before its first step. A plan of
   seats only counts as started (they take attempts in their own frames), so its boot sleeps as
   before: the rule keeps to what the row can prove.

## Consequences

- An idle computer past the cap wakes in a rebuild's time and loses its open pages. 4 is a guess
  for the beta, to be read against real use.
- A wake or a rebuild can now wait on room. A host full of awake computers holds a follow-up for up
  to `wake_retry_s`, then fails one infra attempt with the computer still asleep or on its checkpoint.
- A receipt can hold a `verification` queue slot for up to 900 s. Only on a full host, where the
  slot could not run anything else.
