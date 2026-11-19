# ADR-062: The Space is where people work together

*Status: accepted · 2026-09-30 · amends ADR-061's Phase 1 note and the program spec's session*

## Context

The Percy program spec planned a session: a shared Conversation, with a roster, invites and a
Machine, holding tasks as cards. Designing Phase 1a showed two things:
- a private room needs a visibility fence across every Space listing;
- a many-human conversation needs surgery throughout the one-owner chat stack.

Meanwhile the existing model already has the right sharing shape: a task in a Space is visible
to every member of that Space. That is how a Plane project works.

## Decision

| Question | Decision |
|---|---|
| Unit of collaboration | The **Space**. Everyone in it sees every task in it. A smaller group is a smaller Space. Spaces stay flat, and no task is private inside a shared Space. |
| Live multiplayer unit | The **Task**: its thread, stream, steering, gates and presence. |
| Session, room, channel | None. No table, route or UI string uses those words for this concept ("session" collides with `AsyncSession` and `AuthSession`, and "channel" with ADR-056 bot channels and the Lane-2 frame field). |
| Machine (Phase 1b) | One per task. It persists across turns, and a fork clones it. |
| Roles | The Space's, unchanged. Viewers watch. Editors follow up, steer, pause, resume, cancel, answer, replan and fork. The owner manages members, invite links, budget and settings. Connector attach and connector-write approvals stay with the creator. |
| Joining | Org members only: the owner's add by email, or an owner-minted `si_` link (hashed, TTL'd, single-use: the join that grants membership spends it; amended 2026-10-06, was use-capped - see Consequences). |
| Attribution | Every human action writes its actor. Each thread turn has an author. `usage_events.actor_id` labels spend by the turn's author (the author of the turn open at the time of spend; 1c's plan epochs refine it), reporting only; spend still totals per org. |
| Private memory | Fenced. A task in a Space never reads or writes a user lane. Private-bearing event fields are shown to the creator only. |
| Presence | `presence.join`, `presence.leave`, `presence.here` and `presence.typing` on the task stream. Ephemeral, spread by gossip between stream handlers, expired by a client TTL. Never persisted. |

## Consequences

- **The streaming rule gains one exemption.** Presence is state, not content, so it never ends
  in a durable snapshot and never carries a Lane-1 `id:`.
- **Bots as Space members** are their own cycle after Phase 1c.
- **Bot channels** are untouched; retiring them is a separate decision.
- **Valkey stays deferred.** Presence NOTIFY volume is the measured trigger ADR-061 names.
- **The fence has one home** (`operations/context.Fence`, 2026-10-04): every door that reads or
  writes private context asks it. That includes the conversation a chat-born task came from: its
  thread's turn 0 is read through the fence of the task being rendered (a fork's, never the
  root's), so work in a Space reads only a conversation of that Space, and its report home never
  leaves over a personal conversation's channel. **A write is narrower than a read**
  (`Fence.writes_to`, 2026-10-06): work writes only into its own home, so a Space task writes
  nothing into anyone's personal conversation (no report, no gate row, no continuation from its
  card there; its turns stay on its own thread), and a task taken home writes nothing into its
  Space's conversation.
- **A teammate's word borrows the creator's connectors for the rest of the task** (owner ruling,
  2026-10-06, reversing review item C9-17 that had been accepted as designed). Connector tools run
  on the creator's credential, so a teammate's turn, and since 2026-10-01 a teammate's steer or
  answer in her turn, put every mounted tool on ask (approval and its knock are hers alone). That
  borrow used to end with the turn, yet the teammate's words stay in the thread every later turn
  reads; now once anyone other than the creator has taken a turn, steered or answered in a task,
  every later turn of it asks her for each call, reads and owner overrides included (`off` still
  wins). System and clock actions never count. The same holds through forks (same day): a fork
  carries its ancestors' conversation up to each fork point, so a teammate's word there - a
  turn, a steer, an answer, or the parent task itself when someone else created it - borrows the
  fork from its first turn, while words added to the parent after the fork never do. The
  archivists ask the same question. Spec §14.1 records the mechanism.
- **A join link lets one person in** (owner ruling, 2026-10-06: "Once a user accepts the invite,
  the invite link should expire. Invites are always time bound and one time use."). It replaces
  the use cap the owner chose per link (1..500, default 25): a bearer code that keeps working
  after it did its job can be forwarded to someone the owner never meant to invite. Every link
  still expires (1..30 days, default 7). The first accept that grants membership spends it; a
  second holder and a member the owner later removes get the dead-code 404. An accept that grants
  nothing new (the owner, or someone already at that role or higher) spends nothing, on a spent
  link too: re-opening the link you joined through answers your role, not an error (amended the
  same day; the first cut gave the joiner the 404 as well). A spent code is thus an oracle only to
  people who can already see the Space; a revoked or expired link is dead for everyone. A link is dead, too, while its Space's owner is out of the org or suspended: a
  departing person's private Space is archived (owner ruling, 2026-10-06), never opened by a
  link they minted before they left.
- **A fork reads its parent as it stood at the fork** (2026-10-04): `task.created.parent_seq`
  records the branch point, the transcript reads each ancestor only up to it, and the inherited
  files are the parent's shelf at the fork. A parent the fork's creator can no longer open (taken
  home, never shared) is not read at all. A teammate's fork is never a window into what its
  parent's creator does afterwards.
