# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Crisis lines by country, as data: a reply to someone at risk names the line where they are, never another
country's. Keyed by the country names of the tz tables' `iso3166.tab` (`blocks.region`). Unlisted or
unknown, the reply points to the international directory and asks where the person is."""

from __future__ import annotations

from typing import Final, NamedTuple


class CrisisLine(NamedTuple):
    service: str
    number: str


# Free, 24/7 national lines. A change is reviewed against the service's own site and findahelpline.com.
LINES: Final[dict[str, CrisisLine]] = {
    "India": CrisisLine("Tele-MANAS", "14416 or 1-800-891-4416"),
    "United States": CrisisLine("988 Suicide & Crisis Lifeline", "call or text 988"),
    "Canada": CrisisLine("9-8-8 Suicide Crisis Helpline", "call or text 988"),
    "Britain (UK)": CrisisLine("Samaritans", "116 123"),
    "Ireland": CrisisLine("Samaritans", "116 123"),
    "Australia": CrisisLine("Lifeline", "13 11 14"),
    "New Zealand": CrisisLine("Need to talk?", "call or text 1737"),
    "Germany": CrisisLine("TelefonSeelsorge", "0800 111 0 111"),
    "France": CrisisLine("3114 (numéro national de prévention du suicide)", "3114"),
}

DIRECTORY: Final = "findahelpline.com"


def crisis_fact(country: str | None) -> str:
    """One ambient-facts line: the crisis line for `country`, or the directory when it has none."""
    line = LINES.get(country or "")
    if line is not None:
        return (
            f"- If the person may be at risk of harming themselves, the crisis line where they are ({country}) is "
            f"{line.service}: {line.number}. Give that one, never another country's line; {DIRECTORY} lists others."
        )
    where = f"in {country}" if country else "where they are"
    return (
        f"- If the person may be at risk of harming themselves, no crisis line is on file for {where}: point to "
        f"{DIRECTORY} and ask which country they are in; never give another country's line as theirs."
    )
