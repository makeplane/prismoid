# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker frame images: one build context per profile, each tag the hash of its recipe.

The profiles are a line, each layered on the one before it (`FROM ${BASE_IMAGE}`): `base` is the
office and data toolchain, `code` adds git, ripgrep, node and pytest, `browser` adds Chromium and its
sidecar. It is `schemas/machines.widen`'s order, so a wider Machine runs every narrower step.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import docker
import logfire
from docker.errors import ImageNotFound

IMAGE_DIR = Path(__file__).parent / "image"
IMAGE_CODE_DIR = Path(__file__).parent / "image_code"
IMAGE_BROWSER_DIR = Path(__file__).parent / "image_browser"
# Each profile's build context and the profile it is layered on.
RECIPES: Final[dict[str, tuple[Path, str | None]]] = {
    "base": (IMAGE_DIR, None),
    "code": (IMAGE_CODE_DIR, "base"),
    "browser": (IMAGE_BROWSER_DIR, "code"),
}


def image_digest(profile: str = "base") -> str:
    """sha256 over every file in the build context (sorted path + bytes), folding in a layered
    profile's parent tag, so any context or parent change is a different image."""
    root, parent = RECIPES[profile]
    digest = hashlib.sha256()
    if parent is not None:
        digest.update(image_tag(parent).encode())
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def image_tag(profile: str = "base") -> str:
    """`prismoid-frame:<hash of the build context>`: the tag IS the recipe, so an edited Dockerfile is a
    different image that cannot be mistaken for the stale one."""
    name = "prismoid-frame" if profile == "base" else f"prismoid-frame-{profile}"
    return f"{name}:{image_digest(profile)[:12]}"


TAGS: Final[dict[str, str]] = {profile: image_tag(profile) for profile in RECIPES}
_images_ready: set[str] = set()


def ensure_image(client: docker.DockerClient, profile: str = "base") -> str:
    """Build the frame image (its parent first) once per process per profile if this exact recipe is
    absent; returns the tag. An image deleted mid-flight surfaces at `create()`."""
    tag = TAGS[profile]
    if tag in _images_ready:
        return tag
    try:
        client.images.get(tag)
    except ImageNotFound:
        root, parent = RECIPES[profile]
        buildargs = {"BASE_IMAGE": ensure_image(client, parent)} if parent is not None else None
        logfire.info("building frame image {tag}", tag=tag)
        client.images.build(path=str(root), tag=tag, rm=True, buildargs=buildargs)
    _images_ready.add(tag)
    return tag
