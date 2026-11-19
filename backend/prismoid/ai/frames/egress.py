# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The egress proxy - the only way out of a frame.

The frame sits on an **internal** Docker network: no gateway, no DNS, no route. This proxy
is a bastion with a foot in both worlds - reachable *from* the frame network, and itself able to
reach the internet. So the only path from an agent to the outside runs through here, and here we
say yes or no per task.

**Deliberately stdlib-only, plus exactly two modules of ours.** No new dependency, and
the image is `python:3.12-slim`. The forward itself is `prismoid.egress_relay` - one copy shared with
the device helper's proxy, which keys a frame by credential instead of source IP (`principal_of`)
- and it imports `prismoid.schemas.egress`, the shared vet a resolved answer is classified
against, so a name pinned here is judged by the SAME rule the web door uses, not a second
hand-maintained copy that could drift. Both are stdlib-only, so `deploy/compose.yaml` mounts them
(plus the two trivial `__init__.py` files above them) read-only alongside this file - nothing else
is imported, and nothing else is mounted. A forward proxy is 200 lines of asyncio; a new
dependency is forever.

**The allowlist is per task, and it is pushed, not pulled.** The worker registers a frame's
allowed domains with the control API at `frame.create()`; the proxy keys them by the frame's
source IP. A proxy that had to *ask* the database on every request would put a query on the path
of every HTTP call an agent makes, and would need database credentials inside the one process
whose whole job is to be exposed.

**A denial is a `tool.called ok=false` on the flight recorder**. An egress denial that
is invisible gets debugged for an hour; one that shows up on the stream is a five-second fix.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Final, cast

from prismoid.egress_relay import relay  # the ONE relay, shared with the device helper

LOG = logging.getLogger("egress")

PROXY_PORT: Final = 3128
CONTROL_PORT: Final = 3129

# The control plane's shared secret. Read from the environment because
# this file runs as its own script in the proxy container (it imports exactly one module of ours -
# the shared vet, mounted alongside it - and no `conf`). A frame can reach the control port on the
# frame network, so without this a frame could POST /allow
# to widen its OWN allowlist and exfiltrate. When the secret is set, every control request must
# carry it in the header below; the worker that registers frames is its only other holder.
CONTROL_SECRET_ENV: Final = "PRISMOID_EGRESS_CONTROL_SECRET"
CONTROL_SECRET_HEADER: Final = "x-egress-control-secret"


def control_headers() -> dict[str, str]:
    """The shared secret authorising an egress control-plane call, as the worker sends it.

    A frame can reach the proxy's control port on the frame network, so `POST /allow` must prove it
    comes from the worker - not from model code widening its own allowlist. The secret lives only in
    the worker and the proxy (compose sets both from `PRISMOID_EGRESS_CONTROL_SECRET`) and never
    enters a frame. Read from the environment directly: it is proxy wiring, not routing intent.

    The default matches compose's own `${PRISMOID_EGRESS_CONTROL_SECRET:-dev-egress-control-secret}`
    fallback so an unconfigured `just dev` worker and proxy agree out of the box; prod sets a real
    secret on both sides. (A dev default, not a secret at rest - the s3/sandbar dev-default precedent.)"""
    return {CONTROL_SECRET_HEADER: os.environ.get(CONTROL_SECRET_ENV, "dev-egress-control-secret")}


# ip → allowed domains. Populated by the worker at frame.create() via the control API.
_ALLOW: dict[str, set[str]] = {}
_VERSIONS: dict[str, int] = {}  # ip → the last applied allowlist version
# The audit trail: every decision, so the worker can journal denials it did not see happen.
_DENIALS: list[dict[str, str]] = []


def _client_ip(writer: asyncio.StreamWriter, _auth: str = "") -> str:
    """The Docker frame's principal: its source IP on the internal frame network."""
    peer = writer.get_extra_info("peername")
    return str(peer[0]) if peer else ""


def _record_denial(ip: str, host: str, reason: str) -> None:
    _DENIALS.append({"ip": ip, "host": host, "reason": reason})
    LOG.warning("DENY %s → %s (%s)", ip, host, reason)


