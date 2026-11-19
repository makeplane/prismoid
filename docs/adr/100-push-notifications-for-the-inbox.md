# ADR-100 · Push notifications for the inbox

**Status:** accepted · 2026-11-17 · the mobile app needs the inbox on the lock screen.
**Amends** the HANDOFF §1 Inbox row: the inbox still sends no mail, but it now pushes to the
recipient's own phones.

## Context

The inbox is how Prismoid tells someone who was not watching that work finished, failed or waits on
them. On the web that means opening a tab. The mobile app (a separate repository) needs the same
items as push notifications, its badge at the unread count, and a phone that follows reads made
elsewhere. A step's mid-run question (an INPUT gate) also knocked nobody until now: only approval,
connect and hand-off gates wrote an inbox item, so a question sat unseen until its TTL.

## Decision

### 1. Push devices

A `push_devices` table (identity tenancy: a phone belongs to a person, not a workspace): `actor_id`,
`session_id` (the login session that registered it), `token` (FCM's registration token, unique),
`platform` (`ios` | `android`, a vocabulary CHECK), `app_id`, `app_version`, `created_at`,
`last_seen_at`. A device lives as long as its session: `AuthSession.revoke`, `revoke_one` and
`revoke_all_for` (logout, the admin's revocation, a password reset) delete the session's devices in
the same transaction, the session sweep's hard delete cascades (`ON DELETE CASCADE`), and a push only
reaches a device whose session is still live, so an expired session's phone hears nothing before the
sweep removes it.

Named for push throughout (`PushDevice`, `push_devices`, `/v1/push/devices`): a bare "device" is
the person's Mac (ADR-078), and a word means one thing.

`POST /v1/push/devices` (`{token, platform, app_id, app_version}` → 201 `PushDeviceOut`) upserts on
the token: a token already known moves to the caller and their session and is seen now. `DELETE
/v1/push/devices/{id}` (204) removes one of the caller's own devices; anyone else's is a 404. Both
take a login session (`SessionActorDep`, and the session id through `LoginSessionIdDep`): an API key
has no session for the device to end with. The row is the person's, not a workspace's (a person
removes their phone from whichever workspace they are in), but the workspace door applies as on
every route except the way back in: a person with no workspace left has no inbox to push, and logout
still ends their phones with the session. The token never leaves the server again; `DeviceOut`
carries no token. It is stored as FCM handed it (a send needs it) and never logged: alone it lets
nobody act, and only the deployment's Firebase key can send to it.

### 2. When a push is asked for

`operations/inbox._add`, the inbox's one writer, asks for the item's push with
`dispatch.enqueue_after_commit`: the enqueue runs when the caller's outermost transaction commits and
is dropped when it rolls back. The inbox writes inside other operations' transactions (gates, the
settle path, routines), so it cannot enqueue after a commit it never sees; a session `after_commit`
listener (the call log's blob sweep is the precedent) is the one place that does. Nothing is asked
for while push is off or the recipient has no live device. The job id is `push-inbox-{item}`.

A read asks for a silent push the same way, before its own commit, for each person whose items it
read: one item (only when it was unread), a task's on its page (the caller's own), or a closed gate's
knocks (every recipient's, each person their own push). An item is read once, so the smallest id of
what one read stamped keys its job: `push-read-{item}`. A read-all that changed something is
`push-read-all-{actor}-{workspace}-{read stamp}`. A dispatcher outage therefore never turns a committed
read into an error.

The enqueue runs in an empty context (`contextvars.Context()`), the move `durable.start` makes inside
an effect. An inbox item written in a job's body (a routine's fire: `fire_routine` → `execute_fire` →
`routine_held`) would otherwise hand that job to the enqueue, and the push would start as its child,
journaled out of order and unreplayable when the job recovers. Detached, every push is a job of its
own.

The after-commit enqueue is best effort: a process that dies between the commit and the enqueue
loses the push. No sweeper re-offers it; a missed alert is a courtesy lost, and the inbox row it
announces stands.

### 3. Sending

Two jobs (`push_inbox`, `push_read`) on their own `push` queue (16 at a time), each one journaled
effect: the effect reads the item and the recipient's unread counts, builds the message, sends it to
every live device concurrently, deletes the devices whose token FCM called dead and commits the
prune. An item read before its job runs (the person had the task open, or its gate closed) pushes
nothing: the alert would ring for something already seen, after the silent update that cleared it.
A replayed job returns the recorded count instead of pushing again.

**Amended 2026-11-18 (the alert waits):** the read above lost its race. The item is written unread,
the `push` queue takes the job within a second, and the open task page reads the item only after its
stream shows the news and its view has been still for 400 ms, so a person watching the reply land got
the alert and then the silent update that cleared it. `push.announce` now asks for `push_inbox` to
wait (`PRISMOID_PUSH_ALERT_DELAY_S`, 5 s): `enqueue_after_commit` and `Dispatcher.enqueue` carry a
caller's `delay_s` to the engine's delayed enqueue (`durable.start_later`, the client's
`delay_s`), holding no queue slot while it waits, and the dispatchers know no job's delay;
`push_read` still starts at once. A task's
card in a bot's chat reads its task's items on news too (`useSeenTask` with `onOpen: false`), as the
task page does. The cost: every alert lands about five seconds late, and a page slower than the delay
still rings. The boot sweep moves another version's waiting jobs as it moves its queued ones. This
is a stopgap: writing no item for someone watching the task is the follow-up that drops the delay. A push never fails anything: the
effect swallows its errors with a log.

FCM's answer is dead for `UNREGISTERED` and an `INVALID_ARGUMENT` whose message names the
registration token. Every other refusal is a failure that says nothing about the token, and the row
stays: a malformed payload, a 404 that names no token, quota, unavailability, and `SENDER_ID_MISMATCH`,
which says the key is wrong for the token (a wrong or rotated service account) and would otherwise
prune every phone at once; the app registers again on every launch anyway. One failing token never
stops the others.

### 4. The message (the mobile app's contract)

An item's alert: `notification` = the item's title (120 characters) and body (240, whitespace
folded). `data`, every value a string and a null key left out: `type=inbox`, `inbox_id`, `kind`,
`workspace_id`, `task_id`, `gate_id`, `project_id`, `routine_id` and `connector_slug` (from the
payload when present), `unread` (the recipient's unread count in the item's workspace, this item
included), `unread_total` (their unread count across every workspace they belong to, this item
included). Android: `notification.channel_id` is `needs_you` for `gate_opened`, `question` and
`budget` (priority high) and `updates` for every other kind (priority normal); `notification.tag` is
the task id when there is one, so a task's newer alert replaces its older one. APNs: `aps.badge` =
`unread_total`, `aps.sound=default`, `aps.thread-id` = the task id, else the kind; `apns-push-type: alert`.

A read's silent push: `data` = `type=inbox_read`, `inbox_ids` (the ids read, comma-separated, at most 64; or `all`),
`workspace_id` (where the read happened), `unread` (that workspace's new count), `unread_total` (the
person's new count across every workspace). APNs: `aps.content-available=1` and `aps.badge` =
`unread_total`, no alert and no sound, `apns-push-type: background`, `apns-priority: 5`. Android: a
normal-priority data message.

`unread` is per workspace, the count `GET /v1/inbox` reports in that workspace, and `workspace_id`
rides every push so the app can tell which. The badge belongs to the person, not a workspace, so it is
`unread_total`: `InboxItem.unread_total_of`, an identity read on a platform unit of work, counting the
unread items in every active workspace the person is still a member of (a suspended one's inbox
can't be opened, so it would hold the badge up for good).

### 5. Questions knock the inbox

A new kind, `question`: `gate_open.open_question` writes one for whoever can answer it, the approval
gate's audience (the creator, and the turn's author while an editor), titled "{task} asks: {first
question}" (with "(+n more)", 160 characters at most), the batch numbered in the body, the gate on the
item. The web inbox labels the kind "Questions" and answers its gate in place like an approval.

### 6. Configuration and the wire

`PRISMOID_PUSH_CREDENTIALS_JSON` (a `SecretStr`, `conf_push.py`): the Firebase service account's JSON
key, whole; its `project_id` names the FCM project. Set, it must parse as a service account (JSON with
`project_id`, `client_email`, `private_key`), or any boot fails without echoing it. Empty, push is off and
the API and the worker each say so once at boot. A deployment turns push on by setting the key in
its own configuration; no boot refuses to start without it, so a deployment without it simply runs
without push. Off, the
`/v1/push/devices` routes answer 404 as a missing route would and store no token: a server that
sends nothing keeps no phones. They stay in the contract, checked per request, so the API reads the
same on every server.
`PRISMOID_PUSH_FCM_URL`, `PRISMOID_PUSH_OAUTH_SCOPE`, `PRISMOID_PUSH_TIMEOUT_S` and
`PRISMOID_PUSH_ALERT_DELAY_S` (§3's amendment; 0 sends at once) carry the defaults. The URL audit now also exempts `conf_push.py` by name: the group's default endpoint is a
setting like any in conf.py.

The wire is `prismoid/fcm.py`, a sibling of `mailer.py` in the layer contract: FCM's HTTP v1 send,
one POST per token over `httpx`, with an OAuth access token minted from the service account by
`google-auth`. When it lapses (hourly) one job refreshes it in a worker thread, bounded by the push
timeout rather than google-auth's 120 s; the others wait on an asyncio lock, not in the loop's
shared executor, so a hung token endpoint can't starve unrelated `to_thread` work.

## Alternatives rejected

- **firebase-admin.** The official SDK is one `messaging.send` call, but it brings Firestore, Cloud
  Storage, the Google API client and their transitive tree into an image that needs one POST, and its
  sync calls would need a thread pool anyway. `google-auth[requests]` is already locked (google-genai
  depends on it with that extra), so the declaration adds no package to the lock.
- **Signing the OAuth assertion by hand with `cryptography`.** No new declaration at all, but it
  re-implements the JWT bearer grant google-auth already gets right (clock skew, refresh, errors).
- **Enqueue from each caller after its commit.** A dozen callers across gates, the settle path and
  routines; one would forget. The after-commit hook in the one writer cannot be forgotten.
- **A push outbox table and a sweeper.** Exactly-once delivery for a courtesy notification; the
  inbox row is already the durable record.
- **Encrypting tokens at rest.** A registration token is not a credential to Prismoid; the unique
  upsert needs it searchable, and encryption would buy a hash column for no threat it closes.

## Consequences

- A deployment sets the key to turn push on, and the mobile app's iOS and Android builds must be
  registered in that same Firebase project (with the APNs auth key uploaded for iOS). Without the key every deployment boots with push off.
- `google-auth[requests]` is declared in `pyproject.toml` and has a HANDOFF §3 row.
- `push_devices.token` is the one plaintext token at rest; `db/CLAUDE.md`'s rule names the exception.
- A wrong Firebase project in the key answers `SENDER_ID_MISMATCH` for every token: every push fails
  and is logged, and no device is pruned, so fixing the key brings every phone back.
- Every inbox item now costs one indexed `EXISTS` on `push_devices` while push is on; a person with no phone
  costs no job.
