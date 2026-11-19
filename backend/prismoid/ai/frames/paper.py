# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The paper frame: a step's files with no computer under them.

Every serial step starts here: reads, the web, the connector door, the files the model writes and
the publish door need no computer. The first call that does (a shell), or a park holding a draft the
agent wrote, moves the step to its Machine with these files (`carry_to`,
`capabilities/machine_acquire.py`); these files do not outlive the run. On a remote driver every
booted Machine holds a host slot, so a step runs here until it needs one (ADR-063, amended): the
mounted files, the deliverable and the manifest live in this process's memory for the length of
the step, and harvest reads them out exactly as it reads a sandbox's.

The publish door is unchanged: a file becomes an artifact only by `publish` into `/artifacts` with
a manifest entry (`validate_publish`, the same check every driver makes; the target and the entry
from `product.py`, the fields the in-frame runner writes), and harvest stores the bytes in the
object store. What paper does not have it answers honestly, never by raising
`NotImplementedError`: no shell (`exec` returns exit 126 and says so), no browser (the Protocol's
own refusal), no network (egress is the worker's door, never a frame's), nothing to sleep, wake or
bind. Nothing here outlives the step: a resume re-mounts its inputs, as on every frame.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Literal

from prismoid.ai.frames.driver import ExecResult, FrameDriver, Owner
from prismoid.ai.frames.layout import ARTIFACTS, MANIFEST, STANDARD_PATHS, FramePaths
from prismoid.ai.frames.product import artifact_target, manifest_entry
from prismoid.ai.frames.publish import validate_publish
from prismoid.errors import ModelIncapable, PolicyDenied
from prismoid.schemas.artifacts import ManifestEntry

PAPER_DRIVER: Final = "paper"

# The exit code a shell gives a command it cannot run, and the sentence that goes with it.
NO_SHELL: Final = ExecResult(126, "", "this step runs without a computer: there is no shell")


class PaperFull(ModelIncapable):
    """A write would take the frame past `max_bytes`: an infra failure the ladder paces. The
    caller sizes a step's mounts before it chooses paper, so this is the backstop, not the rule."""


class PaperFrame:
    """One step's files in memory, keyed by their virtual path. Satisfies `FrameDriver`."""

    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "none"
    driver: ClassVar[str] = PAPER_DRIVER
    # A path here is a key, never a link: a write lands where it is named (`writable_target`).
    follows_links: ClassVar[bool] = False

    def __init__(self, name: str, *, max_bytes: int) -> None:
        self.name = name
        self.max_bytes = max_bytes
        self.paths: FramePaths = STANDARD_PATHS
        self.egress_version = 0
        self.egress_hosts: list[str] = []
        self._files: dict[str, bytes] = {}
        self._system: set[str] = set()  # what the system mounted: read-only to the agent
        self._entries: list[ManifestEntry] = []

    @property
    def ref(self) -> str:
        return f"paper-{self.name}"

    @property
    def recording_dir(self) -> str:
        return f"{ARTIFACTS}/recording-{self.name}"  # never written: paper has no browser

    # ── files ──────────────────────────────────────────────────────────────────────────────

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent") -> None:
        if owner == "agent" and path in self._system:
            raise PermissionError(f"{path}: read-only - it was given to this step, not made by it")
        held = sum(len(b) for p, b in self._files.items() if p != path)
        if held + len(data) > self.max_bytes:
            raise PaperFull(f"{path}: the step's files would pass {self.max_bytes} bytes", infra=True)
        self._files[path] = bytes(data)
        if owner == "system":
            self._system.add(path)

    async def write_file(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def read_bytes(self, path: str) -> bytes:
        try:
            return self._files[path]
        except KeyError:
            raise FileNotFoundError(f"{path}: No such file or directory") from None

    async def read_file(self, path: str) -> str:
        return (await self.read_bytes(path)).decode(errors="replace")  # as `cat` would show it

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names: set[str] = set()
        for held in self._files:
            if held.startswith(prefix):
                head, _, rest = held[len(prefix) :].partition("/")
                names.add(f"{head}/" if rest else head)
        return sorted(names)

    # ── the move to a computer ─────────────────────────────────────────────────────────────

    @property
    def drafts(self) -> bool:
        """The agent wrote a file here: a park must keep it, and this frame ends with the run."""
        return any(path not in self._system for path in self._files)

    async def carry_to(self, frame: FrameDriver) -> None:
        """Hand every file to the computer the step moves to, the system's read-only, and append
        this frame's manifest entries to its manifest, as its own publish runner would."""
        for path, data in self._files.items():
            system = path in self._system
            await frame.write_bytes(
                path, data, mode=0o444 if system else 0o644, owner="system" if system else "agent"
            )
        if self._entries:
            held = list((await frame.manifest()).get("entries", []))
            held += [entry.model_dump(mode="json") for entry in self._entries]
            await frame.write_bytes(MANIFEST, json.dumps({"entries": held}, indent=2).encode())

    # ── the one door ───────────────────────────────────────────────────────────────────────

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        """Copy a file into `/artifacts` and append its manifest entry, after the same type check
        every driver makes. A directory artifact is a computer's (it travels as a tar)."""
        artifact_type = validate_publish(type, path)
        if path not in self._files:
            raise PolicyDenied(f"{path}: no such file - write it (e.g. under /work) first, then publish")
        data = self._files[path]
        target = artifact_target(path)
        self._files[target] = data
        self._entries.append(manifest_entry(target, artifact_type, title, data, sources))

    async def manifest(self) -> dict[str, Any]:
        return {"entries": [entry.model_dump(mode="json") for entry in self._entries]}

    # ── what paper does not have, answered ────────────────────────────────────────────────

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        return NO_SHELL

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        return {"ok": False, "error": "no browser in this frame"}

    async def denials(self) -> list[dict[str, str]]:
        return []  # no egress path: the worker's doors journal their own refusals

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        self.egress_hosts = list(domains)  # recorded for the caller; paper reaches nothing
        return True

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        return None  # born in memory

    async def bind(self, *, start: bool = True) -> bool:
        return False  # always this process's own; nothing was restarted

    async def reset_mounts(self) -> None:
        for path in self._system:
            self._files.pop(path, None)
        self._system.clear()

    async def freeze(self) -> None:
        return None  # no process to stop

    async def thaw(self) -> None:
        return None

    async def destroy(self) -> None:
        self._files.clear()
        self._system.clear()
        self._entries.clear()

    async def close(self) -> None:
        return None  # holds nothing open
