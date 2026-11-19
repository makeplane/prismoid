# sandbar HTTP API

Base URL `http://127.0.0.1:7070` by default. Every route but `/healthz` and `/display` requires
`Authorization: Bearer <SANDBAR_TOKEN>` when a token is configured; `/display` takes only a
display ticket (below) and never the token, and every other route refuses a ticket. Bodies and
responses are JSON except where noted "raw bytes". A JSON request body is at most 16 MiB (`400`
past it); a file body at most 512 MiB.

Errors are `{"error": {"message": "..."}}` with a status code. A `404` also carries a `code`
saying which one it is: `"not_found"` (no such sandbox) or `"no_such_path"` (the sandbox exists,
the path in it does not - `GET …/files` and `GET …/dir`). A path the workload's own identity may
not touch is `403` with code `"forbidden_path"` (the file routes and `GET …/dir`); a path that is not clean and
absolute, or an unknown `owner`, is `400`. A stale egress push's `409` carries
`{"error": {"message": "...", "code": "stale", "version": 5}}`, the version sandbar holds:

| Status | Meaning |
|---|---|
| 101 | the display route upgraded the connection |
| 400 | invalid spec / bad id / bad json (a JSON body over 16 MiB included) |
| 401 | missing or wrong bearer token, or a refused display ticket |
| 403 | a path the workload's own identity may not touch (`code: "forbidden_path"`: the file routes and the dir route) |
| 404 | no such sandbox (`code: "not_found"`), or no such path in a sandbox that exists (`code: "no_such_path"`, the file and dir reads) |
| 409 | a stale egress version (`PUT …/egress` whose `version` is not newer than the applied one; the nested error also carries `code: "stale"` and `version`, the one sandbar holds) |
| 501 | the sandbox's backend cannot stream an exec (the display route) |
| 503 | backend unavailable (e.g. firecracker host lost `/dev/kvm`) |
| 502 | the backend failed to carry out a valid request |

---

### `GET /healthz`

Unauthenticated. `200 → {"status":"ok","backends":["docker","firecracker"]}`.

### `POST /v1/sandboxes`

Create and boot a sandbox. Returns only when it is fully usable.

```jsonc
// request
{
  "id": "frame-abc",            // optional; generated if omitted
  "spec": {
    "image": "prismoid-frame",  // docker ref, or firecracker rootfs template name
    "backend": "firecracker",   // optional; daemon default otherwise
    "cpus": 2,                   // optional (default 2)
    "mem_mb": 2048,              // optional (default 2048)
    "allow_domains": [],         // egress allowlist (enforced by the docker backend with
                                  // SANDBAR_EGRESS=on; firecracker has no NIC, so it refuses
                                  // a non-empty list rather than silently ignoring it)
    "env": {"KEY": "value"},     // optional
    "labels": {"owner": "x"}     // optional; opaque to sandbar, returned by list and get
                                  // (at most 32; keys 1-63 chars, values at most 200)
  },
  "egress_version": 1            // optional; the version allow_domains carries (0 = unversioned)
}
```

```jsonc
// 201
{
  "id": "frame-abc",
  "backend": "firecracker",
  "spec": { … },
  "created_at": "2026-07-31T00:00:00Z"
}
```

### `GET /v1/sandboxes` · `GET /v1/sandboxes/{id}` · `DELETE /v1/sandboxes/{id}`

List (`{"items":[…]}`), fetch one record, or destroy (idempotent; `{"destroyed":"id"}`).

Each record from the list and from fetch-one carries a `state`: `running`, `stopped`, `missing` (the record outlived its container) or `unknown` (the backend could not say; never read as stopped). A backend that cannot report one (firecracker) omits it.

### `POST /v1/sandboxes/{id}/exec`

```jsonc
// request
{"command": "echo hi && exit 3", "timeout_s": 120}
// 200 - a non-zero exit code is a RESULT, not an error
{"exit_code": 3, "stdout": "hi\n", "stderr": "", "duration_ms": 12.4}
// 200 - past timeout_s (default 120) the command is killed: exit 124, the output so far
{"exit_code": 124, "stdout": "begun\n", "stderr": "timed out after 5s", "duration_ms": 5003.1}
// 200 - stdout or stderr past 16 MiB: the rest is dropped, and the result says so
{"exit_code": 0, "stdout": "…16 MiB…", "stderr": "", "duration_ms": 900.2, "truncated": true}
```