async def handle_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One client connection, through the shared relay, the frame keyed by its source IP."""
    await relay(
        reader,
        writer,
        principal_of=_client_ip,
        allowed_for=lambda ip: _ALLOW.get(ip, set()),
        on_deny=_record_denial,
    )


def apply_allow(ip: str, domains: list[str], version: int | None) -> tuple[int, dict[str, object]]:
    """Replace one frame's allowlist unless `version` is stale: a push whose version is
    not newer than the last applied one for this IP is refused with 409, so a slow push can never
    overwrite a newer one. An unversioned push always applies."""
    current = _VERSIONS.get(ip, 0)
    if version is not None and version <= current:
        return 409, {"ok": False, "error": "stale_version", "version": current}
    _ALLOW[ip] = set(domains)
    if version is not None:
        _VERSIONS[ip] = version
    # **Clear this IP's denial history.** Docker reuses container IPs, and the denial log is keyed
    # by IP - so without this, a new frame inherits the refusals of whatever frame last held its
    # address, and the flight recorder shows a task being denied requests it never made.
    # Registration is the start of a frame's life; its record starts empty.
    _DENIALS[:] = [d for d in _DENIALS if d.get("ip") != ip]
    LOG.info("registered %s → %s (v%s)", ip, sorted(domains), version)
    return 200, {"ok": True, "ip": ip, "domains": sorted(domains), "version": version}


def forget(ip: str) -> None:
    _ALLOW.pop(ip, None)
    _VERSIONS.pop(ip, None)


async def handle_control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """The control API. `POST /allow {ip, domains, version?}` (409 `stale_version` when the
    version is not newer than the last applied one) · `DELETE /allow/{ip}` · `GET /denials`.

    Guarded by a shared secret: the proxy sits on the frame network,
    so a frame can reach this port - and `POST /allow` widening a frame's OWN allowlist is an
    exfiltration door. When `PRISMOID_EGRESS_CONTROL_SECRET` is set, every request must carry it in
    the `X-Egress-Control-Secret` header; the worker that registers frames is its only other holder,
    and it never enters a frame. Unset (bare-host dev) serves with a loud startup warning rather
    than breaking - compose sets it by default. The plane can only ever *narrow* what a frame reaches.
    """
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        method, path, *_ = request_line.decode().split()

        length = 0
        provided_secret = ""
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode().partition(":")
            header = name.lower().strip()
            if header == "content-length":
                length = int(value.strip())
            elif header == CONTROL_SECRET_HEADER:
                provided_secret = value.strip()

        # Authorise BEFORE reading the body or mutating state: a frame that reached this port must
        # not rewrite its own allowlist. `hmac.compare_digest` keeps the check constant-time.
        expected = os.environ.get(CONTROL_SECRET_ENV, "")
        if expected and not hmac.compare_digest(provided_secret, expected):
            LOG.warning("control DENY %s %s from %s: bad or missing secret", method, path, _client_ip(writer))
            denied = b'{"error": "forbidden"}'
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(denied)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + denied
            )
            await writer.drain()
            return

        body = await reader.readexactly(length) if length else b"{}"
        payload = json.loads(body or b"{}")

        status = 200
        out: dict[str, object]
        if method == "POST" and path == "/allow":
            raw_version = payload.get("version")
            status, out = apply_allow(
                str(payload["ip"]),
                [str(d) for d in cast("list[object]", payload.get("domains") or [])],
                None if raw_version is None else int(raw_version),
            )
        elif method == "DELETE" and path.startswith("/allow/"):
            forget(path.removeprefix("/allow/"))
            out = {"ok": True}
        elif method == "GET" and path == "/denials":
            out = {"denials": _DENIALS[-100:]}
        elif method == "GET" and path == "/healthz":
            out = {"ok": True, "frames": len(_ALLOW)}
        else:
            out = {"ok": False, "error": "unknown"}

        data = json.dumps(out).encode()
        reason = b"OK" if status == 200 else b"Conflict"  # a stale version push is the one non-200
        writer.write(
            b"HTTP/1.1 "
            + str(status).encode()
            + b" "
            + reason
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(data)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + data
        )
        await writer.drain()
    except Exception as exc:
        LOG.warning("control error: %s", exc)
    finally:
        writer.close()


async def serve() -> None:
    logging.basicConfig(level=os.environ.get("EGRESS_LOG_LEVEL", "INFO"), format="egress: %(message)s")
    if not os.environ.get(CONTROL_SECRET_ENV):
        LOG.warning(
            "%s is unset - the control plane is UNAUTHENTICATED; set it (compose does) so a frame "
            "cannot rewrite its own allowlist",
            CONTROL_SECRET_ENV,
        )
    proxy = await asyncio.start_server(handle_proxy, "0.0.0.0", PROXY_PORT)
    control = await asyncio.start_server(handle_control, "0.0.0.0", CONTROL_PORT)
    LOG.info("proxy on :%d · control on :%d", PROXY_PORT, CONTROL_PORT)
    async with proxy, control:
        await asyncio.gather(proxy.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    asyncio.run(serve())
