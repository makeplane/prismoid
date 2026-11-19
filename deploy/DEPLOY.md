# Deploying Prismoid

The whole product runs on **any Docker host** with one command, and scales to any cloud by
pointing four config values at bigger managed versions of the same four things. Nothing here is
tied to a vendor you cannot self-host (ADR-012).

## The mental model - four ports and one rule

Everything the system needs is one of four commodities, each with many interchangeable
implementations:

| Port | What it is | Bare box | PaaS | Sovereign / on-prem | Hyperscale |
|---|---|---|---|---|---|
| **SQL database** | the record + the job queue (DBOS is on Postgres - no separate queue) | Postgres container | managed Postgres | your Postgres / Patroni | managed + read replicas |
| **Object store (S3 API)** | artifacts & inputs, keyed by content hash | Silo container (a MinIO fork, ADR-083) | R2 / provider bucket | Silo / Ceph | S3 / GCS |
| **Sandbox host** | isolated compute for agent code | sandbard + Docker | their runtime | your Linux + sandbard (Docker backend; Firecracker is experimental) | same |
| **Model endpoints** | LLM inference over HTTP | vendor key | vendor key | self-hosted vLLM/Ollama | vendor or self-hosted |

**The rule that makes it portable: the app reaches all four only through a URL and a credential
in `deploy/.env`.** Swap the URL, you have swapped the provider - no file in this repo changes.

## Quickstart - one box

**Production box, start to finish: `deploy/PROVISION.md`** (a ~30-minute runbook: OS, Docker,
firewall with the docker-bypasses-ufw caveat handled, secrets, DNS, first deploy, releases,
backups). The short version:

```bash
cp deploy/.env.example deploy/.env          # fill it - every ${VAR:?} names its generation command
docker build -t prismoid-frame:prod backend/prismoid/ai/frames/image   # the sandbox rootfs image
docker build -t prismoid-frame-browser:prod --build-arg BASE_IMAGE=prismoid-frame:prod backend/prismoid/ai/frames/image_browser   # the browser profile
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env up --build -d
```

Then browse **https://app.DOMAIN**, with the API at **https://api.DOMAIN** and presigned
downloads at **https://s3.DOMAIN** - Caddy (`deploy/Caddyfile`) is the only thing published to
the internet, terminating TLS with the SSE rules from `docs/runbooks/ingress.md` built in. The
object store's console binds loopback (SSH-tunnel to reach it).

No DNS yet (a VM rehearsal)? Set `CADDY_EXTRA_GLOBAL=local_certs` in `deploy/.env` and point
the three names at the VM in `/etc/hosts` - Caddy signs them from its local CA.

That command stands up: Postgres, Silo (the object store, + bucket init), a one-shot migration, **sandbard** (the
sandbox daemon), **sandbar-egress** (`sandbard relay`, the one door to sandbard's egress proxy,
which sandbard joins to the internal network it makes for each frame with domains), **Caddy**, and
the **API + worker + web**. The worker runs frames through sandbar (`frame_driver=sandbar`) - it
never touches Docker itself; sandbard owns the socket and starts each frame as a container, and
enforces per-frame egress allowlists. **Those containers share the host's kernel**: the network
and capability limits are real, but a kernel exploit from model-written code is a host compromise,
so run this for yourself or people you trust. gVisor (`SANDBAR_DOCKER_RUNTIME=runsc`) is optional
hardening, unproven here; own-kernel isolation is the experimental Firecracker backend (below).
On AWS, see PROVISION.md's "On AWS (EC2)". Those networks need a Docker Engine that meets the floor (below).

### The one prerequisite: the frame image

`sandbard` launches a container per frame from a **rootfs image** carrying the document/spreadsheet
toolchain and the `/task /work /artifacts` layout. Build it once (the `prismoid-frame:prod` above);
`FRAME_IMAGE` in `.env` selects it. It is a normal Docker image on the host - sandbard uses it
through the mounted socket.

