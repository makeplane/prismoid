# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Where settings come from: the files they read, and the one config every group shares.

The leaf of `prismoid.conf`, so a group that lives in its own module (`conf_machines.py`) reads the same
files under the same rules.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic_settings import SettingsConfigDict

# The user's own config file (`prismoid up` on a Mac): the same KEY=value format as `.env`,
# read AFTER it so it wins over the repo's `.env`; real environment variables still win over both.
# A file that is not there is skipped silently (pydantic-settings), and `~` is expanded at read time.
# `PRISMOID_CONFIG_FILE` points elsewhere - the test suite uses it so a developer's own file never leaks in.
USER_CONFIG = os.environ.get("PRISMOID_CONFIG_FILE", "~/.prismoid/config")


def env_files(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The files settings read. A deployed service reads no file of the home it happens to run in:
    the deploys say `PRISMOID_ENV` in the real environment (both spellings count, as `environment` reads
    them), which is what is asked here, before any settings exist."""
    named = environ.get("PRISMOID_ENV") or environ.get("PRISMOID_ENVIRONMENT")
    return (".env",) if named in ("staging", "prod") else (".env", USER_CONFIG)


def cfg(prefix: str = "") -> SettingsConfigDict:
    """One config, one prefix per group.

    `enable_decoding=False` is load-bearing: pydantic-settings JSON-decodes any field whose type
    is complex (list, dict) *before* validators run, so a plain `a,b` in .env dies inside
    json.loads and a BeforeValidator never sees it. Off, the raw string reaches the validators
    in `prismoid.conf` - which is the whole point of a flat .env file.

    `hide_input_in_errors=True` is load-bearing too: the dotenv source puts every `.env` line no
    field claims into the input dict, prefix or not, and a validator's error printed that dict -
    so a failed boot wrote the tail of the last line's secret (a provider key) into the log.
    The error keeps its message, which names the setting and never its value.
    """
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=env_files(os.environ),
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
        hide_input_in_errors=True,
    )
