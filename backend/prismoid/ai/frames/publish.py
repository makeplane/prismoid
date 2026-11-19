# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The publish door's shared half - validation, the in-frame runner and the manifest read.

Every driver publishes the same way, because the door is the PRODUCT's
contract, not the sandbox's: validate the declared type against the registry, copy the bytes
into `/artifacts`, hash them, append a manifest entry.

**The hand-off is injection-proof by construction.** The old shape interpolated the
model-authored `title`, `sources` and `path` into a bash string wrapping a `python3 -c`
program - a title containing a double-quote terminated the bash string, `$` expanded, and a
finished deliverable was refused over its own punctuation (and refused again on every retry,
until "nothing was published" failed the whole task). Now NOTHING model-authored ever touches
a shell: the runner below is a static program written into the frame with `write_bytes` (tar
transport, no shell), the arguments travel as a JSON file beside it, and the exec invokes a
fixed command line. A quote, a backtick, a `$(...)` in a title is just a character in a JSON
string.
"""

from __future__ import annotations

from typing import Any

import orjson

from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.frames.layout import MANIFEST
from prismoid.errors import PolicyDenied
from prismoid.schemas.outputs import DEVICE_ONLY, ArtifactType, accepts, spec_for

# Fixed frame paths - constants ours, never the model's. Overwritten per publish (idempotent).
RUNNER_PATH = "/tmp/.plane-publish.py"
ARGS_PATH = "/tmp/.plane-publish-args.json"


def validate_publish(type: str, path: str, *, on_host: bool = False) -> ArtifactType:
    """The door's type check, shared verbatim by every driver. Raises PolicyDenied with the
    fix in the message - the wrapper turns it into a ModelRetry, so the error IS the prompt.
    `on_host`: the frame is on the device; anywhere else a `DEVICE_ONLY` type is unknown."""
    known = [t for t in ArtifactType if on_host or t not in DEVICE_ONLY]
    try:
        artifact_type = ArtifactType(type)
        if artifact_type not in known:
            raise ValueError(type)
    except ValueError:
        raise PolicyDenied(f"unknown artifact type {type!r}. Known: {[t.value for t in known]}") from None

    if not accepts(artifact_type, path):
        raise PolicyDenied(
            f"{path!r} is not a valid {type} - {type} accepts "
            f"{list(spec_for(artifact_type).extensions) or 'a directory'}"
        )
    return artifact_type


def publish_args(
    path: str, target: str, manifest: str, type: str, title: str, sources: list[str]
) -> dict[str, object]:
    """The runner's whole input, as data. Serialized with the caller's JSON encoder."""
    return {
        "path": path,
        "target": target,
        "manifest": manifest,
        "type": type,
        "title": title,
        "sources": sources,
    }


async def read_manifest(frame: FrameDriver) -> dict[str, Any]:
    """`/artifacts/manifest.json` read through the frame's shell, empty when nothing was published."""
    result = await frame.exec(f"cat {MANIFEST} 2>/dev/null || echo '{{\"entries\": []}}'")
    return dict(orjson.loads(result.stdout or '{"entries": []}'))


# The static in-frame runner: copy (or tar a directory), hash, append the manifest entry.
# stdlib only - it runs inside the frame image. A missing source file exits 2 with a sentence
# the model can act on; the driver surfaces stderr inside its PolicyDenied.
PUBLISH_RUNNER = """\
import datetime
import hashlib
import json
import os
import shutil
import sys
import tarfile

args = json.load(open(sys.argv[1]))
path, target, manifest = args["path"], args["target"], args["manifest"]

os.makedirs(os.path.dirname(target), exist_ok=True)
if os.path.isdir(path):
    # A directory artifact (code, webapp) travels as a tar of the tree - one blob the
    # harvest hashes and the verify frame untars to rebuild.
    with tarfile.open(target, "w") as tf:
        tf.add(path, arcname=os.path.basename(path))
elif os.path.isfile(path):
    shutil.copyfile(path, target)
else:
    sys.stderr.write(path + ": no such file - write it (e.g. under /work) first, then publish\\n")
    sys.exit(2)

with open(target, "rb") as f:
    data = f.read()

entry = {
    "path": target,
    "type": args["type"],
    "title": args["title"],
    "sha256": hashlib.sha256(data).hexdigest(),
    "bytes": len(data),
    "sources": args["sources"],
    "agent": "",
    "model": "",
    "skill_pins": [],
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
entries = json.load(open(manifest))["entries"] if os.path.exists(manifest) else []
entries.append(entry)
json.dump({"entries": entries}, open(manifest, "w"), indent=2)
print(entry["sha256"][:16])
"""
