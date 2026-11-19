# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The running agent run's fence nonce: the taint capability sets it for the run, and every fence the
run writes wears it (per run, so no static prompt text carries it; unset, a fence wears none)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from prismoid.schemas import egress

_NONCE: ContextVar[str | None] = ContextVar("prismoid_fence_nonce", default=None)


@contextmanager
def fence_nonce(nonce: str) -> Generator[None]:
    """Fence everything inside with `nonce` on its data lines: a page cannot know it in advance."""
    token = _NONCE.set(nonce)
    try:
        yield
    finally:
        _NONCE.reset(token)


def fenced(title: str, warning: str, body: str, *, heading: str = "") -> str:
    """`schemas.egress.fenced` wearing the running run's nonce."""
    return egress.fenced(title, warning, body, heading=heading, nonce=_NONCE.get())
