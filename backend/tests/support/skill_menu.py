# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The menu `operations.skills.menu` freezes for an agent, built from the bundled skills instead of the
registry: what a fixture hands `TaskDeps.skill_menu` so `skill_read` mounts as it does in production."""

from __future__ import annotations

from prismoid import storage
from prismoid.ai.agents import grants
from prismoid.ai.skills.format import parse_dir
from prismoid.ai.tools.skills import SkillCard, menu_of
from prismoid.operations.skills import BUILTIN_DIR


def builtin_menu(agent: str) -> tuple[SkillCard, ...]:
    """`agent`'s granted skills at v1, each body named by its hash as the seeder stores it."""
    cards: list[SkillCard] = []
    for name in grants.SKILLS.get(agent, frozenset()):
        parsed = parse_dir(BUILTIN_DIR / name)
        sha = storage.sha256(parsed.body.encode())
        cards.append(
            SkillCard(name=name, version=1, description=parsed.manifest.description, body_sha256=sha)
        )
    return menu_of(cards)
