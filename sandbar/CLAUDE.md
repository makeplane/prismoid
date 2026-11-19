# sandbar - folder rules
Scope: `sandbar/` (its own Go module). Read with the root CLAUDE.md (it wins on conflict),
docs/engineering.md and `sandbar/README.md` (the design, the API table, the security posture and
the test tiers).

## What this folder is for
A standalone sandbox daemon (ADR-013): run untrusted code in a Firecracker microVM (or a Docker
container for dev and trusted work) and drive it over HTTP. `internal/core` is the contract
(`Spec`, `Instance`, `Backend`, `Registry`, ids); `internal/api` the HTTP surface and its error
mapping; `internal/backend/{docker,firecracker}` the isolation technologies; `internal/egress`
and `internal/relay` the controlled way out; `internal/ticket` the display ticket;
`internal/guestwire` the host-to-guest protocol; `cmd/` the binaries. It has no Plane or Prismoid
knowledge: Prismoid is one client of its HTTP API, never linked in.

## Do
- Keep `internal/core` a leaf: the standard library only, no backend, no HTTP. Image paths that
  sit in `core` today are planned to move to boot config; add none.
- Keep the API layer speaking only `core` types, importing no backend; a backend implements
  `Backend` and `Instance` and knows nothing about HTTP. A new isolation technology is one new
  package.
- Express a capability only some backends have as an optional interface in `core` that a backend
  either implements fully or leaves out.
- Map errors to status codes in one place, from typed sentinels the core returns (wrapped with
  `%w`); every 404 names which one it is in `error.code`. Three writers exist today;
  new code uses the existing mapping and adds no fourth.
- Inject time and environment (`now`, `getenv`), as `ticket` and `relay` do; read config through
  one `FromEnv(getenv)` shape (planned).
- Keep the docker backend the behavioural reference the firecracker backend matches on the wire.
- Stay ephemeral: the registry is in memory; durable truth lives with the caller; a restarted
  daemon reaps only orphaned real resources.
- Egress is deny-by-default; every refusal lands on `/denials`; a sandbox never reaches another.
- The display route takes only a single-use, short-lived display ticket signed by the caller's
  key (sandbar holds only the public keys, `SANDBAR_DISPLAY_KEYS`), never the master token, and no
  other route takes a ticket.
- Update `sandbar/README.md` and `docs/api.md` in the same PR as a behaviour or route change.

## Never
- Add a Prismoid or Plane concept to the daemon, or cite a Prismoid review id or a Python path in a
  comment; at most one ADR citation per package doc.
- A required interface method that refuses, or an optional interface implemented only to refuse.
- A third copy of "create an exec" or of a private fake engine in a test.
- Grow `internal/backend/docker/docker.go` (850 lines; exec, files and limits already moved
  out): a planned split divides the rest into network, subnets, adopt, instance and engine
  files; new code goes into its own file.
- Change a client-visible status code without saying so in the PR; unifying the status codes is an
  open owner decision.
- Add a dependency or a Go linter without an ADR; the module depends on the standard library
  and `golang.org/x/sys` only.

## Checked by machine
- `just sandbar-check`: `gofmt`, `go vet`, `go build` (and a Linux cross-build), `go test`. The
  Docker tier skips without a socket; the live egress tier also needs `SANDBAR_RELAY_IMAGE`.
- `quality:go.file_lines` (600) and `quality:go.function_lines` (60), test files excluded.
  Baselined: `docker.go` (850 lines), `Backend.Create` (77), `Backend.Adopt` (61),
  `cmd/sandbard/main.go`'s `main` (84), `Server.display` (66).
- `quality:comments.process_refs`: test files included; `docker.go`, `docker_test.go`,
  `cmd/sandbard/main.go` and `main_test.go` carry baselined citations (planned to be removed).

## Write it like this
- `internal/ticket/ticket.go`: a deep module with a two-verb surface (`Peek`, `Redeem`), injected
  time, typed sentinels, the wire format in five lines of package doc.
- `internal/relay/relay.go`: one job; small private types that each hide real state; config from
  injected `getenv`; an unexported peer seam.
- `internal/core/core.go`'s `Spec.Validate` and `SystemPath`: pure checks returning wrapped
  sentinels that the API maps in one place.

## Review focus
- A `core` import of anything outside the standard library, or a Prismoid-specific path or name in
  `core`.
- A new error path that writes its own status instead of going through the one mapping.
- A capability added as a refusing method on every backend.
- An egress or display change that widens who can reach what: a missing denial record, a route
  that accepts the master token where a ticket belongs, a sandbox that can reach another.
- Tar extraction or file writes that can escape the destination, or exceed the 512 MiB bound.
- A behaviour only a real Engine can prove (isolation, adoption, the relay) tested only against
  the played Engine; the socket tier is where it is proven (README, Testing).