The command runs through `/bin/sh -c` as uid 1000 (the agent), working directory `/work`. It is
never a login shell: no exec reads `$HOME/.profile`, because the sandbox's HOME may be the
workload's own directory, and the caller's system commands run through this same door.

The daemon enforces `timeout_s`: a command past it is killed with what it started and answers
exit code `124` (as `timeout` does), never an error. The docker backend marks each exec's
processes with a `SANDBAR_EXEC` variable and kills, as the exec's own uid, every process that
carries it (the Engine cannot kill an exec); a process that clears its own environment escapes
that sweep. The firecracker backend kills the command's process group. Each of stdout and stderr
keeps its first 16 MiB; `truncated: true` says one of them was cut (the field is absent
otherwise). The firecracker backend may cut them further so the reply fits its guest channel
(output that is mostly control bytes, such as NULs, escapes to six bytes each), and then also
says `truncated: true`.

An exec ends with its shell, not with the last process holding its output. A background child
that still holds stdout or stderr gets about 2 s after the shell exits; then the streams close,
the answer carries the shell's own exit code and what was printed so far, and the child runs on
(it is not killed, and what it prints later is lost). So `sleep 300 & echo hi` answers exit `0`
with `hi` after about 2 s on both backends. To start a long-lived process, redirect its output
(`server >/tmp/server.log 2>&1 &`) and the exec answers at once.

### `POST /v1/sandboxes/{id}/remove`

```jsonc
// request
{"paths": ["/task/inputs/01-overview-final.md", "/task/inputs/figs", "/task/context.md"]}
// 200
{"removed": 3}
```

