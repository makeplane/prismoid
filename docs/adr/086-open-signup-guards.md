# ADR-086 · Open signup guards

**Status:** accepted · 2026-11-14 · a deployment may open signup, and an open door needs guards.

## Context

A deployment that opens signup lets anyone mint an account and a workspace, and every account
administers a workspace that can send invites, which are mail from the deployment's domain. So the
door needs an email check before anyone invites and a workspace cap. The signup page also has to
know, before anyone is signed in, whether signups are open and where the terms, privacy policy and
support address live. Later sections of this record add the guards; this one covers the config door.

## Decision

### 1. The config door

`GET /v1/auth/config` takes no Actor. It returns the deployment's public face and nothing else:
`signup` (`open` or `invite`), `terms_url`, `privacy_url` and `support_email`. Every field comes from
settings (`PRISMOID_AUTH_SIGNUP_ENABLED`, `PRISMOID_WEB_TERMS_URL`, `PRISMOID_WEB_PRIVACY_URL`,
`PRISMOID_SUPPORT_EMAIL`); an unset link is `null` and the page does not show it. The read touches no
table and takes no input, so it reveals nothing about any account: it cannot tell whether an email is
registered.

It is its own route, not part of `/healthz`: health answers a load balancer's question and is kept
to ok or degraded; the config is read by the web app, changes with deployment settings and is cached
by the page for its life.

A refused signup says why: `SignupDisabled` (403, code `signup_disabled`, "Signups are paused.")
replaces the plain `forbidden` the closed door used to give, so the page can switch to its paused state.

### 2. The workspace cap

Every team workspace a person founds is one more tenant to farm: a place for runs, members and
connectors. `PRISMOID_WORKSPACE_MAX_FOUNDED` (int, 0 or more; unset = no limit) caps the team
workspaces one person founds. A deployment that sets it to `0` has teams work through invites into a
personal workspace.

- What counts: every TEAM workspace the person created, in any status (a suspended one was still
  founded). The personal workspace never counts, and a workspace joined by invite was not founded.
- Where: `create_team_workspace` takes the person's `User` row lock, then counts, then creates, so two
  racing creates see each other. Over the cap raises `WorkspaceCapReached` (403, code `workspace_cap`,
  "New workspaces are off here. Invite people into your workspace instead."). The rate limiter stays.
- Platform admins are exempt.
- `GET /v1/auth/me` carries `can_create_workspace` (`workspaces.can_found`), so the switcher hides
  "Create workspace" instead of offering a door that answers 403.

### 3. An emailed proof of an address, before any invite

A person proves their address with an emailed code, and only a proven address sends invites.

- Signup still works unverified: the account, the workspace and the session exist.
- The proof is the `password_resets` pattern, one door over: an emailed single-use `ev_` code,
  SHA-256 at rest, a 24 h TTL, superseded by a resend, swept after a grace, at most five a person an
  hour (`email_verifications`; `PRISMOID_AUTH_EMAIL_VERIFICATION_*`). A claim is bound to the user it was
  minted for, so one person's code never verifies another. Mail whose send failed un-mints its row.
- The mail links `{web_base_url}/verify-email#<code>`: the code rides the URL fragment, which no server
  or proxy ever logs; with no web address set the mail shows the code to paste. The page reads the
  fragment, keeps it in `sessionStorage` across a sign-in detour, and posts it once.
- `POST /v1/auth/me/verify-email` `{code}` -> `MeOut` and `POST /v1/auth/me/verification` (202) are
  Actor-authenticated, so neither is a new unauthenticated door. Verifying twice, by any road, is
  quiet: an already-verified person touches no code.
- An invite to this address (the inviter mailed it, so it is proved) and `prismoid up`'s first run mark the
  address verified at once; no code is mailed.
- Sending an invite waits on the same proof. An invite is mail from our domain carrying a workspace
  name and an inviter name the sender chose, and a fresh account administers its own personal
  workspace, so with open signup one account would otherwise buy unlimited phishing-shaped mail.
  `workspaces.invite` refuses an unverified person with 403 ("Verify your email before you invite
  anyone.") and caps each person at `PRISMOID_WORKSPACE_INVITE_MAX_PER_HOUR` invites an hour (default
  30, durable: a count of their `workspace_invites` rows, 429 past it).
- Existing users are backfilled as verified (`email_verified_at = created_at`): they joined under
  invite-only.
- `GET /v1/auth/me` carries `email_verified`.
- A bad, expired, used or other person's code answers 422 with `fields.code`, never 401: a 401 means a
  dead session to the web client, which would sign the person out and lose the link.

## Alternatives rejected

- Folding the config into `/healthz`: mixes a load-balancer probe with page data and widens what the
  probe discloses.
- Baking the values into the web build: a signup toggle or a support address would need a redeploy.

## Consequences

- The verification pair is Actor-authenticated, so it adds no door. The config read is a new unauthenticated route: listed in the root `CLAUDE.md` and `backend/prismoid/api/CLAUDE.md` door lists.
- The web signup page fetches the config once per page load.
