# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The license gate: every dependency's license lets Prismoid be distributed under AGPL-3.0-only.

`python tools/license_gate.py` (from backend/, CI's `security` job) lists the license of every
installed Python package (pip-licenses, run through uvx against this project's virtualenv) and
every production JS package of the pnpm workspace (`pnpm licenses list --prod`). Each license must
be one this file knows to be compatible with AGPL-3.0. A license known to be incompatible fails
(proprietary or missing, SSPL, BUSL, Elastic, Commons Clause, non-commercial or no-derivatives
terms, GPL-2.0-only), and so does any license the gate does not recognise: an unknown string is
reported for a person to read, never passed silently.

An SPDX `A OR B` passes when either side passes (the recipient may choose it); `A AND B`, and a
list pip-licenses joins with `;` or `,`, passes only when every part does. A package whose license
text the tools cannot read correctly gets an `ALLOWED` line naming the package and the reason.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PIP_LICENSES = "pip-licenses==5.5.5"
PROJECT = "prismoid"  # this repo's own package: its license is the one being protected, not a dependency

# (ecosystem, package name) -> why it is allowed. Every line is a decision someone reviewed.
ALLOWED: dict[tuple[str, str], str] = {
    ("python", "pypdfium2"): (
        "Apache-2.0 or BSD-3-Clause; the bundled PDFium binary is BSD-3-Clause and its third-party "
        "parts carry permissive licenses (FreeType taken under its GPL-2.0-or-later option)"
    ),
    ("npm", "khroma"): (
        "MIT (fabiospampinato/khroma, mermaid's colour library); its package.json has no `license` "
        "field, so without an installed tree to read its `license` file pnpm reports Unknown"
    ),
}

# Lower-cased license names and SPDX ids compatible with distributing the whole under AGPL-3.0:
# permissive licenses, weak copyleft, and the GPL-3.0 family itself.
COMPATIBLE = frozenset(
    {
        # permissive
        "0bsd",
        "3-clause bsd license",
        "apache software license",
        "apache-2.0",
        "apache license 2.0",
        "blueoak-1.0.0",
        "bsd",
        "bsd license",
        "bsd-2-clause",
        "bsd-3-clause",
        "cc-by-4.0",
        "cc0-1.0",
        "cnri-python",
        "isc",
        "isc license (iscl)",
        "mit",
        "mit license",
        "mit-0",
        "mit-cmu",
        "psf-2.0",
        "python software foundation license",
        "python-2.0",
        "the unlicense (unlicense)",
        "unlicense",
        "zlib",
        "zpl-2.1",
        # Fonts: shipped beside the program for the browser to load, never combined into it.
        "ofl-1.1",
        # weak copyleft
        "lgpl-2.1-only",
        "lgpl-2.1-or-later",
        "lgpl-3.0-only",
        "lgpl-3.0-or-later",
        "mozilla public license 2.0 (mpl 2.0)",
        "mpl-2.0",
        # the GPL-3.0 family
        "agpl-3.0-only",
        "agpl-3.0-or-later",
        "gpl-2.0-or-later",
        "gpl-3.0-only",
        "gpl-3.0-or-later",
    }
)

# Known to forbid distribution under AGPL-3.0: named so the report says why, not just "unknown".
_INCOMPATIBLE = re.compile(
    r"proprietary|unlicensed|^unknown$|^$|sspl|server side public|busl|business source|^bsl-1\.1"
    r"|elastic|commons clause|-nc\b|-nd\b|noncommercial|non-commercial|noderivatives"
    r"|^gpl-2\.0(-only)?$|^gplv2$",
    re.IGNORECASE,
)


def _split_top(expression: str, operator: str) -> list[str]:
    """Split on ` OR ` / ` AND ` outside parentheses."""
    parts: list[str] = []
    depth, start, i = 0, 0, 0
    token = f" {operator} "
    while i < len(expression):
        char = expression[i]
        depth += char == "("
        depth -= char == ")"
        if depth == 0 and expression[i : i + len(token)].upper() == token:
            parts.append(expression[start:i])
            start = i = i + len(token)
            continue
        i += 1
    parts.append(expression[start:])
    return [part.strip() for part in parts]


def _unwrap(expression: str) -> str:
    """`(A OR B)` -> `A OR B`, only when the outer pair encloses the whole expression."""
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for i, char in enumerate(expression):
            depth += (char == "(") - (char == ")")
            if depth == 0 and i < len(expression) - 1:
                return expression  # `(A) AND (B)`: the first pair closes early
        expression = expression[1:-1].strip()
    return expression


def verdict(license_text: str) -> str | None:
    """None when the license is compatible; otherwise why not ("incompatible: X" or "unrecognised: X")."""
    # A package that put its whole license text in the field: the first line names it.
    text = license_text.strip().splitlines()[0].strip() if license_text.strip() else ""
    text = _unwrap(text)
    alternatives = _split_top(text, "OR")
    if len(alternatives) > 1:
        reasons = [verdict(alternative) for alternative in alternatives]
        return None if any(r is None for r in reasons) else reasons[0]
    parts = re.split(r"\s*[;,]\s*", text) if ";" in text or "," in text else _split_top(text, "AND")
    if len(parts) > 1:
        return next((r for r in map(verdict, parts) if r is not None), None)
    if text.lower() in COMPATIBLE:
        return None
    if _INCOMPATIBLE.search(text):
        return f"incompatible: {text or 'no license'}"
    return f"unrecognised: {text}"


def python_packages() -> list[tuple[str, str, str]]:
    venv_python = BACKEND / ".venv" / "bin" / "python"
    out = subprocess.run(
        ["uvx", PIP_LICENSES, "--python", str(venv_python), "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [(p["Name"], p["Version"], p["License"]) for p in json.loads(out) if p["Name"] != PROJECT]


def js_packages() -> list[tuple[str, str, str]]:
    out = subprocess.run(
        ["pnpm", "licenses", "list", "--prod", "--json", "--recursive"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found: list[tuple[str, str, str]] = []
    for license_text, packages in json.loads(out or "{}").items():
        for package in packages:
            found.extend((package["name"], version, license_text) for version in package["versions"])
    return found


def main() -> int:
    inventory = [("python", *p) for p in python_packages()] + [("npm", *p) for p in js_packages()]
    failures: list[str] = []
    for ecosystem, name, version, license_text in sorted(inventory):
        reason = verdict(license_text)
        if reason is None:
            continue
        allowed = ALLOWED.get((ecosystem, name))
        if allowed is None:
            failures.append(f"  {ecosystem} {name} {version}: {reason}")
        else:
            print(f"  allowed {ecosystem} {name} {version} ({reason}): {allowed}")
    print(f"license gate: {len(inventory)} packages checked against AGPL-3.0-only")
    if failures:
        print("not known to be compatible (read the license; add an ALLOWED line with the reason):")
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
