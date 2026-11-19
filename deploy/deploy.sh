#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Deploy one release to the box this script lives on, built on the box from the checked-out sha.
#
#   git fetch origin && git checkout --detach <git-sha>
#   deploy/deploy.sh <git-sha> [--wait-idle]
#
# What one deploy is: prove the checkout IS the sha and the Docker Engine meets the frame network's
# floor, pin the sha (local image tags + PRISMOID_WORKER_APP_VERSION) into deploy/.env, build the frame
# image and the stack's images, stop the worker, run the one-shot migrate,
# bring everything up, and refuse to call it done until the public /healthz answers and the NEW
# worker has beaten. Stop-start, never rolling - the single worker's crash-recovery is
# chaos-drill-proven, and ADR-027's version pinning makes a cross-sha boot sweep the old
# release's queued ghosts LOUDLY instead of freezing the queue.
# `--wait-idle` is for operator deploys where cancelling in-flight work is unacceptable.
#
# Rollback is the same two lines with the previous sha: its tagged images are still on the box, so
# the build is a cache hit. Every release's images stay until you prune them by hand.
set -euo pipefail

SHA="${1:?usage: deploy.sh <git-sha> [--wait-idle]}"
cd "$(dirname "$0")" # /srv/prismoid/deploy
COMPOSE=(docker compose -f compose.prod.yaml --env-file .env)

env_value() { # NAME's value in deploy/.env: its last line (compose's pick), an inline comment and blanks dropped
    grep "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//' || true
}

with_search() { # a COMPOSE_PROFILES list, with `search` added once
    local list="${1//[[:space:]]/}"
    case ",$list," in
    *,search,*) echo "$list" ;;
    *) echo "${list:+$list,}search" ;;
    esac
}

# SearXNG is opt-in: its compose profile rides along only when the search chain names it. It is
# added to deploy/.env's COMPOSE_PROFILES, never in its place: a `--profile` flag replaces that list,
# which would leave the object store (profile minio) off and the migrate's skills seed failing.
# A line filled in but for its comment (`SEARXNG_SECRET=   # REQUIRED ...`) is empty here: compose
# would hand the comment to SearXNG as its secret.
if env_value PRISMOID_SEARCH_ENGINES | grep -Eq '(^|,)[[:space:]]*searxng[[:space:]]*(,|$)'; then
    if [[ -z "$(env_value SEARXNG_SECRET)" ]]; then
        echo "DEPLOY REFUSED: PRISMOID_SEARCH_ENGINES names searxng but SEARXNG_SECRET is empty in deploy/.env." >&2
        echo "  Fix: set SEARXNG_SECRET=\$(openssl rand -hex 32)  (deploy/.env.example)" >&2
        exit 1
    fi
    COMPOSE_PROFILES=$(with_search "$(env_value COMPOSE_PROFILES)")
    export COMPOSE_PROFILES # the environment wins over the --env-file's line
fi

pin() { # set KEY=VALUE in deploy/.env, replacing any existing line
    if grep -q "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >>.env
    fi
}

pg() { # one value out of the stack's Postgres
    "${COMPOSE[@]}" exec -T postgres psql -U prismoid -d prismoid -tAc "$1"
}

# The frame network's floor (ADR-063 S2). Each sandbox with domains sits on an --internal network of
# its own made with inhibit_ipv4, and sandbard refuses one whose IPAM reports a gateway. On an engine below that
# floor an egress-on sandbard exits at every boot and, through the worker's depends_on, the worker
# never starts - every task stops, not only frames, and the deploy hangs. So prove the engine on a
# throwaway network first, while the running release keeps serving: nothing pinned, nothing stopped.
frame_net_preflight() {
    local probe="sandbar_floor_probe_$$" ver out ipam="" gw why=""
    ver=$(docker version -f '{{.Server.Version}}' 2>/dev/null || echo unknown)
    if ! out=$(docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true "$probe" 2>&1); then
        why="it cannot create an --internal inhibit_ipv4 network ($out)"
    elif ! ipam=$(docker network inspect "$probe" -f '{{json .IPAM.Config}}'); then
        why="it cannot inspect the throwaway network $probe"
    fi
    docker network rm "$probe" >/dev/null 2>&1 || true
    # JSON, not {{.Gateway}}: Docker 29's CLI prints a missing gateway as "invalid IP" in a template.
    gw=$(grep -oE '"Gateway":"[^"]+"' <<<"$ipam" | cut -d'"' -f4 | paste -sd' ' - || true)
    if [[ -z "$why" && -n "$gw" ]]; then
        why="it gives an --internal inhibit_ipv4 network a gateway ($gw), so the host holds an address sandboxes reach"
    fi
    if [[ -n "$why" ]]; then
        echo "DEPLOY REFUSED: Docker Engine $ver is below the frame network's floor - $why." >&2
        echo "  This release needs a Docker Engine whose --internal + inhibit_ipv4 networks report no gateway" >&2
        echo "  (verified on 29.4); below it sandbard refuses to boot and the worker never starts." >&2
        echo "  Fix: upgrade Docker Engine on this host, then re-run this deploy (deploy/DEPLOY.md," >&2
        echo "  \"Upgrading to the S2 frame network\"). Nothing was pinned or stopped: the running release is untouched." >&2
        exit 1
    fi
    echo "deploy: pre-flight ok - Docker Engine $ver gives an internal inhibit_ipv4 network no gateway"
}