### First run: add your models (or the first task fails)

A fresh deployment's model catalog is **empty on purpose** - which models you run and what they
cost is your decision, not something baked into a deploy (§9). Until you add one, a task plans and
then fails with *"no enabled model serves 'frontier-reasoner'."* Seed the starter set:

```bash
just deploy-add-models        # or: docker compose … exec worker prismoid models seed
```

That adds a small Anthropic + Google catalog (a frontier reasoner, a fast executor, and a
different-family judge - cross-family verification needs one). It is idempotent, and each model
needs its provider key in `.env`. **Verify the prices** (`backend/prismoid/runner/default_models.yaml`,
captured 2026-07) against your provider, or run your own: `prismoid models seed --file yours.yaml`,
or add them one at a time with `prismoid models add …`.

## The config contract (`deploy/.env`)

| Value | Meaning | Single box | Real deploy |
|---|---|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | the model muscle (≥1, or self-host) | your key | your key or a vLLM URL via `PRISMOID_GATEWAY_*` |
| `POSTGRES_PASSWORD` · `MINIO_USER` · `MINIO_PASSWORD` · `SANDBAR_TOKEN` | secrets (the passwords and the token are required: compose refuses to start without them; `MINIO_USER` defaults to `prismoid`) | generate them (`.env.example`) | from your secret manager |
| `API_PUBLIC_URL` | the URL a **browser** uses to reach the API (baked into the web bundle) | `http://localhost:8100` | `https://api.example.com` |
| `WEB_ORIGIN` | the web's origin, for CORS | `http://localhost:8080` | `https://app.example.com` |
| `S3_PUBLIC_ENDPOINT` | where a browser fetches a presigned download | `http://localhost:9000` | your public object-store URL |
| `FRAME_IMAGE` | the sandbox rootfs image | `prismoid-frame:prod` | same |
| `DOCKER_GID` | the Docker socket's group id: the sandbard image runs unprivileged and reaches the Engine through this group (compose `group_add`) | `deploy.sh` pins it (`stat -c %g /var/run/docker.sock`) | Kubernetes: the same id in the pod's `securityContext.supplementalGroups` |
| `SCREEN_SIGNING_KEY` · `SANDBAR_DISPLAY_KEYS` · `SANDBAR_SCREEN` | the live screen (ADR-065): the key that signs screen and display tickets (required), its public half for sandbard (required), and `true` to give a sandbar Machine a screen | required / required / `true` | from your secret manager; see deploy/.env.example |

`API_PUBLIC_URL` and `S3_PUBLIC_ENDPOINT` are the only subtle ones: they must be the addresses a
**browser** can reach, not the in-compose service names - the API URL is compiled into the SPA at
build time, and download links are signed for the public store endpoint.

## Recipe: a PaaS (Railway / Render / Fly)

Don't run the datastores yourself - rent them, and deploy three services from this repo:

