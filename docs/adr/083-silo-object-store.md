# ADR-083 · Silo as the self-hosted object store

**Status:** accepted · 2026-11-05 · a fresh clone's `just up` failed: the MinIO images it named are gone.
**Amends** ADR-080 (its CI note on `bitnamilegacy/minio`) and the dev and single-host stacks of ADR-012
(the store is still "any S3"; only the container that serves it on our own boxes changes).

## Context

Prismoid keeps artifacts, snapshots and skill bodies in an S3 bucket. On a developer's machine, in CI and
in the single-host deployment that bucket is a container we run ourselves. That container was MinIO:
`minio/minio` for the server and `minio/mc` for the one-shot step that makes the three buckets
(`artifacts`, `snapshots`, `skills`).

MinIO stopped publishing those images, and the tags `deploy/compose.yaml` and
`deploy/compose.prod.yaml` named no longer pull, so `just up` on a fresh clone failed before Prismoid ran
a line. ADR-080 had already moved CI, the nightly frame tests and `just e2e` to the archived
`bitnamilegacy/minio:2025.7.23`, which still pulls but is frozen: no security fixes, no new releases,
and the compose files still named the dead images.

## Decision

1. **The server is [Silo](https://github.com/pgsty/silo)**, `pgsty/silo`, a community-maintained
   fork of the MinIO server under AGPL-3.0-or-later. Pinned by tag and digest, like the other
   third-party images: `pgsty/silo:RELEASE.2026-10-26T00-00-00Z@sha256:7bb2aff6b89efe1cb1303c10fc21d579447434ed06649ac9ec05a5e2dbf30ad7`
   (the multi-arch index).
2. **It is a drop-in.** Checked against the image before adopting it: the same S3 API, the same
   `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` settings, `server /data --console-address ":9001"`, the
   `/minio/health/live` route, a web console on 9001, and the same on-disk format (an object written
   by `bitnamilegacy/minio:2025.7.23` read back unchanged by Silo from the same volume). So the
   compose service keeps the name `minio`, the profile stays `minio`, the volume stays `miniodata`,
   and every `PRISMOID_MINIO_*` / `MINIO_*` variable keeps its name: they now name the protocol, not
   the vendor.
3. **Buckets are made by a step.** Silo has no `MINIO_DEFAULT_BUCKETS`. The `minio-init` one-shot
   makes the buckets with `mcli mb --ignore-existing`, using the `mcli` client the server image
   carries, so one pinned image serves both. `pgsty/mc` (the same client as its own image) is not
   needed.
4. **CI runs Silo from the compose file.** A GitHub Actions service container runs the image's
   default command, and Silo's default prints its help and exits. The `integration` job and the
   nightly frame tests therefore start `minio` with `docker compose -f deploy/compose.yaml up -d
   --wait minio` and run `minio-init`, the same commands the evals, perf and chaos workflows
   already used. `web/e2e/stack.sh` runs the same pinned image with `docker run` and makes the
   buckets with `mcli` inside it.
5. **Production may use any S3.** Nothing in Prismoid depends on Silo: `S3_ENDPOINT` and the other
   `S3_*` variables point the worker at R2, Spaces or a hyperscaler bucket, and an empty
   `COMPOSE_PROFILES` keeps Silo from starting at all.

## Alternatives considered

- **Stay on `bitnamilegacy/minio`.** It pulls today, but it is an archive: no fixes, and nothing
  promises it keeps pulling. It also leaves the compose files on images that do not exist.
- **Build MinIO from source ourselves.** Possible (the source is still AGPL), but it makes us the
  maintainers of an object store's release and security process.
- **SeaweedFS.** Mature and Apache-2.0, but a different system: its own volume, filer and S3
  gateway processes, its own settings and its own data layout. Existing volumes would not open.
- **Garage.** Small and AGPL, built for geo-distributed clusters; it implements a narrower part of
  the S3 API, buckets and keys are made through its own admin CLI, and existing volumes would not
  open.
- **RustFS.** A young S3-compatible server, MinIO-like in shape, but early in its releases.
- **Chainguard's `cgr.dev/chainguard/minio`.** Maintained and minimal, but the free tier serves only
  `latest`, so it cannot be pinned to a version a contributor can reproduce.

## Consequences

- `just up` works again on a fresh clone, and the dev stack, CI, the nightly frame tests, `just e2e`
  and the single-host deployment all run one pinned image.
- An existing `miniodata` volume, on a laptop or a server, opens under Silo as it is. A rollback is
  the previous image line; the data does not change shape.
- Silo is maintained by one small organisation. If it stalls, the swap is again one image line in
  each compose file and `web/e2e/stack.sh`, because nothing above the S3 API knows which server
  answers.
- The dev compose file's other fixed host ports became variables in the same change, with today's
  values as defaults: `PRISMOID_DB_PORT` (5433) and `PRISMOID_EGRESS_CONTROL_PORT` (3129), beside the
  existing `PRISMOID_MINIO_PORT` and `PRISMOID_MINIO_CONSOLE_PORT`. A second stack can now run beside the
  first with `COMPOSE_PROJECT_NAME` and those variables.
