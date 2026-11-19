# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The media lane (ADR-024): `generate_image` - candidates, caps, journal, metering, degrade.

No network: the wire functions are monkeypatched; what these tests pin is the DOCTRINE -
bytes land via `write_bytes`, every attempt journals, units emit per success, the prompt never
reaches a label, a dead chain degrades to an instructive string, and the caps hold.
"""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STANDARD_PATHS
from prismoid.ai.tools import catalog, labels
from prismoid.ai.tools.providers import images as media
from prismoid.ai.tools.providers.images import ImageCandidate, image_toolset

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
JPEG = b"\xff\xd8\xff" + b"x" * 32


@dataclass
class FakeFrame:
    written: dict[str, bytes] = field(default_factory=dict)
    paths = STANDARD_PATHS
    links: dict[str, str] = field(default_factory=dict)  # symlinks the agent made, link -> target

    async def write_file(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def read_file(self, path: str) -> str:
        return ""

    async def read_bytes(self, path: str) -> bytes:
        return b""

    async def exec(self, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        path = shlex.split(command)[-1]  # `realpath -m -- <path>`, the only command this tool runs
        for link, target in self.links.items():
            if path == link or path.startswith(f"{link}/"):
                path = target + path[len(link) :]
        return ExecResult(0, f"{path}\n", "")

    async def publish(self, path: str, type: str, title: str, sources: Any = ()) -> None: ...


@dataclass
class Journal:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.calls.append({"tool": tool, "ok": ok, "summary": summary, "denied": denied_reason})


@dataclass
class Emitted:
    units: list[dict[str, int]] = field(default_factory=list)

    async def __call__(self, units: dict[str, int]) -> None:
        self.units.append(units)


@asynccontextmanager
async def _no_throttle():
    yield


def _throttled(_provider: str):
    return _no_throttle()


def candidate(
    provider: str = "openai", model: str = "gpt-image-1.5", emitted: Emitted | None = None
) -> ImageCandidate:
    return ImageCandidate(
        provider=provider,
        model_id=model,
        protocol="openai_images",
        base_url=None,
        api_key="k",
        emit_units=emitted or Emitted(),
    )


class Ctx:
    tool_call_id = "toolu_media_01"


def toolset_with(
    monkeypatch: pytest.MonkeyPatch,
    wire_results: list[list[bytes] | Exception],
    candidates: list[ImageCandidate],
    frame: FakeFrame,
    journal: Journal,
) -> Any:
    """A toolset whose openai wire yields the scripted results, one per candidate attempt."""
    script = list(wire_results)

    async def fake_wire(cand: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        result = script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setitem(media._WIRES, "openai_images", fake_wire)  # pyright: ignore[reportPrivateUsage]
    ts = image_toolset(("media.generate_image",), frame, journal, candidates, throttled=_throttled)
    assert ts is not None
    return ts.tools["generate_image"].function  # the raw coroutine, callable with (ctx, ...)


async def test_bytes_land_units_emit_and_the_journal_carries_no_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame, journal, emitted = FakeFrame(), Journal(), Emitted()
    generate = toolset_with(monkeypatch, [[PNG]], [candidate(emitted=emitted)], frame, journal)

    result = await generate(Ctx(), "a SECRET palace at dawn", "/work/images/fig.png")

    assert "/work/images/fig.png" in frame.written
    assert frame.written["/work/images/fig.png"] == PNG
    assert emitted.units == [{"image": 1, "request": 1}]
    assert "generated 1 image" in result
    ok_calls = [c for c in journal.calls if c["ok"]]
    assert ok_calls and ok_calls[0]["tool"] == "media.generate_image"
    assert all("SECRET" not in c["summary"] and "palace" not in c["summary"] for c in journal.calls), (
        "the prompt is content and must never reach a journal summary (ADR-020 keys-not-values)"
    )


async def test_candidate_failure_falls_to_the_next_and_both_are_journalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame, journal = FakeFrame(), Journal()
    first, second = Emitted(), Emitted()
    generate = toolset_with(
        monkeypatch,
        [RuntimeError("provider melted"), [PNG]],
        [candidate("openai", emitted=first), candidate("xai", "grok-imagine", emitted=second)],
        frame,
        journal,
    )

    result = await generate(Ctx(), "x", "/work/fig.png")

    assert "generated 1 image" in result
    assert first.units == [] and second.units == [{"image": 1, "request": 1}], "the loser never bills"
    assert [c["ok"] for c in journal.calls] == [False, True], "the failed attempt is on the record"


async def test_a_dead_chain_degrades_to_the_instructive_string(monkeypatch: pytest.MonkeyPatch) -> None:
    frame, journal = FakeFrame(), Journal()
    generate = toolset_with(
        monkeypatch,
        [RuntimeError("down"), RuntimeError("also down")],
        [candidate("openai"), candidate("xai", "grok-imagine")],
        frame,
        journal,
    )

    result = await generate(Ctx(), "x", "/work/fig.png")

    assert "image generation failed" in result
    assert "placeholder" in result and "do not fail the build" in result
    assert frame.written == {}


async def test_a_jpeg_answer_is_named_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some wires ignore the format ask. A .png name on JPEG bytes would lie to python-docx."""
    frame, journal = FakeFrame(), Journal()
    generate = toolset_with(monkeypatch, [[JPEG]], [candidate()], frame, journal)

    result = await generate(Ctx(), "x", "/work/fig.png")

    assert "/work/fig.jpg" in frame.written
    assert "/work/fig.jpg" in result


