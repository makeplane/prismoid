# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a frame's containers are left with: stopped when the Machine goes
idle (`stop_labelled`), removed with their networks and anonymous volumes when the frame is destroyed
(`remove_labelled`); images and named volumes are kept. Only what the journal names and still wears
the frame's label, read from the daemon by that label (`existing_of`), is ever touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Final
from urllib.parse import quote, urlencode

from prismoid.device.docker_journal import Entry, Journal
from prismoid.device.docker_policy import FRAME_LABEL
from prismoid.device.docker_wire import CALL_TIMEOUT_S, LOG, UPSTREAM_ERRORS, call, removal_path

_STOP_GRACE_S: Final = 10


class Unfinished(Exception):
    """Docker could not be asked, or failed: a cleanup to try again."""


async def existing_of(upstream: str, ref: str) -> dict[str, set[str]]:
    """What wears `prismoid.frame=<ref>` on the daemon now, per journal kind: listed by that label alone
    (never by journaled ids, which could outgrow a request), to be intersected with the journal.
    A container's id is kept as `<id>` and, while it runs or is paused, also in `running`."""
    label = json.dumps({"label": [f"{FRAME_LABEL}={ref}"]})
    found: dict[str, set[str]] = {"containers": set(), "networks": set(), "volumes": set(), "running": set()}
    for kind, listing, query in (
        ("containers", "/containers/json", {"all": "1"}),
        ("networks", "/networks", {}),
        ("volumes", "/volumes", {}),
    ):
        status, listed = await call(upstream, "GET", f"{listing}?{urlencode({**query, 'filters': label})}")
        if kind == "volumes" and isinstance(listed, dict):
            listed = listed.get("Volumes") or []  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if status != 200 or not isinstance(listed, list):
            raise Unfinished(f"listing {kind} answered {status}")
        for item in listed:  # pyright: ignore[reportUnknownVariableType]
            _note(found, kind, item, ref)  # pyright: ignore[reportUnknownArgumentType]
    return found


def _note(found: dict[str, set[str]], kind: str, item: object, ref: str) -> None:
    """One listed object, if it wears the frame's label."""
    if isinstance(item, dict) and (item.get("Labels") or {}).get(FRAME_LABEL) == ref:  # pyright: ignore[reportUnknownMemberType]
        ident = str(item.get("Name" if kind == "volumes" else "Id"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        found[kind].add(ident)
        if kind == "containers" and item.get("State") in ("running", "restarting", "paused"):  # pyright: ignore[reportUnknownMemberType]
            found["running"].add(ident)


async def _each(upstream: str, ref: str, verb: str, paths: list[str], wait_s: float = CALL_TIMEOUT_S) -> bool:
    """Every request in `paths`; False when one could not reach Docker or Docker failed (a 5xx),
    so it is tried again. Docker's own 4xx (already gone, in use) is final."""
    done = await asyncio.gather(
        *(call(upstream, verb, p, wait_s=wait_s) for p in paths), return_exceptions=True
    )
    finished = True
    for path, answer in zip(paths, done, strict=True):
        if isinstance(answer, BaseException) or answer[0] >= 500:
            finished = False
        if isinstance(answer, BaseException) or answer[0] not in (200, 204, 304, 404):
            LOG.warning("docker cleanup of %s: %s %s: %s", ref, verb, path.split("?")[0], answer)
    return finished


async def stop_labelled(
    ref: str,
    upstream: str,
    journal: Journal,
    lock: asyncio.Lock | None = None,
    grace_s: int = _STOP_GRACE_S,
) -> None:
    """Stop the frame's journaled containers that run and still wear `prismoid.frame=<ref>` (the Machine
    went idle, or the helper is stopping), and nothing else of the user's: stopped, never
    removed, their volumes and images untouched. The journal forgets what is gone, under the frame's
    `lock`, so no create lands between the listing and the pruning."""
    try:
        async with lock or contextlib.nullcontext():
            before = journal.entry(ref)
            existing = await existing_of(upstream, ref)
            journal.keep(ref, existing, before)
            running = [i for i in journal.entry(ref).get("containers", []) if i in existing["running"]]
        stops = [f"/containers/{quote(i, safe='')}/stop?t={grace_s}" for i in running]
        await _each(upstream, ref, "POST", stops, wait_s=grace_s + 20)
    except (FileNotFoundError, ConnectionRefusedError):
        LOG.debug("no Docker daemon answers: %s has no containers running", ref)
    except (*UPSTREAM_ERRORS, Unfinished) as e:
        LOG.warning("could not stop %s's containers: %s", ref, e)


async def remove_labelled(ref: str, upstream: str, entry: Entry) -> bool:
    """The frame has ended: remove its journaled containers (with their anonymous volumes) and
    networks that still wear `prismoid.frame=<ref>`. Its named volumes (a database's data) and every
    image stay. True once done; False when Docker is away or failed, to be tried again."""
    try:
        existing = await existing_of(upstream, ref)
        containers = [i for i in entry.get("containers", []) if i in existing["containers"]]
        done = await _each(upstream, ref, "DELETE", [removal_path("container", i) for i in containers])
        networks = [n for n in entry.get("networks", []) if n in existing["networks"]]
        return await _each(upstream, ref, "DELETE", [removal_path("network", n) for n in networks]) and done
    except (FileNotFoundError, ConnectionRefusedError):
        LOG.debug("no Docker daemon answers: %s's cleanup waits for it", ref)
    except (*UPSTREAM_ERRORS, Unfinished) as e:
        LOG.warning("could not remove %s's Docker objects, will try again: %s", ref, e)
    return False
