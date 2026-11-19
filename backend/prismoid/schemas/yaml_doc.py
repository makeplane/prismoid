# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""YAML a person wrote, read safely: `SKILL.md` and `BOT.md` frontmatter and `cases.yaml`.

`yaml.safe_load` refuses tags but not aliases. A few hundred bytes of nested `&a [*b, *b, ...]` load
as shared references, then expand to billions of items the moment anything walks them (`str()` of a
field, a JSON dump) in the API's event loop. No skill or bot document needs an alias, so the loader
refuses the first one it meets.
"""

from __future__ import annotations

from typing import Any, cast

import yaml
from yaml.composer import ComposerError
from yaml.events import AliasEvent
from yaml.nodes import Node


class _NoAliases(yaml.SafeLoader):
    def compose_node(self, parent: Node | None, index: object) -> Node | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.check_event(AliasEvent):
            event = cast("AliasEvent", self.peek_event())  # the PyYAML stub leaves peek_event untyped
            raise ComposerError(None, None, "YAML aliases (*name) are not allowed here", event.start_mark)
        return super().compose_node(parent, index)  # pyright: ignore[reportArgumentType]


def load_untrusted(text: str) -> Any:
    """`yaml.safe_load` without aliases. Raises `yaml.YAMLError` on bad YAML or any alias."""
    return yaml.load(text, Loader=_NoAliases)
