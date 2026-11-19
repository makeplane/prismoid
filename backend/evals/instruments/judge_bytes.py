# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Run the full verification - suite **and** judge - over two local files.

The negative half of the verification proof, and it exists for the same reason
`verify_bytes.py` does: a judge that has only ever been shown correct work is a judge nobody has tested.

The case it is pointed at is the one M2.1b could not close. The deterministic suite proves a figure
is *derivable* and can never prove the derivation **means** anything - a real fabricated "47%" was
"grounded" as `(5.8M − 3.1M) / 5.8M × 100`, which is R&D headcount minus a building lease, over R&D
headcount. Legal arithmetic; meaningless quantity. **A reader sees it instantly.** That reader is
this.

    uv run python evals/instruments/judge_bytes.py <doc> <source>… --producer-family=anthropic \
        --producer-model=anthropic:<model_id>
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from prismoid.ai.agents import base as agents
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.layout import input_path
from prismoid.ai.gateway.resolve import judge_seats
from prismoid.ai.verification import pipeline
from prismoid.db.session import session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import models as model_ops
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import Family, JudgeFamily
from prismoid.schemas.verification import CheckStatus, JudgeVerdict, VerifierSuite


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    document, sources = Path(args[0]), [Path(p) for p in args[1:]]
    producer = Family(_flag("--producer-family", "anthropic"))
    producer_model = _flag("--producer-model", "")

    payload = await asyncio.to_thread(document.read_bytes)
    digest = hashlib.sha256(payload).hexdigest()

    # ── the deterministic half, in a fresh frame ──────────────────────────────────────────────
    ensure_image()
    frame = DockerFrame(task_id=f"judge-bytes-{digest[:8]}")
    await frame.create(allow_domains=[])
    try:
        artifact_path = pipeline.artifact_frame_path(document.name)
        await frame.write_bytes(artifact_path, payload, mode=0o444, owner="system")
        source_paths = []
        for source in sources:
            path = input_path(source.name)
            await frame.write_bytes(path, source.read_bytes(), mode=0o444, owner="system")
            source_paths.append(path)

        checks = await pipeline.run_suite(
            frame,
            VerifierSuite.DOCUMENT,
            artifact_path=artifact_path,
            artifact_sha256=digest,
            sources=source_paths,
            allow_domains=[],
        )
    finally:
        await frame.destroy()

    mark = {CheckStatus.PASS: "✓", CheckStatus.FAIL: "✗", CheckStatus.SKIP: "·"}
    suite_verdict = pipeline.verdict_of(checks)
    print(f"  the deterministic suite says: {suite_verdict.value.upper()}")
    for check in checks:
        print(f"  {mark[check.status]} {check.name:<20} {check.detail[:96]}")

    # ── the judge: the seat furthest from the producer, reading the same thing ────────────────
    async with session_scope() as session:
        catalog = await model_ops.catalog(session, enabled_only=True)

    seats = judge_seats(catalog, maker_family=producer, maker_model=producer_model)
    if seats.family is JudgeFamily.NONE:
        print("\n  no judge row can be built on this host - nothing to test")
        return 1
    candidates = seats.rows

    prompt = _prompt(payload, sources, checks)
    deps = TaskDeps(task_id=TaskId(__import__("uuid").uuid4()))

    for spec in candidates:
        try:
            result = await agents.build("judge", deps, [spec]).run(prompt)
        except ModelIncapable as exc:
            print(f"  {spec.ref} cannot produce a verdict ({str(exc)[:60]}) - next seat")
            continue

        verdict = result.output
        assert isinstance(verdict, JudgeVerdict)
        print(f"\n  judged by: {spec.ref}  ({seats.family})  ← producer family was {producer!r}")
        print(f"  the judge says: {verdict.verdict.value.upper()}")
        print(f"  {verdict.rationale[:400]}")
        if verdict.bad_derivations:
            print(f"  bad derivations it rejected: {verdict.bad_derivations}")
        if verdict.criteria_missed:
            print(f"  criteria missed: {verdict.criteria_missed}")

        return 0 if verdict.verdict.value == "fail" else 1

    print(f"  every {seats.family} candidate failed to produce a verdict")
    return 1


def _flag(name: str, default: str) -> str:
    """A `--name=value` flag's value, or the default."""
    return next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"{name}=")), default)


def _prompt(payload: bytes, sources: list[Path], checks: list[pipeline.Check]) -> str:
    lines = [
        "## The task",
        "Write a memo for the board on Atlas Robotics' Q3. Every figure must come from the brief.",
        "",
        "## What 'done' means",
        "- every factual claim, number and quotation comes from one of the provided sources",
        "",
        "## The deterministic suite already found this",
    ]
    for check in checks:
        lines.append(f"- [{check.status.value}] {check.name}: {check.detail}")
    lines += ["", "## The sources - everything the author was given"]
    for source in sources:
        lines += [f"### {source.name}", "```", source.read_text(), "```"]
    lines += ["", "## The deliverable", "```", payload.decode(), "```"]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
