# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid artifacts show|verify|download|revisions|restore|label` - what tasks produced, its
provenance and its verification report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import typer

from prismoid.runner import client
from prismoid.schemas.model_class import Family, JudgeFamily

artifacts = typer.Typer(
    help="Artifacts: show, download, verify, revisions, restore, label.", no_args_is_help=True
)


@artifacts.command("show")
def artifacts_show(
    artifact_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """An artifact and its provenance - who made it, with which model, from which sources."""
    a = client.request("GET", f"/v1/artifacts/{artifact_id}", as_json=json_out)
    p = a["provenance"]
    human = (
        f"{a['id']}\n"
        f"  title:   {a['title']}\n"
        f"  type:    {a['type']}\n"
        f"  sha256:  {a['sha256'][:32]}…\n"
        f"  bytes:   {a['bytes']:,}\n"
        f"  agent:   {p.get('agent') or '-'}\n"
        f"  model:   {p.get('model') or '-'}\n"
        f"  sources: {p.get('sources') or []}\n"
        f"  verdict: {a['verdict'] or 'not verified yet (M2)'}"
    )
    if a["type"] == "collection" and not json_out:
        human += "\n" + _collection_preview(artifact_id, int(a["bytes"]))
    client.emit(a, json_out, human=human)


def _collection_preview(artifact_id: str, size: int) -> str:
    """Fetch the collection body via the presigned URL and summarise it - best-effort: any
    network or parse failure is a `preview: unavailable` line, never a failed command."""
    if size > 2_000_000:
        return "  preview: skipped (over 2 MB)"
    try:
        download = client.request("GET", f"/v1/artifacts/{artifact_id}/download", as_json=False)
        body = httpx.get(download["url"], follow_redirects=True, timeout=15.0)
        body.raise_for_status()
        return collection_summary(json.loads(body.content))
    except Exception:
        return "  preview: unavailable"


def _dicts(value: object) -> list[dict[str, Any]]:
    """The objects in a JSON list; anything else in it, or a part that is not a list, reads empty."""
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def collection_summary(doc: dict[str, object]) -> str:
    """The human summary of a collection - fields, rows, views, and a small head of the table.
    Pure (no network), so it is unit-testable beside `_show`'s plan tree."""
    fields, rows, views = (_dicts(doc.get(part)) for part in ("fields", "rows", "views"))

    field_line = ", ".join(f"{f.get('label', f.get('key'))}({f.get('type')})" for f in fields[:8])
    if len(fields) > 8:
        field_line += f" …+{len(fields) - 8}"
    view_line = ", ".join(f"{v.get('name')} ({v.get('layout')})" for v in views)

    lines = [
        f"  fields:  {field_line}",
        f"  rows:    {len(rows)}",
        f"  views:   {view_line}",
    ]

    head = fields[:4]
    if head and rows:
        keys = [str(f.get("key")) for f in head]
        widths = [max(len(str(f.get("label", k))), 12) for f, k in zip(head, keys, strict=True)]

        def cell(value: object) -> str:
            if value is None:
                return "-"
            if isinstance(value, list):
                return ", ".join(str(v) for v in value)
            return str(value)

        header = "  ".join(
            str(f.get("label", k)).ljust(w)[:20] for f, k, w in zip(head, keys, widths, strict=True)
        )
        lines += ["", "  " + header]
        for row in rows[:5]:
            cells = row.get("cells", {})
            cells = cells if isinstance(cells, dict) else {}
            line = "  ".join(cell(cells.get(k)).ljust(w)[:20] for k, w in zip(keys, widths, strict=True))
            lines.append("  " + line)
        if len(rows) > 5:
            lines.append(f"  … {len(rows) - 5} more row(s)")
    return "\n".join(lines)


@artifacts.command("verify")
def artifacts_verify(
    artifact_id: str = typer.Argument(...),
    assert_verified: bool = typer.Option(
        False, "--assert-verified", help="Exit non-zero unless the verdict is PASS (for CI/scripts)."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The verification report: what the suite re-derived in a fresh sandbox, who judged it against the
    task's criteria, and the signature that keeps it from being quietly edited."""
    v = client.request("GET", f"/v1/artifacts/{artifact_id}/verification", as_json=json_out)

    mark = {"pass": "✓", "fail": "✗", "skip": "·"}
    lines = [
        f"verdict:  {v['verdict'].upper()}",
        f"suite:    {v['suite']}",
        f"produced: {v['producer_model']}  (family {v['producer_family']})",
    ]
    if v.get("verifier_model"):
        seat = JudgeFamily.between(
            maker=v["producer_model"],
            maker_family=Family(v["producer_family"]),
            judge=v["verifier_model"],
            judge_family=Family(v["verifier_family"]),
        )
        lines.append(f"judged:   {v['verifier_model']}  (family {v['verifier_family']})  {seat}")
    else:
        lines.append(f"judged:   - {JudgeFamily.NONE} (see the log)")

    valid = v["signature_valid"]
    lines.append(
        "signed:   not signed yet (M2.3)"
        if valid is None
        else f"signed:   {v['algorithm']}  signature_valid: {str(valid).lower()}"
    )

    lines.append("")
    lines.append("checks:")
    for c in v["checks"]:
        lines.append(f"  {mark.get(c['status'], '?')} {c['name']:<20} {c['detail']}")
        # The finding, not just the failure. "numbers:grounded failed" is a shrug; "9.9 is not in
        # any source" is something you can go and look at.
        if c.get("actual"):
            lines.append(f"      → {c['actual']}")

    if v.get("judge_rationale"):
        lines += ["", "the judge:", *(f"  {line}" for line in v["judge_rationale"].splitlines())]

    client.emit(v, json_out, human="\n".join(lines))

    # **`--assert-verified` turns the verdict into an exit code** - so a script or a `just` recipe can
    # gate on "did this actually verify" without parsing prose. `pass` and nothing else exits 0.
    if assert_verified and str(v["verdict"]).lower() != "pass":
        raise typer.Exit(1)


@artifacts.command("download")
def artifacts_download(
    artifact_id: str = typer.Argument(...),
    out: str = typer.Option("", "-o", "--out", help="Where to write it."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Download an artifact. The link is presigned and content-addressed."""
    import httpx

    d = client.request("GET", f"/v1/artifacts/{artifact_id}/download", as_json=json_out)
    path = out or d["filename"]
    body = httpx.get(d["url"], timeout=60.0, follow_redirects=True)
    if body.status_code >= 400:
        client.fail(f"download failed ({body.status_code})", as_json=json_out)

    # The key IS the hash the verification attests to - so a download that does not hash to the
    # sha256 we were promised is not the artifact, and saying so here costs one line.
    import hashlib

    got = hashlib.sha256(body.content).hexdigest()
    if got != d["sha256"]:
        client.fail(f"hash mismatch: expected {d['sha256'][:12]}, got {got[:12]}", as_json=json_out)

    Path(path).write_bytes(body.content)
    client.emit(
        {"path": path, "bytes": len(body.content), "sha256": got},
        json_out,
        human=f"wrote {path} ({len(body.content):,} bytes, sha256 verified)",
    )


@artifacts.command("revisions")
def artifacts_revisions(
    artifact_id: str = typer.Argument(..., help="Any revision on the lineage."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The lineage, newest first: who made each revision, when, labels."""
    page = client.request("GET", f"/v1/artifacts/{artifact_id}/revisions", as_json=json_out)
    lines = []
    for i, r in enumerate(page["items"]):  # newest first - the first row IS the head
        who = r["provenance"].get("agent") or "?"
        label = f"  «{r['label']}»" if r.get("label") else ""
        head = " · latest" if i == 0 else ""
        lines.append(f"v{r['revision']}  {r['id']}  {who}  {r['sha256'][:12]}{label}{head}")
    client.emit(page, json_out, human="\n".join(lines) or "no revisions")


@artifacts.command("restore")
def artifacts_restore(
    artifact_id: str = typer.Argument(..., help="The revision to bring back."),
    base: str = typer.Option(..., "--base", help="The CURRENT head's sha256 (from `revisions`)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Restore by minting: a new head carries the old bytes; nothing is overwritten."""
    r = client.request(
        "POST", f"/v1/artifacts/{artifact_id}/restore", body={"base_sha256": base}, as_json=json_out
    )
    client.emit(r, json_out, human=f"restored as v{r['revision']} ({r['id']})")


@artifacts.command("label")
def artifacts_label(
    artifact_id: str = typer.Argument(..., help="The revision to name."),
    label: str = typer.Argument("", help="The name - empty with --clear removes it."),
    clear: bool = typer.Option(False, "--clear", help="Remove the label."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Name a revision - "final draft" in every history picker."""
    if not clear and not label:
        client.fail("give a label, or --clear to remove one", as_json=json_out)
    if clear and label:
        # Contradictory pair: silently clearing would discard the typed name.
        client.fail("give a label OR --clear - not both", as_json=json_out)
    r = client.request(
        "POST",
        f"/v1/artifacts/{artifact_id}/label",
        body={"label": None if clear else label},
        as_json=json_out,
    )
    client.emit(
        r,
        json_out,
        human=(
            f"v{r['revision']} labelled «{r['label']}»"
            if r.get("label")
            else f"v{r['revision']} label cleared"
        ),
    )
