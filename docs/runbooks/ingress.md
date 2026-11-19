# Ingress — deploying the stream without killing it

The SSE contract (§5.5) survives exactly as long as the ingress does not "help". The rules,
and why each exists:

- **No buffering on stream routes.** The app already sends `X-Accel-Buffering: no`; an
  nginx-family proxy that ignores it needs `proxy_buffering off` on `/v1/tasks/*/events`.
  A buffered stream is not a stream — the client sees nothing until the buffer flushes.
- **No compression on stream routes.** The app never compresses them; the ingress must not
  add it. Compression buffers, and buffering is death (above).
- **HTTP/2 at the edge.** HTTP/1.1's six-connections-per-origin limit is the one real SSE
  constraint a browser has; terminated over h2 it vanishes.
- **Idle timeouts above 15 s.** The stream heartbeats (`: ping`) every 15 s; a proxy with a
  shorter idle timeout kills healthy streams and turns free reconnects into a reconnect storm.
- **Scrub query strings from access logs.** `?token=` exists because a browser's EventSource
  cannot set headers (§5.5). The app hashes the token at rest and Logfire scrubs it from
  spans (`c[sk]_…` patterns); an ingress that logs full request lines re-leaks it. Log the
  path, not the query — or scrub `token=[^&]*` explicitly. This is the rule ADR-009 records;
  it is not optional.
- **Rate limiting belongs here, eventually.** The in-process throttles divide by
  `PRISMOID_API_REPLICA_COUNT` and stay honest as floors; when a real ingress exists, put the
  precise per-client limits at the edge where the fleet is one place.
