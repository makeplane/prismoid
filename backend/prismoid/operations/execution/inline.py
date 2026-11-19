# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The files a step brief carries in (`ai/context/inline`) and what the frameless Lead reads of its turn's
attachments (`lead_files`), within byte and token budgets; read from the skill registry and storage, never a frame.
Best-effort: anything not read stays a file the model is told to read; the Lead's documents convert only in the sidecar.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.context.inline import Included, LeadRead
from prismoid.ai.frames.layout import SKILLS, input_path
from prismoid.ai.skills.format import render
from prismoid.ai.tools import convert
from prismoid.db.models.task import Task
from prismoid.operations import skills as skill_ops
from prismoid.operations.turn.intake import lead_opens
from prismoid.schemas.egress import strip_control
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.tokens import TOKENS

# An attachment larger than this is named, not inlined: a long file is read a window at a time.
INLINE_MAX_BYTES: Final = 4096
# What the Lead reads of one turn's attachments, all files together; past it, a step reads them.
READ_TOKENS: Final = 16_000
# A skill is guidance, usually 2-6 KB; one past this is named and read like before.
SKILL_INLINE_MAX_BYTES = 4 * INLINE_MAX_BYTES
# A person is waiting on the Lead: a document that takes longer to convert is a step's to read.
LEAD_CONVERT_TIMEOUT_S = 15.0
# One character past the Lead's budget, so a cut conversion never fits it.
_LEAD_MAX_CHARS = READ_TOKENS * 4 + 1


def inline_readable(item: TaskInput) -> bool:
    """Whether an input part rides IN the brief: text of at most `INLINE_MAX_BYTES`."""
    return item.is_text and item.bytes <= INLINE_MAX_BYTES


async def included_files(session: AsyncSession, task: Task, spec: StepSpec) -> list[Included]:
    """The step's pinned skills, then the task's inline-readable parts (`inline_readable`) the
    contract names - each as the text the mount puts at its path."""
    files: list[Included] = []
    try:
        for pin in spec.skill_pins:
            resolved = await skill_ops.resolve_pin(session, pin)
            text = render(resolved.manifest, resolved.body)
            if len(text.encode()) <= SKILL_INLINE_MAX_BYTES:
                files.append(Included(f"{SKILLS}/{resolved.name}/SKILL.md", text, data=False))
        named = set(spec.contract.inputs) if spec.contract is not None else set()
        for raw in task.inputs:
            item = TaskInput.model_validate(raw)
            path = input_path(item.filename)
            if path not in named or not inline_readable(item):
                continue
            try:
                text = (await asyncio.to_thread(storage.get, item.sha256)).decode()
            except UnicodeDecodeError:
                continue  # not UTF-8: read_file decides what it is
            files.append(Included(path, text, data=True))
    except Exception:
        logfire.exception("the brief could not inline its files; the step reads them")
        return []
    return files


async def lead_files(items: Sequence[TaskInput]) -> LeadRead:
    """What the Lead reads of a turn's attachments, in their order; one that cannot be read is counted."""
    texts = [(input_path(item.filename), await _lead_text(item)) for item in items]
    return lead_read(texts)


def lead_read(texts: Sequence[tuple[str, str | None]]) -> LeadRead:
    """Each (path, text or None when unread) in attachment order, kept while the turn's files fit `READ_TOKENS`."""
    files: list[Included] = []
    left = READ_TOKENS
    for path, text in texts:
        if text is not None and (cost := TOKENS.count(text)) <= left:
            files.append(Included(path, text, data=True))
            left -= cost
    return LeadRead(tuple(files), len(texts) - len(files))


async def _lead_text(item: TaskInput) -> str | None:
    """One attachment's text for the Lead, or None. Fail-soft: a person is waiting, and a step reads it."""
    if not lead_opens(item):
        return None
    try:
        data = await asyncio.to_thread(storage.get, item.sha256)
        if item.is_text:
            return strip_control(data.decode())
        extension = convert.CONVERTS[item.media_type]
        page = await convert.convert(
            data,
            extension=extension,
            media_type=item.media_type,
            max_chars=_LEAD_MAX_CHARS,
            timeout_s=LEAD_CONVERT_TIMEOUT_S,
        )
    except (UnicodeDecodeError, convert.ConvertFailed):
        return None
    except Exception:
        logfire.exception("an attachment could not be read for the Lead; a step reads it")
        return None
    return strip_control(page.markdown)