Takes back what the system gave the sandbox: each path - a file or a whole directory - is removed
as root, the owner of what `owner=system` writes, and the directory it sat in keeps its mode and
owner (`/task` and `/task/inputs` stay `0555` and root's). Every path must be clean and strictly
below `/task`; anything else is `400` before the backend is asked. A path that is not there is
already removed. It runs no command a caller names (the Docker backend runs one fixed script whose
arguments are the paths); a backend that cannot remove (firecracker) answers `400`.

### `PUT /v1/sandboxes/{id}/files`

Query params: `path` (required, absolute and clean: no `..`, `//`, trailing slash or NUL, else `400`;
`403` code `forbidden_path` if the agent may not write it), `mode` (octal, default `644`), `owner`
(`agent`|`system`, default `agent`; any other value is `400`). **Body is the raw file bytes** (binary-safe, ≤512 MiB).
The daemon reads at most four bodies over 8 MiB (or of unknown length) at once; a fifth waits for
one of them to finish.

```
PUT /v1/sandboxes/frame-abc/files?path=/task/inputs/brief.md&mode=444&owner=system
<raw bytes>
→ 200 {"path":"/task/inputs/brief.md","bytes":1234}
```

Who writes is the `owner`, and on both backends it is who the write *is* (on firecracker guestd
makes an agent write on a thread that is uid:gid 1000, with the same permission answers; a link on
the path is refused there as `403` code `forbidden_path`, where docker follows it as the agent):

- `owner=agent` runs inside the sandbox as uid:gid `1000:1000`: the parent is made, the bytes go to
  a fresh name beside the target and are renamed over it once all of them arrived, so a write cut
  short never replaces a file. The sandbox's own permissions decide: a path uid 1000 may not write
  (a root-owned file, a folder it cannot enter, a link into one, root's file in a sticky folder) is
  `403` code `forbidden_path` and nothing is written. On a stock image Docker makes a missing
  working directory (`/work`) root's, `0755`, so the image must give it to uid 1000 for agent
  writes there. A target that is a directory is refused (`502`). A target that is a pipe or a
  device is refused on firecracker (`502`, no regular file; `writeOwned` checks the open file, for
  agent writes, system writes and archive-restore entries alike); on docker the agent's temp file is
  renamed over it, replacing the pipe or device node.
- `owner=system` is the Engine's write, as root. A path whose existing parent components include a
  symbolic link is `403` code `forbidden_path` (the Engine would follow it; the look is made at the
  time of the write, so a parent the workload can rename may still move under it); a link at the file's
  own name is replaced, not followed. The look fails closed: when root cannot tell what every
  component is (its `stat` could not run, or answered anything but kinds of file and missing
  components), or cannot make the missing parent, the write is `502` with the reason and nothing
  is written.

### `GET /v1/sandboxes/{id}/files?path=…`

`path` must be absolute and clean (`400` otherwise); `403` code `forbidden_path` if the agent may not
read it. Returns the file's **raw bytes** (`Content-Type: application/octet-stream`). Binary-safe: a
`.xlsx` read back through this endpoint hashes identically to what was written.

On both backends a read is the agent's, like an agent write (firecracker: a guestd thread that is
uid 1000, a missing file `404` `no_such_path`; docker: it runs inside the sandbox as
uid:gid `1000:1000` - a look (`stat`, following a link) and then the read (`head`, for exactly the
size the look saw)) - so the sandbox's own permissions decide. A path uid 1000 may not read (the
browser user's closed profile, a file only root may read, a link into either) is `403` code
`forbidden_path`. On the read route both backends follow a link as the agent (they read what uid
1000 could read through it); firecracker refuses a `/proc` magic link (a process's `fd`, `root` or
`cwd`: `403` `forbidden_path`), since its reading thread is guestd's own. On the dir route both
follow a link to a directory.

Answers by backend for a path that holds no regular file:

| Path | docker | firecracker |
|---|---|---|
| absent | `404` `no_such_path` | `404` `no_such_path` |
| a link | followed as the agent (a dangling one is `404`) | followed as the agent (a dangling one is `404`; a `/proc` magic link is `403` `forbidden_path`) |
| a directory, a pipe, a device, a parent that is a file | `404` `no_such_path` | `502` |

Where it is a `404` the body is `{"error": {"message": "no such path: /x: stat: can't stat
'/x': No such file or directory", "code": "no_such_path"}}`: the caller's answer, not a failure. A
missing sandbox is the `404` with code `not_found`; a backend failure stays `502`. A file over
512 MiB is refused (`502`, saying so) before any of it is read. A file that grows while it is read
comes back at the size it had when looked at, and one whose size the kernel does not report (most
of `/proc`) comes back as that size says, so a read never holds more than the look saw.

### `GET /v1/sandboxes/{id}/dir?path=…`

`path` must be absolute and clean (`400` otherwise).

`200 → {"entries":["file.txt","subdir/"]}` - directories carry a trailing slash; an empty directory
is `{"entries":[]}`, never `null`. Answers by backend:

| Path | docker | firecracker |
|---|---|---|
| absent | `404` `no_such_path` | `404` `no_such_path` |
| a dangling link | `200`, the link's own name on a GNU `ls` image (busybox: `404` `no_such_path`) | `404` `no_such_path` |
| a link to a directory | `200`, the target's entries (followed as the agent; an unreadable target is `403`) | `200`, the target's entries (followed as the agent; an unreadable target is `403`) |
| a link to a file | `200`, the link's own name (`ls` lists it) | `502` (`ENOTDIR`) |
| a directory the agent may not read | `403` `forbidden_path` | `403` `forbidden_path` |
| a regular file | `200`, listing the file's own path | `502` |
| a path whose parent is a file | `404` `no_such_path` (`ls`: "Not a directory") | `502` |

### `GET /v1/sandboxes/{id}/archive?path=…&path=…`

Streams a **tar** (`application/x-tar`) of the given paths. Entry names are rooted at each path's
basename (`archive?path=/work` → entries `work/…`), so extracting the tar at `/` restores the
tree. Repeat `path` for multiple roots - this is how a caller snapshots a working set.

A path that does not exist contributes nothing (all absent → a complete empty tar). Any other
failure is never a clean end: before the first byte it is an ordinary error response (`502` with
the reason); after it, the connection is aborted, so the client reads a broken body instead of a
cut-short tar it could take for the whole tree.

### `POST /v1/sandboxes/{id}/archive`

Extracts a tar (raw body) at `/`. The inverse of `GET archive`; the round-trip is a filesystem
snapshot/restore. Entries that would escape `/` are refused.

### `GET /v1/sandboxes/{id}/denials`

What the egress allowlist refused for this sandbox since its list was last set (its create, then each
accepted `PUT …/egress`) - the audit for "why did the fetch fail".

```jsonc
// 200 - empty for a sandbox with no egress
{"denials": [{"host": "ifconfig.me", "method": "CONNECT", "at": "2026-07-31T04:11:26Z"}]}
```

### `PUT /v1/sandboxes/{id}/egress`

Replaces the sandbox's egress allowlist while it keeps running - a consent widening it, a step
narrowing it. The token/principal never changes.

```jsonc
// request
{"allow_domains": ["example.com", "accounts.google.com"], "version": 4}
// 200 - "version" is the version now recorded
{"ok": true, "allow_domains": ["example.com", "accounts.google.com"], "version": 4}
```

**Versioned.** A `version` must be newer than the last one applied (the create's
`egress_version`, then each accepted push), or the push is refused with `409` and the allowlist
is left alone - a slow push can never overwrite a newer list. An accepted push clears the
sandbox's `/denials`: they were refusals of the list it replaced. The `409` body is
`{"error": {"message": "...", "code": "stale", "version": 5}}`, where `version` is the one
sandbar holds, so a caller can adopt it and push once past it. An absent or `0` version is an
unversioned push: it always applies and keeps the recorded version.

`400` when the sandbox has no egress principal (it was created with no `allow_domains`, or its
backend does not implement egress - firecracker refuses it, docs/firecracker.md). `404` for an
unknown sandbox.

### `GET /v1/sandboxes/{id}/display`

The browser screen's pipe: a plain HTTP upgrade, not a
WebSocket.

```http
GET /v1/sandboxes/{id}/display HTTP/1.1
Authorization: Ticket dt_<base64url(claims JSON)>.<base64url(Ed25519 signature)>
Connection: Upgrade
Upgrade: sandbar-display
```

`101 Switching Protocols` (with `Upgrade: sandbar-display`), then raw bytes both ways between the
connection and the browser sidecar's screen bridge (`python3 -m sidecar.screen`, run as the
browser user; its stdin and stdout, stderr discarded). sandbar parses nothing on the pipe, and
it ends when either side closes. It rides an exec, never the sandbox's network: a sandbox with no
route out has a screen too.

