# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`generate_image` - the media lane (ADR-024). The ONLY file that speaks image-API SDKs.

A provider that isn't a chat model is not a gateway concern - it is a **tool**, granted by name,
journalled keys-only, emitting its own `UsageEvent` in native units (`IMAGE`). The call runs in
the worker process and bypasses frame egress by design, like connectors: an explicit grant plus
the journal are the policy, and the frame's no-network law is about model-written code.

The sibling law shapes everything here: `tools` sits beside `gateway` in the engine contract, so
this module imports **no gateway types**. `build()` (above both) resolves the `image-gen` chain,
binds one usage-emitter per candidate (`meter.units_emitter`), resolves keys
(`providers.api_key_for`) and the per-provider throttle, and hands this module plain frozen
values and callables - exactly how `frame_tools` receives its `Frame`.

Failure doctrine: **an image failure costs the figure, never the step.** The candidate chain is
tried in routing order; a candidate that errors is journalled `ok=false` and the next one serves.
When the whole chain is exhausted the tool returns an *instructive string* - not `ModelRetry`
(which would burn the schema-retry budget on weather), not an exception (which would kill a
40-minute section over one illustration) - telling the agent to place a captioned text
placeholder and carry on.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog, labels
from prismoid.ai.tools.call_rows import JournalTool, Said, say
from prismoid.ai.tools.frame_tools import Frame, writable_target
from prismoid.ai.tools.spans import quiet_span

if TYPE_CHECKING:
    from openai.types import ImagesResponse

_ROW: Final = catalog.tool("generate_image")
IMAGE_TIMEOUT_S: Final = 120.0  # one candidate's wall clock, download included
IMAGES_PER_CALL_MAX: Final = 4
IMAGES_PER_STEP_MAX: Final = 12  # a document's worth of figures, not a gallery

# The tool's shape vocabulary → each wire's own words. Kept deliberately coarse: three shapes
# cover documents; pixel-exact control is a v2 dial nobody has asked for.
_OPENAI_SIZES: Final = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}
_GOOGLE_ASPECTS: Final = {"square": "1:1", "portrait": "3:4", "landscape": "4:3"}

Size = Literal["square", "portrait", "landscape"]

# A Google predict call for a model that wire does not serve: not found, or not supported there.
_PREDICT_REFUSED: Final = frozenset({400, 404})


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """One image model, flattened to what this sibling layer may know (never a `ModelSpec`)."""

    provider: str
    model_id: str
    protocol: str  # "openai_images" | "google_images" - validated upstream by the catalog
    base_url: str | None
    api_key: str
    emit_units: Callable[[dict[str, int]], Awaitable[None]]

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model_id}"


def _numbered(path: str, index: int) -> str:
    """`/work/images/fig.png`, 2 → `/work/images/fig-2.png` (the first keeps the bare name)."""
    if index == 0:
        return path
    stem, _, _ = path.rpartition(".png")
    return f"{stem}-{index + 1}.png"


async def _fetch(url: str) -> bytes:
    """Some OpenAI-wire servers answer with a hosted URL instead of inline b64 (xAI does).
    The URL came from the provider's own authenticated response - same trust as the bytes."""
    parts = urlsplit(url)
    with quiet_span("image.fetch {host}{path}", host=parts.hostname or "", path=parts.path or "/"):
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _openai_images(candidate: ImageCandidate, prompt: str, size: Size, n: int) -> list[bytes]:
    """OpenAI and every server on the Images wire; b64 or URL answers alike, and one retry without `size`
    when a 400 names it (or names no parameter)."""
    from openai import AsyncOpenAI, BadRequestError

    client = AsyncOpenAI(
        api_key=candidate.api_key, base_url=candidate.base_url, timeout=IMAGE_TIMEOUT_S, max_retries=1
    )
    kwargs: dict[str, Any] = {
        "model": candidate.model_id,
        "prompt": prompt,
        "n": n,
        "size": _OPENAI_SIZES[size],
    }
    try:
        # Overloads cannot match a `**kwargs` call, so the response type is named here.
        result = cast("ImagesResponse", await client.images.generate(**kwargs))
    except BadRequestError as refused:
        if refused.param not in (None, "size"):
            raise
        kwargs.pop("size")  # the server's palette, its rules - shape was a hint, not a contract
        result = cast("ImagesResponse", await client.images.generate(**kwargs))

    images: list[bytes] = []
    for item in result.data or []:
        if item.b64_json:
            images.append(base64.b64decode(item.b64_json))
        elif item.url:
            images.append(await _fetch(item.url))
    return images