async def test_the_path_law_and_the_caps_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    frame, journal = FakeFrame(), Journal()
    generate = toolset_with(monkeypatch, [[PNG] * 4] * 3, [candidate()], frame, journal)

    assert "refused: path" in await generate(Ctx(), "x", "/artifacts/fig.png")
    assert "refused: path" in await generate(Ctx(), "x", "/work/fig.jpeg")
    assert "refused: path" in await generate(Ctx(), "x", "/work/../opt/plane/browser/x.png")

    # n clamps to 4; three calls of 4 reach the step cap of 12; the fourth refuses.
    for i in range(3):
        result = await generate(Ctx(), "x", f"/work/fig{i}.png", n=9)
        assert "generated 4 images" in result
    refused = await generate(Ctx(), "x", "/work/last.png")
    assert "refused: this step already generated 12" in refused


async def test_a_symlink_the_agent_made_cannot_carry_an_image_out_of_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloud driver writes as root and follows a symlink in the frame, so `/work/t -> /task`
    must not land an image in `/task`: the path is resolved in the frame and confined again,
    before any provider is paid, and each file written is resolved on its own."""
    frame, journal, emitted = FakeFrame(), Journal(), Emitted()
    frame.links = {"/work/t": "/task", "/work/fig-2.png": "/verify/x.png"}
    generate = toolset_with(monkeypatch, [[PNG, PNG]], [candidate(emitted=emitted)], frame, journal)

    assert "refused: path" in await generate(Ctx(), "x", "/work/t/fig.png")
    assert frame.written == {} and emitted.units == [], "refused before a provider is paid"

    await generate(Ctx(), "x", "/work/fig.png", n=2)
    assert list(frame.written) == ["/work/fig.png"], "a variant linked out of /work is not written"


async def test_variants_number_from_the_second_file(monkeypatch: pytest.MonkeyPatch) -> None:
    frame, journal = FakeFrame(), Journal()
    generate = toolset_with(monkeypatch, [[PNG, PNG, PNG]], [candidate()], frame, journal)

    await generate(Ctx(), "x", "/work/images/hero.png", n=3)

    assert set(frame.written) == {
        "/work/images/hero.png",
        "/work/images/hero-2.png",
        "/work/images/hero-3.png",
    }


def test_the_toolset_is_absent_not_broken() -> None:
    assert image_toolset(("media.generate_image",), FakeFrame(), None, [], throttled=_throttled) is None
    assert image_toolset(("media.generate_image",), None, None, [candidate()], throttled=_throttled) is None
    assert image_toolset(("frame.shell",), FakeFrame(), None, [candidate()], throttled=_throttled) is None


def test_the_label_speaks_locators_never_the_prompt() -> None:
    label = labels.label_for(
        "generate_image", {"path": "/work/f.png", "size": "portrait", "n": 2, "prompt": "SECRET"}
    )
    assert label == "/work/f.png portrait n=2"
    assert catalog.journal_name("generate_image") == "media.generate_image"


def test_build_model_refuses_media_protocols_by_name() -> None:
    from prismoid.ai.gateway.providers import build_model
    from prismoid.errors import ModelMisconfigured

    with pytest.raises(ModelMisconfigured, match="media"):
        build_model(provider="openai", model_id="gpt-image-1.5", protocol="openai_images", base_url=None)
