# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The config a frame's commands read, which the helper writes for them: git's and Docker's. The
helper is not sandboxed and a frame's Docker folder is a write root, so what lands there lands by a
path nothing can re-aim."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import subprocess
from pathlib import Path
from typing import Final

GIT_CONFIG_FILE: Final = "gitconfig"
_DOCKER_CONFIG: Final = "config.json"
_NO_REGISTRY: Final = "prismoid.invalid"  # the frame's Docker config's placeholder auth


def _git_value(text: str) -> str | None:
    """`text` as a quoted git config value; None for one with a control character."""
    if re.search(r"[\x00-\x1f\x7f]", text):
        return None
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_gitconfig(base: Path) -> Path:
    """The frame's own global git config (`GIT_CONFIG_GLOBAL`), 0600: the user's name and email,
    copied from their git config, and nothing else of theirs - no `insteadOf`, credential helper,
    `hooksPath` or fsmonitor reaches a fenced command. Its own repositories run no hooks, and a
    bare repository is used only when named."""
    lines: list[str] = []
    for key in ("name", "email"):
        try:
            value = subprocess.run(
                ["git", "config", "--global", "--includes", "--get", f"user.{key}"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
        if value and (quoted := _git_value(value)):
            lines.append(f"\t{key} = {quoted}")
    text = ("[user]\n" + "\n".join(lines) + "\n" if lines else "") + (
        "[safe]\n\tbareRepository = explicit\n[core]\n\thooksPath = /dev/null\n"
    )
    path = base / GIT_CONFIG_FILE
    tmp = base / f".{GIT_CONFIG_FILE}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def write_docker_config(folder: Path) -> None:
    """The frame's own Docker config: the user's CLI plugins (`docker compose`, `buildx`), which the
    CLI looks for beside its config, and no credentials. A config with no auth at all makes the CLI
    ask the keychain's credential helper, which the fence denies and every pull then fails on; one
    placeholder entry keeps it on this file's (empty) store. What a frame keeps there stays, but a
    link is a command's: followed, this write would land at any path the user can create."""
    folder.mkdir(mode=0o700, exist_ok=True)
    config = folder / _DOCKER_CONFIG
    if config.is_symlink():
        config.unlink()
    plugins = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".docker", "cli-plugins")
    with contextlib.suppress(FileExistsError):
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"auths": {_NO_REGISTRY: {}}, "cliPluginsExtraDirs": [plugins]}, f)
