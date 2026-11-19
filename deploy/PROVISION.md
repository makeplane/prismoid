# Provisioning the production box

One-time, ~30 minutes on a fresh Linux machine (the reference: a Hetzner AX-class
dedicated server, 64 GB, 2×NVMe, Ubuntu 24.04). Everything after step 1 is plain Ubuntu - the
provider only decides who racks the hardware. **On AWS?** Read [On AWS (EC2)](#on-aws-ec2) at the
end: it says what differs and points back to the steps here that do not. When this runbook's first deploy goes green it
closes STATE.md's standing item: *"do a full `just deploy-up` on a clean host."*

## 1. Order + OS

Hetzner Robot → AX-class server → activate **Rescue System** → `installimage` → Ubuntu 24.04,
**software RAID 1** across both NVMe (the default layout; keep `/` on the mirror). Reboot into
the installed system as root.

## 2. Users + SSH

```bash
adduser --disabled-password deploy && usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
# your key (and each operator's) - deploys are run by hand on the box, nothing logs in from CI
cat >> /home/deploy/.ssh/authorized_keys   # paste the public keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys

cat > /etc/ssh/sshd_config.d/hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
systemctl restart ssh
```

Re-login as `deploy` (keep the root session open until you have confirmed it works).

## 3. Updates + Docker

```bash
sudo apt update && sudo apt -y install unattended-upgrades iptables-persistent restic git curl
sudo dpkg-reconfigure -plow unattended-upgrades

# Docker Engine + compose plugin from docker.com (not Ubuntu's), per docs.docker.com/engine/install/ubuntu
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy && newgrp docker

ls /dev/kvm          # must exist - the Firecracker workstream depends on it
docker info | grep -i cgroup   # expect cgroup v2
```

## 4. Firewall - and the Docker caveat, written down

Docker inserts DNAT rules ahead of ufw's INPUT chain, so **ufw never protects a
docker-published port**. The real controls are the bind addresses in `compose.prod.yaml`
(only Caddy publishes on 0.0.0.0; MinIO console on loopback) - ufw covers the host's own
services. Sandbar's egress proxy publishes no port at all: a frame container with domains reaches
it through `sandbar-egress`, the relay sandbard joins to the internal network it makes for that
frame alone (ADR-063). Each such network is created with `internal` and
`com.docker.network.bridge.inhibit_ipv4=true`, so the host has no address on it and no other frame
is on it; the posture needs a Docker Engine whose such networks report no gateway (verified on
OrbStack's Engine 29.4), and on an older engine sandbard refuses to boot and says so -
`deploy/deploy.sh` checks this before it stops anything (DEPLOY.md, "Upgrading to the S2 frame
network").

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw enable
```

## 5. Checkout + secrets

```bash
sudo install -d -o deploy -g deploy /srv/prismoid
git clone https://github.com/makeplane/prismoid.git /srv/prismoid
cd /srv/prismoid && cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
```

Fill `deploy/.env` (every `:?` in compose fails fast if you miss one):

```bash
openssl rand -hex 24        # POSTGRES_PASSWORD
openssl rand -hex 24        # MINIO_PASSWORD
openssl rand -hex 32        # SANDBAR_TOKEN
openssl rand -base64 32     # SIGNING_PRIVATE_KEY (Ed25519 seed)
echo "s1:$(openssl rand -base64 32)"  # SCREEN_SIGNING_KEY (computer use; its public half is SANDBAR_DISPLAY_KEYS, deploy/.env.example)
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # CONNECTORS_ENCRYPTION_KEY
```

Plus: `DOMAIN`, `ACME_EMAIL`, the three public URLs (`API_PUBLIC_URL`/`WEB_ORIGIN`/
`S3_PUBLIC_ENDPOINT` = `https://{api,app,s3}.DOMAIN`), one model provider key, the SMTP relay
(e.g. Resend: `smtp.resend.com` / 587 / `resend` / the API key - the API refuses to boot in
prod without a real mail backend), and `LOGFIRE_TOKEN` (empty = flying blind on purpose).

## 6. DNS

A (and AAAA) records for `app.DOMAIN`, `api.DOMAIN`, `s3.DOMAIN` → the box's IP. **DNS-only /
grey cloud** - no CDN proxying: SSE wants an unbuffered path and ACME http-01 wants a straight
one. TTL 300 while iterating.

## 7. First deploy (built on the box)

```bash
cd /srv/prismoid
docker build -t prismoid-frame:prod backend/prismoid/ai/frames/image   # the sandbox rootfs image
docker build -t prismoid-frame-browser:prod --build-arg BASE_IMAGE=prismoid-frame:prod \
  backend/prismoid/ai/frames/image_browser                         # the browser profile, layered on it
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env up --build -d
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env ps   # everything healthy?
just deploy-add-models        # MANDATORY - the model catalog is empty on purpose (DEPLOY.md)
```

Make the first platform admin ON THE BOX. No web path grants admin, and signup is invite-only
(`PRISMOID_AUTH_SIGNUP_ENABLED=false` in `compose.prod.yaml`), so nobody who finds the fresh API first
can claim the console:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec api \
  prismoid admin bootstrap --email you@example.com --name "Your Name"
```

It prints a one-time set-password code once (it is stored only as a hash and expires like a
reset code; `--new-code` mints another). Open `https://app.DOMAIN/reset-password`, paste the
code and choose a password. Run it for an address that already has an account and it only grants
the admin scope. Everyone else joins by a workspace invite from its Members page, whose
code is the only way to sign up.

Prove it from your laptop, not the box:

```bash
curl -fsS https://api.DOMAIN/healthz                       # {"status":"ok",...} - and nothing else
PRISMOID_CLI_API_URL=https://api.DOMAIN prismoid login             # the admin you just bootstrapped
B=https://api.DOMAIN just smoke                            # real task → artifact, sha-verified
```

Then in a browser: `https://app.DOMAIN` → sign in → run a task → watch the live stream → open
the artifact (a presigned `s3.DOMAIN` URL). Confirm the ingress scrubs credentials:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec caddy \
  sh -c 'grep -o "token=[^&\"]*" /data/access-api.log | sort -u'   # → token=REDACTED only
```

## 8. Deploying a release

Every later release is two lines on the box, as `deploy`:

```bash
cd /srv/prismoid && git fetch origin && git checkout --detach <sha>
deploy/deploy.sh <sha>               # --wait-idle to let in-flight work finish first
```

`deploy.sh` refuses a checkout that is not exactly `<sha>`, builds every image on the box from it
(tagged with the sha), then stops the worker, migrates, starts the stack and health-gates
(DEPLOY.md, "Production notes"). Rollback = the same two lines with the previous sha: its images
are still on the box, so the build is a cache hit.

## 9. Backups + alerts

```bash
sudo tee /etc/prismoid-backup.env >/dev/null <<'EOF'
RESTIC_REPOSITORY=sftp:uXXXXX@uXXXXX.your-storagebox.de:/prismoid
RESTIC_PASSWORD=<openssl rand -hex 32 - store it OFF this box too; without it backups are noise>
HEALTHCHECK_URL=https://hc-ping.com/<uuid>
EOF
sudo chmod 600 /etc/prismoid-backup.env
# Storage Box: enable SSH support in Robot; ssh-copy-id the root key restic will use.
sudo restic init
sudo /srv/prismoid/deploy/backup/install.sh
sudo /srv/prismoid/deploy/backup/backup.sh          # force the first one; check `restic snapshots`
```

External probes (free tiers): an uptime monitor on `https://api.DOMAIN/healthz` and
`https://app.DOMAIN` at 1-minute cadence; healthchecks.io catches a backup timer that stops
firing. In Logfire: an alert on error-level spans, then the six conditions in
`docs/runbooks/dashboard.md`.

## 10. Reboot test

`sudo reboot`. The stack must come back by itself (`restart: unless-stopped`), certs must not
re-issue (caddy volume), the frames sandbard made must come back adopted, each on its own network
still internal AND with no gateway - for every name `docker network ls -q --filter label=sandbar.id`
lists, `docker network inspect <name> -f '{{json .Internal}} {{json .IPAM.Config}}'` prints `true`
and a config with no `"Gateway"` - and `https://api.DOMAIN/healthz` must answer within a couple of
minutes.

## On AWS (EC2)

This runbook has not yet been walked end to end on a fresh instance (it will be before the release).

Prismoid runs on one EC2 instance: the same `deploy/compose.prod.yaml` stack, sandbar on its Docker
backend, every image built on the box. No images are published. Steps 5 to 10 above apply
unchanged except where this section says otherwise; read them with this section open.

**What the sandbox is, and is not.** Each Machine is a Docker container sandbar starts on this
instance's Docker Engine. Containers share the instance's kernel, so model-written code that finds
a kernel exploit owns the instance. Run it for yourself or people you trust, on an instance that
holds nothing else (no other workloads, and no IAM role on the instance at all; "Backups to S3"
below uses an IAM user's keys in a file instead).
Own-kernel isolation (Firecracker) is experimental and needs `/dev/kvm`, which ordinary EC2
instances do not have; gVisor (`SANDBAR_DOCKER_RUNTIME=runsc`) is optional hardening that is
documented and unproven here. See `docs/security/threat-model.md`.

### Size it

Each Machine is limited to 2 vCPU and 2048 MB (`resources` in `backend/prismoid/conf_machines.py`, for
both the base and browser profiles); a Machine sleeps after 10 idle minutes (`idle_s`) and a
workspace holds at most 10 warm Machines (`workspace_warm_cap`). The limits are ceilings, not
reservations, and these numbers are not measured under load on EC2: budget the ceiling.

- One concurrent computer task: 2 vCPU and 2 GiB, plus the stack's own base load (Postgres, Silo,
  API, worker, two web servers, Caddy, sandbard), which we estimate at about 4 GiB and 1 vCPU.
- Assumption: up to 4 computer tasks at once. That is 8 vCPU and 8 GiB of Machines plus the base,
  so start at **m7i.2xlarge** (8 vCPU, 32 GiB; an estimate). For up to 2 at once, m7i.xlarge (4 vCPU, 16 GiB)
  is the floor. Tasks that need no computer run on paper in the worker and cost no Machine.
- Builds run on the box too (the images, Chromium included): more RAM keeps them from competing
  with running tasks, and `deploy/deploy.sh <sha> --wait-idle` waits for in-flight work.
- **Root volume: 100 GiB gp3 (an estimate).** Docker's images, every Machine's disk, Postgres and Silo's data
  (artifacts, plus sleeping Machines' `/work` checkpoints, kept 14 days, each up to 2 GiB) all
  live under `/var/lib/docker` on it. The images alone are several GiB, and each release keeps its
  previous sha's images for rollback. EBS grows online: extend it when `df -h /` passes 70%.

### Launch

- AMI: **Ubuntu Server 24.04 LTS**, x86_64. Login user `ubuntu`, with your key pair.
- Security group: inbound **80/tcp and 443/tcp (and 443/udp) from 0.0.0.0/0**, **22/tcp from your
  IP only**, nothing else. Postgres, the Silo console, sandbard and the egress proxy are not
  published (they stay on the compose network or loopback); a rule for them would only expose
  them. The security group is the firewall here: step 4's ufw is optional (Docker bypasses it
  anyway).
- Instance metadata: require IMDSv2 and set the hop limit to 1, so a container cannot reach the
  instance role's credentials (`aws ec2 modify-instance-metadata-options --instance-id <id>
  --http-tokens required --http-put-response-hop-limit 1`). Defence in depth: sandboxes are also
  on networks with no route to the host.
- Allocate an **Elastic IP** and associate it, so DNS survives a stop and start.
- DNS (step 6): A records for `app.DOMAIN`, `api.DOMAIN`, `s3.DOMAIN` to the Elastic IP. In Route 53
  these are plain A records, no CDN in front.

### Do on the instance

Step 2 (users) is already done by the AMI: use `ubuntu` wherever the steps say `deploy`. Then
step 3, with these differences: `sudo usermod -aG docker ubuntu && newgrp docker`; skip the
`/dev/kvm` check (not available, not needed for the Docker backend). Docker Engine must come from
docker.com as step 3 installs it, not Ubuntu's `docker.io`: sandbar gives each Machine an
`--internal` network with no host address, and **sandbard refuses to boot on an Engine that gives
such a network a gateway** (`CheckEgress`; the code names no version, it proves the behaviour on a
throwaway network). Check it before the first deploy, as DEPLOY.md's "Check a box by hand" does:

```bash
docker version -f '{{.Server.Version}}'
docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true floor_check
docker network inspect floor_check -f '{{json .IPAM.Config}}'   # no "Gateway" key = the floor is met
docker network rm floor_check
```

`SANDBAR_DOCKER_EGRESS_POOL` is `10.213.0.0/16` in the compose file; it must overlap nothing the
host routes. A default VPC is `172.31.0.0/16`, so the default is clear, but check `ip route` if you
peer VPCs.

Then step 5's checkout and `deploy/.env`, with the values the compose file refuses to start
without spelled out (names exactly as in `deploy/.env.example`):

```bash
sudo install -d -o ubuntu -g ubuntu /srv/prismoid
git clone https://github.com/makeplane/prismoid.git /srv/prismoid
cd /srv/prismoid && cp deploy/.env.example deploy/.env && chmod 600 deploy/.env

stat -c %g /var/run/docker.sock                      # DOCKER_GID (deploy.sh pins it on every deploy)
echo "s1:$(openssl rand -base64 32)"                 # SCREEN_SIGNING_KEY
KEY="s1:<the value you just generated>"             # SANDBAR_DISPLAY_KEYS is its public half:
echo "${KEY%%:*}:$({ printf '\060\056\002\001\000\060\005\006\003\053\145\160\004\042\004\040'; printf %s "${KEY#*:}" | base64 -d; } | openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64)"
```

Set `DOMAIN`, `ACME_EMAIL`, `API_PUBLIC_URL`, `WEB_ORIGIN`, `S3_PUBLIC_ENDPOINT`, `FRAME_IMAGE`,
`FRAME_BROWSER_IMAGE`, `DOCKER_GID`, `SCREEN_SIGNING_KEY`, `SANDBAR_DISPLAY_KEYS`,
`SANDBAR_SCREEN=true` (the default; `false` gives Machines no live screen), the other secrets from
step 5, one model key and the SMTP relay. A missing screen key or `DOCKER_GID` fails
`docker compose config` with its generation command, never a stack whose screen 503s. Check before
building:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env config -q && echo ok
```

### First deploy

Build the images on the box and bring the stack up (step 7's first block: the frame image, the
browser image, then `up --build -d`). Every later release is `deploy/deploy.sh <sha>` (step 8),
which rebuilds all of them. Then:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec worker prismoid models seed   # MANDATORY: the catalog is empty on purpose
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec api \
  prismoid admin bootstrap --email you@example.com --name "Your Name"
```

`prismoid admin bootstrap` prints a one-time set-password code; open
`https://app.DOMAIN/reset-password` and choose a password (step 7 has the details). Then run
step 7's "Prove it" block, and one task that opens a page: the task card shows a Machine starting
and the Screen pane shows the browser.

### Web search

A task can search the web only when an engine is configured: with none, the worker has no
`web.search` tool at all (ADR-047). `deploy/compose.prod.yaml` passes the five `PRISMOID_SEARCH_*`
lines of `deploy/.env` to the API and the worker (the worker searches; the API's replan writes the
planner's brief, which says whether a step can read the internet), empty by default. Each setting
is one line of `deploy/.env`, there already and empty: fill those lines in (never add a second line
for a name, since compose takes the last one). Pick one of two ways, then run
`deploy/deploy.sh <sha>` as usual.

**A keyed engine** (Brave, Tavily, Exa, Perplexity or Parallel; no extra container). In `deploy/.env`,
fill in:

```
PRISMOID_SEARCH_ENGINES=brave
PRISMOID_SEARCH_KEYS=brave=<your key>
```

`backend/.env.example` lists the engines and the name=value grammar. A comma chain
(`brave,searxng`) falls to the next engine when one fails.

**SearXNG on the box** (no account, no key). In `deploy/.env`, fill in (and add `search` to the
`COMPOSE_PROFILES` line):

```
PRISMOID_SEARCH_ENGINES=searxng
PRISMOID_SEARCH_URLS=searxng=http://searxng:8080
SEARXNG_SECRET=<openssl rand -hex 32>
COMPOSE_PROFILES=minio,search
```

`deploy.sh` sees `searxng` in the chain and starts the `searxng` service (compose profile
`search`) beside the stack, adding `search` to `COMPOSE_PROFILES` if you left it out; a hand-run
compose reads it from `COMPOSE_PROFILES`. Do not pass `--profile search` instead: a profile flag
replaces `COMPOSE_PROFILES`, so Silo (profile `minio`) would not start. It publishes no port: the
worker reaches it at `http://searxng:8080` on the compose network, and `deploy/searxng/settings.yml`
turns on the JSON output the worker reads. The secret comes from `SEARXNG_SECRET`; `deploy.sh`
refuses to run with `searxng` named and the secret empty (an inline comment is no value: a line left
as `SEARXNG_SECRET=   # ...` is empty). Never publish its port or put it behind
Caddy: it is your infrastructure, not a public service.

Check it from the box:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env \
  exec worker python -c "import urllib.request as u; print(u.urlopen('http://searxng:8080/search?q=test&format=json').read(80))"
```

SearXNG asks public search engines on your behalf, so some of them rate-limit a busy box; a keyed
engine is steadier for heavy use.

### Backups to S3

Step 9's restic setup, with an S3 bucket as the repository instead of a Storage Box:

```bash
# From your workstation (or in the S3 console), not the instance - it has no IAM role or aws CLI:
#   aws s3 mb s3://prismoid-backup-<account>-<region> --region <region>   # private; block public access
# replace every <...> before sourcing
sudo tee /etc/prismoid-backup.env >/dev/null <<'EOF'
RESTIC_REPOSITORY=s3:s3.<region>.amazonaws.com/prismoid-backup-<account>-<region>/prismoid
# RESTIC_PASSWORD: generate with `openssl rand -hex 32` and store it OFF this box too; without it backups are noise
RESTIC_PASSWORD=<openssl rand -hex 32 - store it OFF this box too>
AWS_ACCESS_KEY_ID=<an IAM user's key limited to that bucket>
AWS_SECRET_ACCESS_KEY=<its secret>
HEALTHCHECK_URL=https://hc-ping.com/<uuid>
EOF
sudo chmod 600 /etc/prismoid-backup.env
sudo sh -c 'set -a; . /etc/prismoid-backup.env; restic init'
sudo /srv/prismoid/deploy/backup/install.sh
sudo /srv/prismoid/deploy/backup/backup.sh          # force the first one; check `restic snapshots`
```

The IAM user needs only `s3:ListBucket` on `arn:aws:s3:::<bucket>` and `s3:GetObject`,
`s3:PutObject` and `s3:DeleteObject` on `arn:aws:s3:::<bucket>/*` (restic prunes). Keys in the file
are preferred to an instance role, which would put credentials on an instance that runs
model-written code. Silo's data lives on the instance volume and is in the snapshot; take an EBS
snapshot too if you want a second copy. The rest of step 9 (uptime probes, Logfire alerts) applies.

### Reboot test

Step 10 as written. Also stop and start the instance from the console once: the Elastic IP keeps
its address, and `https://api.DOMAIN/healthz` must answer within a couple of minutes without
touching the box.
