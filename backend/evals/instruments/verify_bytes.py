# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Run the document suite over two local files, in a real frame. The negative half of M2.1b.

`prismoid artifacts verify` can only ever show you the suite passing work the system itself
produced. That is a demo, not a test. **A verifier that has only been shown correct work is a
verifier nobody has tested** - and this project has already shipped one of those (an early egress
check, green for two micros because the tool it invoked was not installed).

So this takes any document and any sources off the disk, puts them in a fresh sandbox exactly as
the pipeline does, and prints what the suite makes of them. Exit 1 if the verdict is `pass`, because
here we are asking it to catch a fabrication.

    uv run python evals/instruments/verify_bytes.py /tmp/fabricated.md tests/fixtures/atlas-q3.md
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.layout import input_path
from prismoid.ai.verification import pipeline
from prismoid.schemas.verification import CheckStatus, VerifierSuite


async def main() -> int:
    document = Path(sys.argv[1])
    sources = [Path(p) for p in sys.argv[2:]]

    payload = await asyncio.to_thread(document.read_bytes)
    ensure_image()
    frame = DockerFrame(task_id=f"verify-bytes-{hashlib.sha256(payload).hexdigest()[:8]}")
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
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            sources=source_paths,
            allow_domains=[],
        )
    finally:
        await frame.destroy()

    verdict = pipeline.verdict_of(checks)
    mark = {CheckStatus.PASS: "✓", CheckStatus.FAIL: "✗", CheckStatus.SKIP: "·"}
    print(f"  verdict: {verdict.value.upper()}")
    for check in checks:
        print(f"  {mark[check.status]} {check.name:<20} {check.detail[:100]}")
        if check.actual:
            print(f"      → {check.actual}")

    return 1 if verdict.value == "pass" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
