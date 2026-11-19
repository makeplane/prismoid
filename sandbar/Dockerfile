# syntax=docker/dockerfile:1
# sandbard - the sandbox daemon, as a container (the single-box / compose deployment).
#
# Multi-stage: a Go builder compiles a static binary; the runtime is a minimal alpine that ships
# only that binary. In this image sandbard uses the DOCKER backend - it talks to the host's Docker
# Engine over a mounted /var/run/docker.sock and spawns sibling frame containers on the host
# (docker-out-of-docker). That is the pragmatic single-machine model.
#
# The FIRECRACKER backend is NOT a container concern: a microVM needs /dev/kvm and host networking,
# so on a bare/sovereign machine run this same binary as a host service (systemd), not in Docker -
# see deploy/DEPLOY.md.

# Base images are pinned by digest so a rebuild of the same commit gets the same bytes; the tag
# in each reference records what the digest was resolved from (Docker ignores it once a digest is
# present), and Dependabot proposes the bumps.
FROM golang:1.26-alpine@sha256:ecb67f987aa1a57794c91d19ade7ae0209f2f0f5496aaef9e6ae85ddf92d02ab AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
# Static, stripped, reproducible-ish. CGO off so the binary runs on a scratch/alpine runtime.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/sandbard ./cmd/sandbard

FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS runtime
# ca-certificates so the egress proxy can validate upstream TLS on the operator's behalf.
RUN apk add --no-cache ca-certificates
COPY --from=build /out/sandbard /usr/local/bin/sandbard

# 7070 = the control API (the worker reaches it). 7080 = the egress proxy on sandbard, and the
# relay's listen port on sandbar-egress (`sandbard relay`); frames reach only the relay.
EXPOSE 7070 7080
# Non-root: the daemon only talks to the Engine over the socket and writes nothing to disk. The
# docker backend reaches the mounted socket through its GROUP, whose id is host-specific, so the
# runner adds it (compose: `group_add: ["${DOCKER_GID}"]`; Kubernetes: `supplementalGroups`). The
# socket is still root-equivalent on the host: on a shared host prefer the firecracker backend and
# the jailer (DEPLOY.md). No HEALTHCHECK here: the same image runs `sandbard relay`, which serves
# no /healthz, so the daemon's probe lives in compose.
USER 65532:65532
ENTRYPOINT ["sandbard"]