# beat_since EPOCH: the worker has beaten after EPOCH (Postgres's clock) and the beat is fresh.
beat_since() {
    pg "select beat_at > to_timestamp($1) and extract(epoch from (now() - beat_at)) < 180
        from system_beats where name = 'worker';" | grep -q t
}

# The images are built from the working tree, so they are the sha only if the tree is: the checkout
# must be the sha, with no tracked file changed (deploy/.env is ignored, so it never counts).
checkout_is_sha() {
    local want head
    if ! want=$(git rev-parse --verify --quiet "$SHA^{commit}"); then
        echo "DEPLOY REFUSED: $SHA is not a commit here - git fetch origin first." >&2
        exit 1
    fi
    head=$(git rev-parse HEAD)
    if [[ "$head" != "$want" ]]; then
        echo "DEPLOY REFUSED: the checkout is $head, not $want - git checkout --detach $SHA first." >&2
        exit 1
    fi
    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
        echo "DEPLOY REFUSED: tracked files differ from $want (git status) - the images would not be that sha." >&2
        exit 1
    fi
    SHA="$want"
}

checkout_is_sha
frame_net_preflight

# sandbard runs unprivileged and reaches the Engine through the socket's group (compose group_add).
# An operator's own DOCKER_GID that is not the socket's group would boot a sandbard that cannot
# reach the Engine: refuse it here, before anything is pinned, built or stopped.
sock_gid=$(stat -c %g /var/run/docker.sock)
cur_gid=$(env_value DOCKER_GID)
if [[ -n "$cur_gid" && "$cur_gid" != "$sock_gid" ]]; then
    echo "DEPLOY REFUSED: DOCKER_GID=$cur_gid in deploy/.env is not the Docker socket's group ($sock_gid)." >&2
    echo "  Fix: set DOCKER_GID=$sock_gid (getent group docker | cut -d: -f3) or remove the line." >&2
    exit 1
fi
pin GIT_SHA "$SHA" # → PRISMOID_WORKER_APP_VERSION (ADR-027)
pin BACKEND_IMAGE "prismoid/backend:$SHA"
pin WEB_IMAGE "prismoid/web:$SHA"
pin WEB_ADMIN_IMAGE "prismoid/web-admin:$SHA" # the operator console - a separate app, a separate image
pin SANDBAR_IMAGE "prismoid/sandbard:$SHA"
pin FRAME_IMAGE "prismoid-frame:$SHA" # sandbard spawns this on the HOST daemon (PRISMOID_WORKER_SANDBAR_IMAGE)
pin FRAME_BROWSER_IMAGE "prismoid-frame-browser:$SHA" # the browser profile, on the frame image (PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE)
pin DOCKER_GID "$sock_gid"

# Built while the running release keeps serving; nothing has stopped yet.
echo "deploy: building images for $SHA"
docker build -t "prismoid-frame:$SHA" ../backend/prismoid/ai/frames/image
docker build -t "prismoid-frame-browser:$SHA" --build-arg "BASE_IMAGE=prismoid-frame:$SHA" ../backend/prismoid/ai/frames/image_browser
"${COMPOSE[@]}" build

if [[ "${2:-}" == "--wait-idle" ]]; then
    # Queue vitals ride the worker's Beat payload (system_beats.info.queues, ADR-027).
    for _ in $(seq 1 60); do
        BUSY=$("${COMPOSE[@]}" exec -T postgres psql -U prismoid -d prismoid -tAc \
            "select coalesce((select sum((v->>'running')::int + (v->>'enqueued')::int)
                              from system_beats, jsonb_each(info->'queues') as t(k, v)
                              where name = 'worker'), 0);")
        [[ "$BUSY" == "0" ]] && break
        echo "deploy: queues busy ($BUSY running/enqueued) - waiting 10s"
        sleep 10
    done
fi

echo "deploy: stopping worker (stop-start; recovery adopts same-sha work, sweeps cross-sha ghosts)"
"${COMPOSE[@]}" stop worker
# The beat gate below wants a beat from THIS boot: the old worker beats once a minute, so its last
# beat would pass a plain freshness check even if the new worker never started. Read Postgres's
# clock now that the old worker is stopped, and accept only a beat after it.
since=$(pg "select extract(epoch from now());" 2>/dev/null || true)
if [[ ! "$since" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    since=0 # no Postgres yet (a first deploy): there is no old beat to mistake for a new one
    echo "deploy: no Postgres clock before boot - the beat gate takes any fresh beat"
fi
"${COMPOSE[@]}" run --rm migrate
"${COMPOSE[@]}" up -d --remove-orphans

DOMAIN=$(env_value DOMAIN)
ok=""
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "https://api.${DOMAIN}/healthz" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 5
done
if [[ -z "$ok" ]]; then
    "${COMPOSE[@]}" ps
    "${COMPOSE[@]}" logs --tail 80 api worker caddy
    echo "DEPLOY FAILED: https://api.${DOMAIN}/healthz never answered" >&2
    exit 1
fi

# The API being up says nothing about the worker - its liveness is the Beat row, and only a beat
# newer than the stop above is the new worker's.
fresh=""
for _ in $(seq 1 24); do
    if beat_since "$since"; then
        fresh=1
        break
    fi
    sleep 5
done
if [[ -z "$fresh" ]]; then
    "${COMPOSE[@]}" logs --tail 80 worker
    echo "DEPLOY FAILED: the new worker never beat (no beat since the old worker stopped)" >&2
    exit 1
fi

echo "deployed $SHA"
