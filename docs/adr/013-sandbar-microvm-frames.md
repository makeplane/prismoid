# ADR-013 — sandbar: microVM frames as an independent subproject

*Status: accepted · July 2026 · executes ADR-012 risk #1 (isolation gap) and Phase C2 · new
dependency (Go subproject + `golang.org/x/sys`) per the §3 rule*

## Context

Frames — the sandbox an agent works inside (§10) — run in Docker containers today. A container
shares the host kernel, so a kernel exploit from model-generated code is a host compromise and
every co-tenant frame falls with it. ADR-012 named this the **first** thing to fix on the way to
multi-tenant cloud, and the unanimous industry position (E2B, Fly, Cloudflare, AWS Lambda
MicroVMs, Anthropic's and OpenAI's own code execution): run untrusted model code in a **microVM**
(own kernel, KVM-isolated) or a userspace-kernel sandbox (gVisor), never a shared-kernel
container.

Three ways to close it: (a) buy a SaaS sandbox (E2B/Modal), (b) embed Firecracker orchestration
into the Python worker, (c) build a small, standalone sandbox daemon behind the existing
`FrameDriver` seam. The owner's directive was explicit — an **independent, well-documented,
reusable** sandbox project, in any language, that interfaces with Plane at arm's length and can
serve other projects.

## Decision

Build **sandbar**: a standalone sandbox daemon in the repo (`sandbar/`, Go), with no Plane
imports and no Plane knowledge, that Plane talks to over HTTP.

- **Language: Go.** Firecracker orchestration is process + unix-socket + vsock work; Go's
  goroutines and stdlib `net/http` make the daemon small and dependency-free (stdlib only, plus
  `golang.org/x/sys` for the guest's `AF_VSOCK`). A single static binary is trivial to deploy
  beside the worker or on its own fleet.
- **Two backends behind one `core.Backend` interface:** `docker` (dev / trusted; runs on macOS)
  and `firecracker` (prod / untrusted; Linux+KVM). They are wire-identical, so you develop
  against docker and deploy against firecracker with no code change. A firecracker backend on a
  host without `/dev/kvm` **refuses to start** rather than degrade to no isolation.
- **HTTP is the only coupling.** Plane's `SandbarFrame` (`ai/frames/sandbar_driver.py`)
  implements the existing `FrameDriver` protocol over sandbar's REST API. A `new_frame()` factory
  (`ai/frames/__init__.py`) chooses `docker`|`sandbar` from `settings.worker.frame_driver` — the
  one seam; the five call sites (execute, verify, reap, discard, dev) never name a backend.
- **`publish` stays a Plane concept.** sandbar's interface is exec + files + tar + destroy — all a
  sandbox *is*. The one harvest door (Rule 9) and the manifest are composed in the Python driver
  from `exec`, byte-for-byte the same script the Docker driver runs. A sandbox does not know what
  an artifact is; the platform does.
- **Ephemeral by construction.** sandbar's registry is an in-memory map; durable truth stays
  upstream (Postgres + CAS-S3). A frame is always rebuildable from those, which is exactly what
  keeps DBOS replay sound — and matches ADR-012's "provider state is cache, never truth"
  invariant.

## Why not the alternatives

- **E2B / Modal (SaaS):** the sandbox is the platform's core security boundary; renting it means
  a third party runs our tenants' untrusted code, and couples our uptime and data-egress story to
  theirs. sandbar is self-hostable and ours. (E2B remains a credible *operational* fallback behind
  the same `FrameDriver` seam if we ever choose it — the seam is the point.)
- **Firecracker in the Python worker:** would fuse VM orchestration into the durable-execution
  process, put a Linux-only concern in the middle of a cross-platform codebase, and make the
  sandbox un-reusable. A daemon with an HTTP seam keeps the worker portable and the sandbox
  independent.
- **gVisor instead of Firecracker:** a legitimate second backend (Modal and Anthropic use it);
  10–30% syscall overhead vs a full VM's stronger boundary. Left as a future `core.Backend` — the
  interface already admits it.

## Consequences

- **Isolation gap closes** on `frame_driver=sandbar` with the firecracker backend: own kernel per
  frame. Docker stays the default until the rootfs/kernel pipeline and a warm pool are
  production-ready.
- **A new build artifact**: the Go module and two binaries (`sandbard`, `sandbar-guestd`). `just`
  recipes build and test it; `go test ./...` is green without Docker (integration tests skip).
- **A documented, reusable asset**: `sandbar/README.md` + `docs/{api,firecracker}.md` carry the
  architecture, the security model, and the accumulated Firecracker knowledge, so the project
  stands on its own for other uses.
- **Open edges** (roadmap, in the README): egress (v1 VMs have no NIC — `allow_domains` is
  carried but unenforced), microVM memory-snapshot warm starts, reflink rootfs copies, jailer
  hardening. Each is additive behind the interfaces.

## Verification

Proven end-to-end on macOS with the docker backend: Python `SandbarFrame` → HTTP → Go `sandbard`
→ Docker → container, exercising create (~186 ms), exec (exit codes, stdout/stderr), binary-safe
read, root-444 system files vs uid-1000 agent files, dir listing, tar snapshot→wipe→restore,
`publish` + manifest against the real `computer-frame` image, reattach, and destroy — every
`FrameDriver` method green. The firecracker backend's host preflight, VM config, and vsock
handshake are unit-covered; the full microVM boot requires a Linux/KVM host (`docs/firecracker.md`).
