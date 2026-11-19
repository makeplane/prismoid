# Roadmap

What comes next for Prismoid. It names directions, not dates, and any of it can change. When an item
lands it moves into [STATE.md](STATE.md), which says what is true today. To work on one, open an
issue first ([CONTRIBUTING.md](CONTRIBUTING.md)).

## Near term

- **`prismoid up` on Linux.** Local mode (ADR-077) and the `device` driver (ADR-078) are macOS only:
  every agent command on the device runs under a Seatbelt profile, and Linux needs a fence of its
  own before the same one-command start can work there. The embedded Postgres already runs on Linux.
- **Public container images.** Today every image builds on the machine that runs it
  (`docker compose -f deploy/compose.prod.yaml up --build`). Versioned images for the backend, the
  web app, sandbar and the Machine images would turn a server install into a pull.
- **sandbar on more than one host.** A deployment talks to one sandbar daemon
  (`PRISMOID_WORKER_SANDBAR_URL`). Next: Machines spread across several hosts, with a clear "host full"
  answer when one is out of room.
- **gVisor proven for sandbar's Docker backend.** `SANDBAR_DOCKER_RUNTIME=runsc` is documented as
  optional hardening. It becomes a supported setting once Prismoid's whole stack (the browser,
  documents, the screen) is proven on it.
- **The Firecracker backend to shipping.** sandbar's microVM backend gives each sandbox its own
  kernel and speaks the same API as the Docker backend, so a deployment moves to it with no code
  change. It stays experimental until it is proven to the Docker backend's bar
  ([sandbar/docs/firecracker.md](sandbar/docs/firecracker.md)).
- **The whole-desktop screen for self-hosters.** On sandbar the live screen is the browser
  (ADR-065); the web's VNC pane and the relay's control stream wait for a driver that serves one.

## Later

- **The email channel**, once inbound mail carries a sender-authentication verdict (SPF, DKIM,
  DMARC). Until then it is refused (`backend/prismoid/operations/channels.py`).
- **Email verification at signup**, so an operator can open signup on a public server safely
  (`backend/prismoid/conf_auth.py` keeps it closed outside dev until then).
- **More than one worker**, after the two-worker chaos drill passes (ADR-009).
- **The scale valves ADR-009 names, each on its trigger:** a connection pooler at the third API
  replica, monthly `task_events` partitions, and a dedicated pub/sub behind the live-event hub.
- **Metrics for sandbar's daemon**, so an operator sees a host's sandboxes, memory and disk the
  way the API's and worker's spans show theirs.

Ideas and requests are welcome as GitHub issues.
