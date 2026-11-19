# sandbar

**A sandbox daemon: run code in a disposable, isolated sandbox, drive it over HTTP.**

sandbar gives each sandbox a hardened container (the **docker** backend, the one it ships) and
exposes create / exec / file-I/O / snapshot / destroy as a small HTTP API. An experimental
[Firecracker](https://firecracker-microvm.github.io/) backend - its own Linux kernel per sandbox
in a KVM guest - sits behind the same API. Your application speaks that API in any language and
never links sandbar's code, so the isolation technology is a *deployment* choice, not a
dependency.

It was built for [Prismoid](../) to run model-generated code, but it is a standalone project with
no Prismoid knowledge in it - point any program that needs disposable, isolated compute at
`sandbard` and go.

```
   your app  ──HTTP──▶  sandbard  ──▶  backend  ──▶  one sandbox per task
  (any lang)            (this repo)     docker           a hardened container   (shipped)
                                        firecracker       a microVM              (experimental)
```

## Support matrix

| | docker | firecracker |
|---|---|---|
| Status | **shipped** - the backend Prismoid's self-hosted deploy runs | **experimental** - evaluate it, do not serve users with it |
| Isolation | a container on the host's kernel: non-root workload, every capability dropped, `no-new-privileges`, a process cap, an open-file cap, no swap, an init that reaps; optional read-only rootfs, storage cap and OCI runtime (e.g. gVisor's `runsc`) | its own kernel in a KVM guest |
| Exec, files, archive | yes | yes |
| Exec timeout enforced, output bounded | yes | yes |
| Egress allowlist | yes (`SANDBAR_EGRESS=on`) | no - refuses `allow_domains` (no NIC) |
| Display route, state and start, remove, adoption across a restart | yes | no |
| Automated tests | pure, played-Engine and real-Engine tiers | unit tests only (no KVM in CI) |
| Runs on | any Docker Engine (macOS included) | Linux with `/dev/kvm` |

