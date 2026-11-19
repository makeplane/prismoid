# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's frame that starts as paper and becomes its Machine at `swap`: the toolsets hold it from the
build, so the swap is invisible to them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from prismoid.ai.frames.driver import ExecResult, FrameDriver, Owner
from prismoid.ai.frames.layout import FramePaths
from prismoid.ai.frames.paper import PAPER_DRIVER, PaperFrame


class OnDemandFrame:
    """A step's frame that starts as paper and becomes its Machine at `swap`. Satisfies `FrameDriver`."""

    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "none"
    driver: ClassVar[str] = PAPER_DRIVER  # read from the Machine's own frame once it is there

    def __init__(self, paper: PaperFrame) -> None:
        self.paper = paper
        self.current: FrameDriver = paper
        self._mirror()

    def _mirror(self) -> None:
        """The plain facts the Protocol holds as fields, read from the frame now under it."""
        self.paths: FramePaths = self.current.paths
        self.egress_version = self.current.egress_version
        self.egress_hosts = self.current.egress_hosts

    @property
    def on_paper(self) -> bool:
        return self.current is self.paper

    def swap(self, frame: FrameDriver) -> None:
        """From now on every call lands on `frame`, the step's Machine."""
        self.current = frame
        self._mirror()

    @property
    def follows_links(self) -> bool:
        return getattr(self.current, "follows_links", True)

    @property
    def ref(self) -> str:
        return self.current.ref

    @property
    def recording_dir(self) -> str:
        return self.current.recording_dir

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        await self.current.create(task_id, allow_domains, labels=labels, egress_version=egress_version)

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        return await self.current.exec(command, timeout)

    async def write_file(self, path: str, content: str) -> None:
        await self.current.write_file(path, content)

    async def read_file(self, path: str) -> str:
        return await self.current.read_file(path)

    async def read_bytes(self, path: str) -> bytes:
        return await self.current.read_bytes(path)

    async def list_dir(self, path: str) -> list[str]:
        return await self.current.list_dir(path)

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent") -> None:
        await self.current.write_bytes(path, data, mode=mode, owner=owner)

    async def destroy(self) -> None:
        await self.current.destroy()

    async def close(self) -> None:
        await self.current.close()

    async def bind(self, *, start: bool = True) -> bool:
        return await self.current.bind(start=start)

    async def reset_mounts(self) -> None:
        await self.current.reset_mounts()

    async def freeze(self) -> None:
        await self.current.freeze()

    async def thaw(self) -> None:
        await self.current.thaw()

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        return await self.current.allow(domains, version)

    async def manifest(self) -> dict[str, Any]:
        return await self.current.manifest()

    async def denials(self) -> list[dict[str, str]]:
        return await self.current.denials()

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        await self.current.publish(path, type, title, sources)

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        return await self.current.browser(command, timeout)