1. **Managed Postgres** and an **object bucket** (the platform's, or Cloudflare R2). Copy their
   URLs/keys into the app's env.
2. **API** and **worker** - both from `backend/Dockerfile` (one image, two commands:
   `run api` / `run worker`). Set the four ports' env to the managed services.
3. **web** - `web/Dockerfile` (build context: the repo root), with `VITE_PRISMOID_API_URL` = the API's public URL.
4. **sandbard** - `sandbar/Dockerfile`. On a PaaS that gives each service the Docker socket it can
   use the docker backend; if not, run sandbard on a small VM you control (below) and point
   `PRISMOID_WORKER_SANDBAR_URL` at it. With egress on (the worker always asks for domains), an
   egress-on sandbard also needs an internal network made with
   `com.docker.network.bridge.inhibit_ipv4=true` and the relay (`sandbard relay`, the same image)
   on it and on sandbard's network, or it exits at boot (sandbar README, Egress).

Migrations: run `alembic upgrade head && prismoid skills seed` once as a release step (the
`migrate` service shows the exact command).

## Recipe: sovereign / on-prem, with real microVM isolation (experimental)

The Firecracker backend is experimental and not supported for production. For untrusted-code
isolation without renting it, you can try running **sandbard with its Firecracker backend** on
a Linux/KVM host - own kernel per frame. This is a **host service, not a container** (a microVM
needs `/dev/kvm` and host networking):

1. Build a rootfs with `sandbar/scripts/build-rootfs.sh` and get a Firecracker `vmlinux`
   (see `sandbar/docs/firecracker.md`).
2. Run sandbard as a systemd unit:
   ```
   SANDBAR_TOKEN=… SANDBAR_BACKENDS=firecracker SANDBAR_BACKEND=firecracker \
   SANDBAR_FC_KERNEL=/var/lib/sandbar/vmlinux sandbard
   ```
3. Point the worker at it: `PRISMOID_WORKER_SANDBAR_BACKEND=firecracker`,
   `PRISMOID_WORKER_SANDBAR_URL=http://<host>:7070`.

Everything else - Postgres, Silo/Ceph, API, worker, web - runs from the same images on your own
machines. Nothing leaves your walls. (Firecracker egress - a NIC + tap routed to sandbar's proxy -
is on the roadmap; until then a firecracker frame runs with no network, and `allow_domains` is
refused with a clear error rather than run airtight.)

## Upgrading to the S2 frame network

The release that ships ADR-063 gate S2 moves sandboxes with domains off the default bridge: each
gets an `--internal` network of its own (`sandbar-net-<id>`, a /28 of `SANDBAR_DOCKER_EGRESS_POOL`,
default `10.213.0.0/16`, made with `com.docker.network.bridge.inhibit_ipv4=true`) that sandbard
creates, checks and removes, and reaches sandbard's egress proxy only through `sandbar-egress`
(`sandbard relay`), which sandbard joins to that network. No two sandboxes share a network, so no
sandbox reaches another, of any workspace. sandbard no longer publishes 7080 on the host, and a restart
adopts the sandboxes it made instead of removing them. Nothing is done by hand.

- **The floor.** The posture needs a Docker Engine whose `--internal` + inhibit_ipv4 networks
  report no gateway in IPAM (verified on OrbStack's Engine 29.4). Below it sandbard refuses to
  boot (it proves the floor on a throwaway network from its pool), and the worker, which waits for
  a healthy sandbard, never starts: every task stops, not only frames.
- **The pool.** Pick `SANDBAR_DOCKER_EGRESS_POOL` so it overlaps nothing the host routes (a VPC, a
  VPN, another compose project); a block another network holds is skipped, never fatal. A /16 holds
  4096 sandboxes with domains; the relay joins each network (measured on Engine 29.4: about 50 ms a
  join at 300 networks, 90 ms at 500, 1000 joined without error).
- **The pre-flight.** `deploy/deploy.sh` checks the floor before it pins or stops anything: it
  creates a throwaway `--internal -o com.docker.network.bridge.inhibit_ipv4=true` network, reads
  its IPAM, and removes it. If creation fails or a gateway shows, the deploy stops with
  `DEPLOY REFUSED: Docker Engine <version> is below the frame network's floor` and the running
  release keeps serving. Upgrade Docker Engine on the host (`docker version`), then re-run the deploy.
- **Check a box by hand** before the first S2 deploy:
  ```bash
  docker version -f '{{.Server.Version}}'
  docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true floor_check
  docker network inspect floor_check -f '{{json .IPAM.Config}}'   # no "Gateway" key = the floor is met
  docker network rm floor_check
  ```
- **After the deploy**, `docker network ls --filter label=sandbar.id` lists one network per
  sandbox with domains, each `docker network inspect <name> -f '{{json .Internal}} {{json .IPAM.Config}}'`
  shows `true` and no `Gateway`, and `docker ps -a --filter name=^/sandbar- --filter network=bridge`
  is empty (a sandbox an older daemon left behind would still have a direct route out: `docker rm -f` it).
- **From a release with the shared `sandbar_frame_net`**: the old sandbard removes its sandboxes as
  it stops (the last deploy that does), so the new one has none to adopt; then remove the old
  network, `docker network rm sandbar_frame_net`.
- **Restarts and deploys keep sandboxes.** sandbard adopts every container it labelled at boot -
  the record, the network, the relay joined again, the credential with an empty allowlist until
  the worker's next push - and keeps them at exit, so a deploy costs no warm Machine its work. Its
  API answers only after adoption (compose's `start_period: 5m`). A recreated relay has left every
  network: compose restarts sandbard with it (`depends_on: restart: true`), and the boot joins it
  again.

## Production notes

- **TLS / ingress**: shipped - `deploy/Caddyfile` terminates TLS for the three hostnames with
  the SSE rules made real: h2, no compression or buffering on the api host, idle timeouts above
  the 15 s heartbeat, and `token=` / `X-Amz-Signature` redacted from its own access logs. Set
  `API_PUBLIC_URL`/`WEB_ORIGIN`/`S3_PUBLIC_ENDPOINT` to the https URLs (compose refuses to
  start without them).
- **Browser hardening**: the app and api hosts send HSTS (one year, no preload), `nosniff`,
  `X-Frame-Options: DENY`, a Referrer-Policy and a Content-Security-Policy; the Caddyfile explains
  each CSP directive against what the SPA loads. The app's policy allows the object store only at
  `S3_PUBLIC_ENDPOINT` (Caddy reads it from the environment): a provider that signs
  virtual-hosted URLs (`bucket.endpoint`) needs that host added to the policy. The CSP keeps
  `script-src 'unsafe-inline'` because artifact page previews are sandboxed `srcdoc` iframes, which
  inherit the page's policy and run their own inline scripts. Request bodies are capped at the
  ingress: 100 MB on the api host (a task with ten attached files is about 70 MB), 1 MB on the
  app host.
- **Secrets**: no dev-grade defaults remain - every secret is `${VAR:?}` in the compose file and
  documented with its generation command in `.env.example`, including the Postgres password
  inside the default database URLs and the MinIO password behind the default S3 secret key, so
  a `deploy/.env` that misses one fails `docker compose config` instead of booting on a
  credential printed in this repository. `SIGNING_PRIVATE_KEY` and
  `CONNECTORS_ENCRYPTION_KEY` are load-bearing across redeploys: losing them invalidates prior
  verification signatures and orphans stored connector tokens.
- **Deploys**: by hand on the box - `git fetch origin && git checkout --detach <sha>`, then
  `deploy/deploy.sh <sha>`: it refuses a checkout that is not exactly that sha, pins it, builds
  the frame image and the stack's images on the box (tagged with the sha), stops the worker,
  migrates, brings everything up and health-gates. Stop-start on a single worker is safe by
  design (chaos-drill-proven; ADR-027 pins `PRISMOID_WORKER_APP_VERSION` to the release sha).
  Before anything stops, the script proves the Docker Engine meets the frame network's floor
  (above), and it calls a deploy done only on a worker beat newer than the moment the old worker
  stopped. Rollback = the same two lines with the previous sha (its images are still on the box,
  so the build is a cache hit; prune old releases' images by hand). Deploys that must not cancel
  in-flight work: `deploy/deploy.sh <sha> --wait-idle`. No workflow deploys a box.
- **The API and its workers run ONE version** (ADR-027 §1, amended): the API stamps
  `PRISMOID_WORKER_APP_VERSION` on every turn it enqueues and a worker takes only its own
  version's, so a worker of an older release (one without the Project memory fence, say) never
  runs a turn the new API started. Both read the same value from the shared env; the API warns
  at boot (`version skew`) when the live worker's beat names another. Stop-start needs nothing
  more: the new worker's boot sweep moves the old version's never-started jobs (a turn, a chat
  reply, a bot draft) onto the new version, same id and inputs, and the task sweeper backs up the
  turns. A **rolling** deploy (several workers,
  `PRISMOID_WORKER_REAP_VERSION_GHOSTS=false`) has no boot sweep, so the order is the safety:
  roll the API first (new turns now wait for a new-version worker), start the new workers, let
  the old workers drain the old version's ENQUEUED work - in `dbos.workflow_status`, no row with
  `status = 'ENQUEUED'` and the old `application_version` - and only then retire them. A worker
  retired early strands those turns: nothing of their version is left to take them.
- **Backups**: `deploy/backup/` - nightly restic snapshot of BOTH databases (`prismoid` and
  DBOS's `prismoid_dbos_sys`) + the MinIO volume to off-host storage, 7d/4w/3m retention; the
  plaintext dumps are deleted from the box once restic holds them. A monthly restore drill
  restores both databases into scratch databases and reads a sample of MinIO files back from the
  snapshot, comparing each with the live volume. The checkout path, compose project, MinIO volume
  and staging directory are `PRISMOID_SRV`, `PRISMOID_COMPOSE_PROJECT`, `PRISMOID_MINIO_VOLUME` and
  `PRISMOID_BACKUP_DIR` in `/etc/prismoid-backup.env` (defaults: `/srv/prismoid`, `prismoid`,
  `prismoid_miniodata`, `/var/backups/prismoid`). Install per PROVISION.md §9.
- **Scale**: one API + one worker carries a lot (ADR-009). Add API replicas behind the proxy
  first; a second worker needs a unique `PRISMOID_WORKER_EXECUTOR_ID` and the two-worker chaos
  drill. The datastores scale independently - that is the whole point of the four ports.
- **Health**: bare `GET /healthz` says only ok/degraded (what a load balancer can act on);
  `GET /healthz/full` - behind any Actor - carries the migration revision, worker pulse, and
  queue vitals.

## The operator console (admin.DOMAIN)

The admin surface is a SEPARATE application built from the same `web/Dockerfile` (repo-root context)
(`APP_TARGET=admin`, see `web/Dockerfile`): its own bundle, its own container (`web-admin` in
`compose.prod.yaml`), its own hostname. The public web image ships none of it. The login door
refuses non-admin accounts outright; `AdminDep` on every `/v1/admin/*` route remains the lock
underneath.

Bring-up, a mirror of the app hostname:

1. DNS: `admin.DOMAIN` → the box, DNS-only (Caddy terminates TLS itself, like `app.`/`api.`).
2. `deploy/.env`: set `ADMIN_ORIGIN=https://admin.DOMAIN` (it joins the API's CORS allowlist).
3. `deploy/deploy.sh <sha>` - the ordinary deploy builds `web-admin` beside the rest; the
   `admin.{$DOMAIN}` vhost is already in both Caddyfiles, and Caddy fetches the cert on the
   first request.
4. Prove it: `curl -sI https://admin.DOMAIN` → 200; sign in with an admin account; a non-admin
   account is refused at the login with the scope message.

**Fencing it to a tailnet:** the vhost ships answering publicly, with auth as the only gate. To
fence it, in the active Caddyfile's `admin.{$DOMAIN}` block uncomment the
`@tailnet remote_ip 100.64.0.0/10` matcher pair and the `respond 404` fallback, delete the bare
`reverse_proxy` line, and add the same fence on the **api** vhost for the admin API:

    @admin_api path /v1/admin/*
    @admin_api_public {
        path /v1/admin/*
        not remote_ip 100.64.0.0/10
    }
    respond @admin_api_public "not found" 404

Then `docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec caddy caddy reload
--config /etc/caddy/Caddyfile`. Tailscale must run ON THE BOX (`tailscale up`) so tailnet traffic
arrives with a 100.64.0.0/10 source address; operators browse the same `admin.` hostname, resolved
to the box's tailnet IP (a split-DNS record or a MagicDNS alias) once the public path answers 404.