A container shares the host's kernel: a kernel exploit from inside one is a host compromise. The
docker backend narrows what a workload can reach, but it is not a VM. For code from people you do
not trust, run sandbar on a host given to that purpose, set `SANDBAR_DOCKER_RUNTIME=runsc` where
gVisor is installed, and read [Security posture](#security-posture).

## Why microVMs (where the firecracker backend is headed)

A container shares the host kernel. A kernel exploit from code running inside a container is a
host compromise - and with it, every other container on the box. That is an acceptable risk for
*your* code; it is not acceptable for code a language model wrote, which is exactly the workload
agent platforms run. Firecracker gives each sandbox its **own kernel** in a hardware-virtualized
guest, with a ~5 MiB memory overhead and ~125 ms boot - VM isolation at container density. It is
what [E2B](https://e2b.dev), [Fly Machines](https://fly.io), Cloudflare Sandboxes, and AWS Lambda
MicroVMs all run untrusted code inside. sandbar is a small, legible, self-hostable member of that
family.

The **docker** backend is the one sandbar ships today, and the behavioural reference the
firecracker backend matches on the wire - so once firecracker is ready, a caller moves to it with
no code change. Until then, firecracker is experimental (see the support matrix).

## Design

Three ideas, and the whole codebase falls out of them.

**1 - One contract, `internal/core`.** A sandbox is an `Instance` (exec, read/write files, tar
archive/extract, destroy). A backend is a `Backend` (name, create). That is the entire seam. The
API layer speaks only these types and imports no backend; a backend implements these two
interfaces and knows nothing about HTTP. Adding an isolation technology is one new package that
satisfies `Backend` - nothing else in the tree changes.

**2 - HTTP is the public seam.** The daemon hides Go, hides which backend isolated the work, and
maps a handful of sentinel errors onto status codes. The routes *are* the API (see below). One
bearer token guards everything but `/healthz`; run the daemon on a private network beside its
caller - the token is a tripwire against a misrouted request, not a public auth system.

**3 - Ephemeral by construction; durable truth lives upstream.** sandbar's registry is an
in-memory map. There is nothing to persist because a sandbox is not where anything important
lives - the caller hydrates a sandbox from its own durable store (object storage, a database) at
start, and harvests results back out at the end. A restarted daemon starts empty and reaps any
orphaned real resources. This is what makes sandboxes cheap to throw away and safe to lose, and
it is the same posture Anthropic and OpenAI describe for their own code-execution sandboxes.

The guest agent (`sandbar-guestd`) inside a Firecracker VM speaks a wire protocol
(`internal/guestwire`) over `AF_VSOCK` that is deliberately *isomorphic* to the public HTTP API -
request header + optional body, response header + optional body. One mental model runs the whole
path from your program down to the kernel-isolated process.

## Layout

```
cmd/sandbard/          the daemon (HTTP server, backend wiring, graceful shutdown)
cmd/sandbar-guestd/    the in-VM agent (vsock server; Linux only, baked into the rootfs)
internal/core/         the contract: Spec, Instance, Backend, Registry, ids
internal/api/          the HTTP surface and error mapping
internal/ticket/       display tickets: Ed25519 verify, single use (the display route's credential)
internal/backend/
  docker/              the shipped backend - Docker Engine API over its unix socket
  firecracker/         experimental backend - Firecracker API + vsock to guestd
internal/guestwire/    the host⇄guest protocol (imported by both sides)
scripts/build-rootfs.sh   builds a Firecracker rootfs with guestd inside
docs/                  api.md · firecracker.md
```

## Quickstart - docker backend (any machine with Docker)

```bash
# build and run the daemon
go build -o /tmp/sandbard ./cmd/sandbard
SANDBAR_TOKEN=dev SANDBAR_BACKENDS=docker /tmp/sandbard &   # no egress; see below for egress on

# create a sandbox, run something, tear it down
T='Authorization: Bearer dev'
curl -s -H "$T" localhost:7070/healthz
curl -s -H "$T" -X POST localhost:7070/v1/sandboxes \
  -d '{"id":"demo","spec":{"image":"alpine:3.20"}}'
curl -s -H "$T" -X POST localhost:7070/v1/sandboxes/demo/exec \
  -d '{"command":"echo hello from a sandbox"}'
curl -s -H "$T" -X DELETE localhost:7070/v1/sandboxes/demo
```

> **macOS note:** the default port is **7070**, not 7000 - macOS's AirPlay Receiver
> (ControlCenter) silently binds `*:7000` and will intercept `localhost:7000`. If you must use
> 7000, disable AirPlay Receiver in System Settings → General → AirDrop & Handoff.

## Quickstart - firecracker backend (Linux + KVM, experimental)

See [docs/firecracker.md](docs/firecracker.md) for the full setup. In short:

```bash
# 1. a guest kernel (vmlinux) and a rootfs with guestd inside
sudo ./scripts/build-rootfs.sh            # → /var/lib/sandbar/rootfs/prismoid-frame.ext4
#    (download a Firecracker-compatible vmlinux separately; see the doc)

# 2. run the daemon with the firecracker backend
sudo SANDBAR_TOKEN=dev \
     SANDBAR_BACKENDS=firecracker SANDBAR_BACKEND=firecracker \
     SANDBAR_FC_KERNEL=/var/lib/sandbar/vmlinux \
     /tmp/sandbard

# same HTTP API - the sandbox is now a microVM.
curl -s -H 'Authorization: Bearer dev' -X POST localhost:7070/v1/sandboxes \
  -d '{"id":"vm1","spec":{"image":"prismoid-frame"}}'
```

The daemon **refuses to start** the firecracker backend on a host without `/dev/kvm` or a
kernel, rather than silently degrading to no isolation - the one failure this project exists to
prevent.

## The API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | liveness + which backends are available (unauthenticated) |
| `POST` | `/v1/sandboxes` | create; body `{id?, spec, egress_version?}` - `spec` is `{image, backend?, cpus?, mem_mb?, allow_domains?, env?, labels?}` |
| `GET` | `/v1/sandboxes` | list (each record with its `state`) |
| `GET` | `/v1/sandboxes/{id}` | one record, with its `state` |
| `DELETE` | `/v1/sandboxes/{id}` | destroy (idempotent) |
| `POST` | `/v1/sandboxes/{id}/start` | run a `stopped` sandbox again, its files kept; answers the record with its `state` (`404` code `not_found` if its container is gone, `400` for a backend that reports no state) |
| `POST` | `/v1/sandboxes/{id}/exec` | run a command; body `{command, timeout_s?}` |
| `POST` | `/v1/sandboxes/{id}/remove` | take back system paths: body `{paths: [...]}`, each clean and below `/task`, removed as root (`400` for any other path, or a backend that cannot) |
| `PUT` | `/v1/sandboxes/{id}/egress` | replace the egress allowlist and clear its denials; body `{allow_domains: [...], version?}` - `409` if the version is stale, `400` if the sandbox has no egress |
| `PUT` | `/v1/sandboxes/{id}/files?path=&mode=&owner=` | write one file (raw body) - `400` for an unclean path or an unknown owner, `403` code `forbidden_path` if the agent may not write it |
| `GET` | `/v1/sandboxes/{id}/files?path=` | read one file (raw bytes) - `400` for an unclean path, `403` code `forbidden_path`, `404` code `no_such_path` if the path is not there |
| `GET` | `/v1/sandboxes/{id}/dir?path=` | list a directory - `400` for an unclean path, `403` code `forbidden_path` if the agent may not read it, `404` code `no_such_path` if the path is not there |
| `GET` | `/v1/sandboxes/{id}/archive?path=&path=…` | stream a tar of paths |
| `POST` | `/v1/sandboxes/{id}/archive` | extract a tar at `/` (raw body) |
| `GET` | `/v1/sandboxes/{id}/display` | upgrade to the browser's screen bridge (`Upgrade: sandbar-display`); a display ticket only, never the token - see docs/api.md |

`state` is `running`, `stopped` (its processes are gone, its files are not: `start` runs it again), `missing` (the record outlived its container) or `unknown` (the backend could not say; never read as stopped). A backend that cannot report one (firecracker) omits it.

Every `404` names which one it is in `error.code`: `not_found` (no such sandbox) or
`no_such_path` (the sandbox exists, the path in it does not).

Full request/response shapes in [docs/api.md](docs/api.md).

### The file-owner distinction

`PUT …/files?owner=system` writes as uid 0; `owner=agent` (default) writes as uid 1000, and
sandbox commands run as uid 1000. This is not tidiness - it is load-bearing for any platform that
later *verifies* a sandbox's output. "system" files are what the sandbox was **given** (inputs,
instructions) and must be read-only to the workload; "agent" files are its workshop. A sandbox
where the workload can rewrite what it was given cannot answer "does this output trace to a source
we provided?" - the answer is always yes.

The uid is who the write *is*, not a label stamped on it afterwards. On the docker backend an agent
write runs inside the sandbox as `1000:1000` (into a fresh name beside the target, then renamed
over it, so a write cut short never replaces a file), so the sandbox's own permissions decide: a
path uid 1000 may not write - a root-owned file, a folder it cannot enter, a link into one, root's
file in a sticky folder such as `/tmp` - is `403` code `forbidden_path`, and nothing is written. An
image's workshop must therefore be uid 1000's: Docker makes a missing working directory root's,
`0755`, so on a stock image an agent write into `/work` is refused until the image gives `/work` to
uid 1000. A system write is the Engine's, as root; it refuses (`403` code `forbidden_path`) a path
whose existing parent components include a symbolic link, since the Engine would follow it (a check
made at the time of the write: a parent the workload can rename may still move under it), and it
replaces a link at the file's own name rather than following it. sandbar enforces who writes; which
paths a workload may name is its caller's to decide.

A read is the agent's too: on docker it runs inside the sandbox as `1000:1000` (a look,
then a read of exactly the size the look saw); on firecracker guestd reads on a thread that is
uid 1000 (agent writes and listings likewise), which follows a link as uid 1000 (a `/proc` magic
link is refused, `403` code `forbidden_path`, since that thread is guestd's own) and finds an absent
file `404` code `no_such_path`. So what uid 1000 may not read - the browser user's
closed profile, a file only root may read, a link into either - is `403` code `forbidden_path`, and
a link is followed only as far as uid 1000 could follow it. A file over 512 MiB is refused before a
byte of it is read.

The file door runs its commands from the system's folders only (`PATH=/usr/sbin:/usr/bin:/sbin:/bin`,
never the image's own PATH, which the workload might be able to write): an image needs `/bin/sh`
and `stat`, `head`, `cat`, `mkdir`, `mv`, `chmod` and `rm` there (and `find` for the remove route).

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SANDBAR_ADDR` | `127.0.0.1:7070` | listen address |
| `SANDBAR_TOKEN` | *(empty = auth off)* | bearer token, compared in constant time. Empty turns auth off, which sandbard allows only on a loopback `SANDBAR_ADDR` (it refuses to boot otherwise) |
| `SANDBAR_BACKEND` | `docker` | default backend for a spec that names none |
| `SANDBAR_BACKENDS` | `docker` | comma list of backends to enable |
| `SANDBAR_DOCKER_SOCK` | `/var/run/docker.sock` | docker backend socket |
| `SANDBAR_DOCKER_NET` | `none` | docker network mode for a sandbox with NO domains (`none` = no route out) |
| `SANDBAR_DOCKER_EGRESS_POOL` | `10.213.0.0/16` | the IPv4 range each sandbox with domains takes its own INTERNAL /28 network from (`sandbar-net-<id>`, made with `com.docker.network.bridge.inhibit_ipv4=true` and checked: no gateway, so no host address). A /16 holds 4096; a block another network holds is skipped. Needs an Engine whose such networks report no gateway (verified on OrbStack's Engine 29.4): with `SANDBAR_EGRESS=on`, sandbard proves it at boot and refuses an older one with a message saying to upgrade it |
| `SANDBAR_DOCKER_PIDS_LIMIT` | `4096` | processes and threads per sandbox (its cgroup's cap; `0` = no cap) |
| `SANDBAR_DOCKER_NOFILE` | `65536` | open files per process (`RLIMIT_NOFILE`; `0` = the Engine's default) |
| `SANDBAR_DOCKER_NPROC` | *(unset)* | `RLIMIT_NPROC`. Left unset on purpose: the kernel counts it per uid across the whole host, and every sandbox's workload runs as uid 1000, so one busy sandbox would spend every other's share. The process cap above is the per-sandbox bound |
| `SANDBAR_DOCKER_READONLY_ROOTFS` | `off` | `on` makes the image's filesystem read-only; the writable paths are then `SANDBAR_DOCKER_TMPFS`. The Engine's archive routes cannot see into a tmpfs, so a system write and the archive routes cannot reach below one (an agent's write and a file read run inside the sandbox and can): use it only for workloads that do not take files that way |
| `SANDBAR_DOCKER_TMPFS` | *(none; `/tmp` when the rootfs is read-only)* | tmpfs mounts, space-separated `path:options` entries, e.g. `/tmp:rw,nosuid,size=1g /run:size=64m` |
| `SANDBAR_DOCKER_STORAGE_SIZE` | *(none)* | cap on a sandbox's writable layer, e.g. `20G`. Needs a storage driver that supports one (overlay2 on xfs with `pquota`, btrfs, zfs); on any other the Engine refuses every create |
| `SANDBAR_DOCKER_RUNTIME` | *(the Engine's, runc)* | the OCI runtime, e.g. `runsc` (gVisor: a user-space kernel per sandbox), once installed and registered with the Engine |
| `SANDBAR_RELAY_CONTAINER` | `sandbar-egress` | the relay container (`sandbard relay`) sandbard joins to each of those networks; with `SANDBAR_EGRESS=on` it must exist at boot |
| `SANDBAR_FC_KERNEL` | *(required for fc)* | guest kernel (vmlinux) path (firecracker is experimental) |
| `SANDBAR_FC_ROOTFS_DIR` | `/var/lib/sandbar/rootfs` | rootfs templates, `<image>.ext4` |
| `SANDBAR_FC_RUN_DIR` | `/var/lib/sandbar/run` | per-VM working state |
| `SANDBAR_FC_BOOT_TIMEOUT_S` | `10` | how long `create` waits for guestd |
| `SANDBAR_EGRESS` | *(off)* | `on` enables the allowlist egress proxy |
| `SANDBAR_EGRESS_ADDR` | `0.0.0.0:7080` | where the proxy listens (the relay reaches it; sandboxes reach only the relay) |
| `SANDBAR_EGRESS_URL` | `http://sandbar-egress:7080` | the relay as a sandbox reaches it (its host is the name the relay joins each network under) |
| `SANDBAR_EGRESS_CONNECT_PORTS` | `443,80` | the ports a `CONNECT` tunnel may reach, a comma list or `*` for any; a tunnel to another port of an allowlisted host is refused and recorded |
| `SANDBAR_RELAY_TO` | *(required, `sandbard relay` only)* | the proxy the relay pipes to, `host:port` |
| `SANDBAR_RELAY_ADDR` | `0.0.0.0:7080` | where the relay listens |
| `SANDBAR_RELAY_MAX_CONNS` | `4096` | concurrent connections the relay will carry |
| `SANDBAR_RELAY_MAX_PER_PEER` | `64` | concurrent connections from one source IP (one sandbox), so no sandbox takes every slot; refusals are logged with their source, once per sandbox per 10 s |
| `SANDBAR_DISPLAY_KEYS` | *(empty = no display route)* | display tickets' Ed25519 public keys, `kid:base64,...` (a malformed entry stops the boot) |

Browser sandboxes: the caller names the image in `spec.image`; Prismoid's worker reads it from
`PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE` (unset ⇒ the worker refuses a browser profile with a
reason). `just sandbar-browser-image` builds and prints it.

## Egress - a controlled way out

Deny-by-default, allowlist per sandbox. Turn it on with `SANDBAR_EGRESS=on`; the daemon runs one
allowlist forward proxy that every backend shares.

- **No `allow_domains`** → the sandbox has **no route out at all** (docker: `network=none`, can't
  even resolve DNS; firecracker: no NIC). Airtight.
- **`allow_domains: ["example.com"]`** → the sandbox joins an internal network with no gateway
  and reaches `example.com` (and subdomains) only through the relay to the proxy; a process
  that ignores `HTTP_PROXY` reaches nothing. An uncredentialed, unregistered client gets a `407` (`Proxy-Authenticate:
  Basic realm="sandbar"`, so a browser-grade client retries with the credential it was given); a
  credentialed request to a host outside its allowlist gets a flat, final `403`, and so does a
  `CONNECT` to a port outside `SANDBAR_EGRESS_CONNECT_PORTS` (default 443 and 80). All are
  recorded; the refusals of a principal nobody registered (a wrong token) are capped, so a client
  cannot grow the proxy's memory by inventing tokens.

```bash
just sandbar-dev &   # the daemon and the relay (see Topology)
T='Authorization: Bearer dev'
curl -s -H "$T" -XPOST localhost:7070/v1/sandboxes \
  -d '{"id":"e","spec":{"image":"curlimages/curl:8.10.1","allow_domains":["example.com"]}}'
# allowed:
curl -s -H "$T" -XPOST localhost:7070/v1/sandboxes/e/exec \
  -d '{"command":"curl -s -o /dev/null -w %{http_code} https://example.com"}'   # → 200
# denied:
curl -s -H "$T" -XPOST localhost:7070/v1/sandboxes/e/exec \
  -d '{"command":"curl -sS https://ifconfig.me"}'   # → curl: (56) CONNECT tunnel failed, 403
curl -s -H "$T" localhost:7070/v1/sandboxes/e/denials
# → {"denials":[{"host":"ifconfig.me","method":"CONNECT","at":"…"}]}
```

**Topology.** A sandbox with domains gets an internal network of its own - a /28 of
`SANDBAR_DOCKER_EGRESS_POOL` that gives the host no address - and sandbard joins the relay to it,
so the relay is its only way out and no other sandbox is ever on its network:

```
sandbox --(sandbar-net-<id>: internal, no gateway, just the two)--> sandbar-egress (relay) --> proxy on sandbard :7080
```

The relay is a TCP pipe (`sandbard relay`) that parses nothing; the proxy still authenticates
and allowlists. sandbard joins none of these networks and publishes no proxy port; it removes a
sandbox's network with the sandbox. Under compose (`deploy/compose.prod.yaml`) the relay is
compose's `sandbar-egress`. On a plain host, with sandbard running on the host itself, an operator
starts the relay (hardened as under compose; `--add-host` lets it reach the proxy on the host):

```bash
docker run -d --name sandbar-egress --restart unless-stopped --user 65534:65534 --read-only --cap-drop ALL \
  --network bridge --add-host host.docker.internal:host-gateway \
  -e SANDBAR_RELAY_TO=host.docker.internal:7080 <sandbard image> relay
```

The S2 posture needs a Docker Engine whose `--internal` +
`com.docker.network.bridge.inhibit_ipv4=true` networks report no gateway (verified on OrbStack's
Engine 29.4); on an older engine sandbard refuses to boot and says to upgrade the engine.

**Restarts.** A Docker sandbox outlives sandbard: containers and networks carry their sandbox's
id, a boot adopts them (the record and spec, the network checked again, the relay joined again,
the credential with an EMPTY allowlist until its caller pushes one), and an exit keeps them. A
network left with no sandbox is removed at boot. Firecracker is experimental: its microVMs are
still torn down at exit.

**How it works.** The proxy authorises the *destination*, not the payload: HTTPS is a `CONNECT`
tunnel it checks-then-blindly-relays (it never sees your TLS plaintext), and closes once neither
side has sent a byte for 5 minutes; plain HTTP it checks and forwards, without the hop-by-hop
headers (the proxy credential among them). Allowlists are keyed by a **per-sandbox credential** the backend hands the workload as
its proxy password (`HTTPS_PROXY=http://sb:<token>@proxy`), so the same token identifies the
sandbox no matter what source IP the network presents - portable across Docker Desktop, a Linux
bridge, and a firecracker tap. Domain matching is dot-anchored: `example.com` allows
`api.example.com`, never `evil-example.com`.

**Backends.** The **docker** backend enforces this today (working, tested end-to-end). The
**firecracker** backend refuses `allow_domains` for now (a v1 microVM has no NIC - it says so
rather than run airtight and fail your fetch silently); the NIC + tap design is in
[docs/firecracker.md](docs/firecracker.md).

## Security posture

- **Isolation**: docker (shipped) = a container on the host's shared kernel: non-root workload,
  `CapDrop: ALL`, `no-new-privileges`, a cgroup process cap, an open-file cap, memory with no swap
  on top, and the Engine's init as pid 1 so orphans are reaped. A shared kernel is a shared
  kernel: a kernel exploit escapes it. For untrusted tenants, give sandbar a host of its own and
  set `SANDBAR_DOCKER_RUNTIME=runsc` (gVisor) where installed. firecracker (experimental) = own
  kernel per sandbox.
- **Network**: deny-by-default (above). Off, a sandbox has no route out; on, it gets an internal
  network of its own with no gateway and reaches only its allowlist through the relay - never
  another sandbox - and every refusal under its current list is on `/denials`.
- **Auth**: one bearer token, compared in constant time; private-network deployment. Not a
  multi-tenant auth system. With no token, sandbard boots only on a loopback address. The
  display route is the one exception: it takes only a short-lived, single-use display ticket
  signed by the caller's key (sandbar holds only the public half), never the token, and no other
  route takes a ticket. The egress proxy is a *separate* listener (`SANDBAR_EGRESS_ADDR`, default `:7080`) that sandboxes
  reach only through the relay, deny-by-default for any principal it does not know. Behind the
  relay it sees one source address, so the per-sandbox token is what names a sandbox (an IP
  principal would name the relay).
- **Path traversal**: the file routes take only clean absolute paths, and tar extraction refuses
  entries that escape the destination. On docker an agent's file write and every file read run
  inside the sandbox as uid 1000, so a link the workload planted carries them nowhere the workload
  could not reach itself;
  a system write (the Engine's, as root) refuses a parent with a symbolic link in it. Inside a
  microVM the same rule holds: the guest agent (root) performs an agent write and every read on
  a thread holding uid 1000's credentials (never as root), so the guest's permissions refuse a
  root-owned or `/browser` path (`403` code `forbidden_path`), and it opens every path it writes
  without following a symlink, so a link the workload planted never turns a write into one
  elsewhere; a read follows a link only as uid 1000 could, never a `/proc` magic link.
- **Resource limits**: per-sandbox cpu, memory (no swap), process and open-file caps; an exec is
  killed at its `timeout_s` (exit 124) and keeps at most 16 MiB of each of stdout and stderr
  (`truncated: true` past it); single-file writes and reads bounded at 512 MiB, with at most four
  large writes held in memory at once; JSON request bodies bounded at 16 MiB.

## Testing

```bash
go test ./...                     # pure + played-Engine tests always run; the Docker tier runs if a socket is present
just sandbar-image                # builds sandbard:dev (the daemon AND the relay) and prints the export
SANDBAR_RELAY_IMAGE=sandbard:dev go test ./internal/backend/docker/   # + the live egress tests
```

Three tiers:

- **Pure and played-Engine** - core, api, relay, egress, and the docker backend against a played
  Engine API (a sandbox's network: the pool, the refusals, the relay, adoption; the create body).
  No Docker needed. CI runs these on
  every push (the `sandbar` job in `.github/workflows/ci.yml`, with
  `SANDBAR_DOCKER_SOCK=/nonexistent` so the tiers below skip there).
- **A Docker socket** - the docker backend's real-Engine tests (round trip, airtight, the boot
  check on this Engine, probe 5 against a host service, no IPv6 / default route / outside DNS for
  a sandbox with domains, the file door: an agent write refused where uid 1000 may not write or
  through a link into a closed folder, every kind of system write, a checkpoint round trip, a read
  of the browser profile refused, a read over the bound refused unread, a 50 MiB round trip, root's
  commands found only in the system's folders). They use the socket at `SANDBAR_DOCKER_SOCK` (default
  `/var/run/docker.sock`) and skip, not fail, without one - a mock would test nothing, since
  talking to Docker correctly is the backend's whole job.
- **A socket and the relay image** - the live egress tests (probes 1-4 through the relay, two
  sandboxes that cannot reach each other, a restart that adopts, the live allowlist update, the
  browser through the proxy) start `sandbard relay` from
  `SANDBAR_RELAY_IMAGE`; **without it they skip**. The browser test also needs
  `SANDBAR_BROWSER_IMAGE` (`just sandbar-browser-image`). Their proxy listens where the relay
  reaches this host: loopback on Docker Desktop and OrbStack, the default bridge's gateway on a
  Linux Engine (host-gateway), so the tier runs on a stock Docker CE host too.

Pass `-run` when you target a few; the tests remove the `sbtest*` containers and the networks they make (from a 10.214.x.0/24 pool, clear of a live sandbard's default).

## Roadmap

The honest edges, in rough priority:

1. **Egress** - a virtio-net device + a host-side allowlist proxy, so `allow_domains` becomes
   real. Today a firecracker sandbox is fully offline.
2. **Snapshot/restore of the microVM itself** - Firecracker's pause + memory snapshot for
   ~sub-second warm starts (the E2B/CodeSandbox pattern). Today `archive`/`extract` snapshot the
   *filesystem*, which is enough for durable-execution resume but not for fast warm pools.
3. **Reflink rootfs copies** - clone templates with `FICLONE`/copy-on-write on XFS/Btrfs instead
   of a byte copy, so a new VM's disk is near-instant (the blockdiff pattern).
4. **A warm pool** - pre-booted VMs handed out on create, the single biggest cold-start win.
5. **A guest metrics channel** - cpu/mem/io per sandbox over a second vsock port.

Each is additive behind the existing interfaces; none changes the API a caller sees.

## License

Part of the Prismoid repository. Extractable - no Prismoid imports, no Prismoid assumptions.