**The ticket** is the only credential this route takes. Claims
`{"sandbox", "iat", "exp", "jti", "kid"}` (unix seconds), signed with Ed25519 over
`"sandbar-display-v1\n"` + the claims bytes; `exp - iat` at most 30 s; ±5 s of clock skew; the
`sandbox` must be the route's; single use - spent once the request has proved itself and before
the bridge starts, so one ticket starts at most one exec and a failed upgrade still spends it (the
caller mints another); a ticket issued before the daemon started plus 5 s is refused, so a restart
cannot reopen a replay. sandbar holds only public keys: `SANDBAR_DISPLAY_KEYS=kid:base64,...`. Plane
prints the value from `backend/`, with the same `PRISMOID_API_SCREEN_SIGNING_KEY` set (unset, it
prints a throwaway dev key no ticket will match):
`uv run python -c 'from prismoid.operations import tickets; print(",".join(f"{k}:{v}" for k, v in tickets.public_keys().items()))'`.
Without uv, from the key alone (`KEY=<kid>:<base64 seed>`):
`echo "${KEY%%:*}:$({ printf '\060\056\002\001\000\060\005\006\003\053\145\160\004\042\004\040'; printf %s "${KEY#*:}" | base64 -d; } | openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64)"`.

`503` display disabled (no keys) · `401` no ticket, or a refused one (the reason: `malformed`,
`signature`, `unknown_kid`, `lifetime`, `expired`, `stale`, `mismatch`, `replayed`) · `400`
without the upgrade headers, or a backend that refuses (firecracker has no streaming exec yet)
· `404` unknown sandbox · `501` a backend that cannot stream · `502` the exec could not start.

---

## A full session, with curl

```bash
T='Authorization: Bearer dev'; B=localhost:7070
curl -s -H "$T" -XPOST $B/v1/sandboxes -d '{"id":"s","spec":{"image":"prismoid-frame"}}'
curl -s -H "$T" -XPUT  "$B/v1/sandboxes/s/files?path=/task/inputs/brief.md&owner=system&mode=444" \
  --data-binary @brief.md
curl -s -H "$T" -XPOST $B/v1/sandboxes/s/exec -d '{"command":"wc -l /task/inputs/brief.md"}'
curl -s -H "$T" "$B/v1/sandboxes/s/archive?path=/work" > snapshot.tar
curl -s -H "$T" -XDELETE $B/v1/sandboxes/s
```

## Notes for client authors

- **Idempotency**: `create` with an explicit `id` is not idempotent - a repeat is `400 already
  exists`. Pick your own ids (e.g. `task-{id}`) and treat a 400-exists as "already running".
- **Timeouts**: `exec` can run for minutes; set a generous client read timeout. The daemon
  kills each exec at `timeout_s` and answers exit code `124`.
- **Streaming**: `archive` streams; read it incrementally for large working sets rather than
  buffering the whole tar in memory.
- **Reference client**: Prismoid's `backend/prismoid/ai/frames/sandbar_driver.py` is a complete,
  commented async client (Python/httpx) if you want a worked example.