async def _google_images(candidate: ImageCandidate, prompt: str, size: Size, n: int) -> list[bytes]:
    """Google's images via google-genai: Imagen's `generate_images`, falling through to Gemini-image
    `generate_content` when the key is refused the predict wire (a 404, or a 400)."""
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=candidate.api_key)
    images: list[bytes] = []
    try:
        result: types.GenerateImagesResponse = await client.aio.models.generate_images(
            model=candidate.model_id,
            prompt=prompt,
            config=types.GenerateImagesConfig(number_of_images=n, aspect_ratio=_GOOGLE_ASPECTS[size]),
        )
        for generated in result.generated_images or []:
            image = generated.image
            if image is not None and image.image_bytes:
                images.append(bytes(image.image_bytes))
        return images
    except errors.ClientError as refused:
        if refused.code not in _PREDICT_REFUSED:
            raise

    shaped = f"{prompt} - {_GOOGLE_ASPECTS[size]} aspect ratio composition"
    for _ in range(n):
        response = await client.aio.models.generate_content(
            model=candidate.model_id,
            contents=shaped,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
        for cand in response.candidates or []:
            for part in (cand.content.parts or []) if cand.content else []:
                data = getattr(getattr(part, "inline_data", None), "data", None)
                if data:
                    images.append(bytes(data))
    return images


_WIRES: Final[dict[str, Callable[[ImageCandidate, str, Size, int], Awaitable[list[bytes]]]]] = {
    "openai_images": _openai_images,
    "google_images": _google_images,
}


def wire_for(protocol: str) -> Callable[[ImageCandidate, str, Size, int], Awaitable[list[bytes]]] | None:
    """The wire function for a media protocol - for the `media-ping` instrument, which probes
    one candidate directly. Everything else goes through `image_toolset`."""
    return _WIRES.get(protocol)


def image_toolset(
    granted: tuple[str, ...],
    frame: Frame | None,
    journal: JournalTool | None,
    candidates: Sequence[ImageCandidate],
    *,
    throttled: Callable[[str], AbstractAsyncContextManager[None]],
) -> FunctionToolset[Any] | None:
    """The media toolset, or None when it cannot exist - no candidates, no frame, or no grant.

    Absent beats broken: an agent whose catalog has no enabled image model simply has no
    `generate_image` in its vocabulary, and the section-contract builder omits the figure
    clauses to match (the long-form lane reads the same catalog).
    """
    if frame is None or not candidates or _ROW.journal not in granted:
        return None

    toolset: FunctionToolset[Any] = FunctionToolset(id="prismoid-images")
    generated_this_step = 0

    async def _serve(candidate: ImageCandidate, prompt: str, path: str, size: Size, n: int) -> list[str]:
        wire = _WIRES.get(candidate.protocol)
        if wire is None:
            raise RuntimeError(f"{candidate.ref} declares unknown media protocol {candidate.protocol!r}")
        assert frame is not None
        async with throttled(candidate.provider):
            async with asyncio.timeout(IMAGE_TIMEOUT_S):
                images = await wire(candidate, prompt, size, n)
        if not images:
            raise RuntimeError(f"{candidate.ref} returned no images")
        # Meter the instant the wire returns: the provider has generated and WILL bill these
        # images, so the ledger must record them BEFORE the fetch/write below - a write error or a
        # cancelled step must never swallow spend that already happened. Count what the provider
        # returned, not what we manage to write. The prompt is never journalled - units only.
        # A wire that raises or times out mid-generation has no countable result, so it stays unmetered.
        await candidate.emit_units({"image": len(images), "request": 1})
        written: list[str] = []
        for index, data in enumerate(images):
            target = _numbered(path, index)
            if data[:3] == b"\xff\xd8\xff":
                # A JPEG despite the ask (some wires ignore format). The bytes are the truth;
                # a `.png` name would lie to python-docx - re-extend honestly.
                target = target[: -len(".png")] + ".jpg"
            # Each name resolved on its own: a variant's name may be a link out of `/work`.
            resolved = await writable_target(frame, target)
            if resolved is None or not resolved.startswith("/work/"):
                continue
            target = resolved
            await frame.write_bytes(target, data)
            written.append(target)
        return written

    async def generate_image(
        ctx: RunContext[Any], prompt: str, path: str, size: Size = "square", n: int = 1
    ) -> str:
        """Generate an illustrative image and save it into the sandbox as PNG.

        Use this for illustrations, covers, and conceptual imagery. **Data charts are NOT this
        tool's job** - build charts with matplotlib in `shell` so every plotted number is a real
        number (and state the figures in text, or nothing can verify them).

        `path` must live under `/work/` and end in `.png` (e.g. `/work/images/fig-s03-1.png`).
        `size`: square | portrait | landscape. `n` > 1 writes `-2`, `-3`… variants.
        Then embed the file in your document (`doc.add_picture(...)` / `platypus.Image(...)`)
        and reference it in prose with a caption. If this tool reports failure, place a captioned
        text placeholder instead and continue - never fail the build over an illustration.
        """
        nonlocal generated_this_step
        assert frame is not None
        # The model may name the path as the frame really has it; the frame's verbs take the virtual
        # one, normalized so a `..` cannot climb out of `/work` and resolved in the frame so a
        # symlink cannot carry it out - before any provider is paid.
        virtual = await writable_target(frame, path)
        if virtual is None or not virtual.startswith("/work/") or not path.endswith(".png"):
            return frame.paths.render(
                "refused: path must live under /work/ and end in .png - e.g. /work/images/figure.png"
            )
        path = virtual
        n = max(1, min(int(n), IMAGES_PER_CALL_MAX))
        if generated_this_step + n > IMAGES_PER_STEP_MAX:
            return (
                f"refused: this step already generated {generated_this_step} images "
                f"(cap {IMAGES_PER_STEP_MAX}). Use the ones you have."
            )

        summary = labels.label_for(_ROW.name, {"path": path, "size": size, "n": n})
        last_reason = "no image model enabled"
        for candidate in candidates:
            served = labels.clamp(f"{summary} via {candidate.provider}")
            try:
                written = await _serve(candidate, prompt, path, size, n)
            except Exception as failed:  # fail-soft: journalled ok=false; try the next author
                await say(journal, Said.failed(_ROW.journal, failed, served))
                last_reason = f"{candidate.ref}: {type(failed).__name__}: {failed}"
                continue
            await say(journal, Said(_ROW.journal, True, served))
            generated_this_step += len(written)
            names = frame.paths.render(", ".join(written))
            return (
                f"generated {len(written)} image{'s' if len(written) != 1 else ''}: {names}. "
                "Embed the file(s) in the document and reference each in prose with a caption."
            )

        return (
            f"image generation failed ({last_reason}). Continue WITHOUT this figure: insert a "
            "captioned text placeholder (e.g. '[figure omitted: could not be generated]'), keep "
            "the surrounding prose self-sufficient, and do not fail the build over it."
        )

    doc = generate_image.__doc__ or ""
    if (rendered := frame.paths.render(doc)) is not doc:  # the frame's own spelling of /work
        generate_image.__doc__ = rendered
    toolset.add_function(generate_image, name=_ROW.name)
    return toolset
