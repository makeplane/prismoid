# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid models` - adding a model is data, not a deploy, as a command.

    prismoid models add xai:grok-5-fast \\
      --classes fast-executor,frontier-reasoner \\
      --family xai --context 256000 \\
      --price input_token=0.20/M output_token=0.80/M \\
      --evaluate

One line. No deploy. Routed on the next request.

The `--price …/M` suffix is not sugar: providers quote per *million* tokens and humans think in
those units, while the ledger multiplies per *single* unit. Doing that division here, once, is why
nobody ever has to type `0.0000002`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import typer

from prismoid.runner import client

if TYPE_CHECKING:  # heavy imports (pydantic_ai) stay lazy so `prismoid models list` starts fast
    from prismoid.ai.gateway.conformance import ConformanceReport
    from prismoid.schemas.model_class import ModelClass


@dataclass(slots=True)
class _EvalOutcome:
    """What `_evaluate` hands the command: a report, or an error to print. Never both."""

    report: ConformanceReport | None = None
    claimed: frozenset[ModelClass] = field(default_factory=frozenset)
    error: str = ""


models = typer.Typer(help="The model catalog. Adding a model is data, not a deploy.", no_args_is_help=True)


def _parse_price(spec: str) -> tuple[str, Decimal]:
    """`input_token=0.20/M` → (`input_token`, Decimal('0.0000002'))."""
    unit, _, value = spec.partition("=")
    value = value.strip()
    if value.endswith("/M"):
        return unit.strip(), Decimal(value[:-2]) / Decimal(1_000_000)
    if value.endswith("/K"):
        return unit.strip(), Decimal(value[:-2]) / Decimal(1_000)
    return unit.strip(), Decimal(value)


@models.command("list")
def models_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Every model the catalog knows, and whether it is routable."""
    rows = client.request("GET", "/v1/admin/models", as_json=json_out)
    lines = [
        f"  {'✓' if r['enabled'] else '·'} {r['ref']:<28} {r['family']:<10} {','.join(r['classes'])}"
        for r in rows
    ]
    client.emit(rows, json_out, human="\n".join(lines) or "no models - `prismoid models add …`")


@models.command("seed")
def models_seed(
    file: str = typer.Option("", "--file", help="A catalog YAML; defaults to the shipped starter set."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Seed a starter cloud catalog (idempotent; never overwrites an operator's edit) so a fresh deployment
    can route. Verify its prices; `--file` seeds your own YAML; each model needs its provider key."""
    n, chains, reclassed = asyncio.run(_seed_models(file or None))
    client.emit(
        {"seeded": n, "chains": chains, "reclassed": reclassed},
        json_out,
        human=f"seeded {n} model(s), {chains} routing chain(s), re-classed {reclassed} row(s)"
        if n or chains or reclassed
        else "catalog already has these models",
    )


async def _seed_models(path: str | None) -> tuple[int, int, int]:
    from pathlib import Path

    import yaml

    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops
    from prismoid.operations import routing as route_ops

    src = Path(path) if path else Path(__file__).parent / "default_models.yaml"
    data = yaml.safe_load(src.read_text())
    data = data if isinstance(data, dict) else {}
    entries = list(data.get("models") or [])
    was = data.get("superseded") or {}
    async with session_scope() as session:
        added = await ops.seed_catalog(session, entries)
        reclassed = await ops.reclass_untouched(session, entries, was.get("classes") or {})
        chains = await route_ops.seed_routing(
            session,
            data.get("routing") or {},
            settings=data.get("settings") or {},
            superseded=was.get("routing") or {},
            superseded_settings=was.get("settings") or {},
        )
        return added, chains, reclassed


@models.command("add")
def models_add(
    ref: str = typer.Argument(..., help="provider:model_id, e.g. anthropic:claude-… or local:qwen2.5:3b"),
    classes: str = typer.Option(..., "--classes", help="Comma-separated model classes."),
    family: str = typer.Option("", "--family", help="Defaults to the provider."),
    protocol: str = typer.Option("", "--protocol", help="anthropic|openai|google|openai_compatible"),
    context: int = typer.Option(0, "--context"),
    base_url: str = typer.Option("", "--base-url", help="For any OpenAI-compatible provider (T2)."),
    price: list[str] = typer.Option([], "--price", help="unit=usd, e.g. input_token=0.20/M"),
    evaluate: bool = typer.Option(False, "--evaluate", help="Run the conformance check right after adding."),
    enable: bool = typer.Option(False, "--enable", help="Route to it immediately."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Add a model. **Disabled by default** - `--evaluate` scores it, `--enable` trusts it."""
    provider, _, model_id = ref.partition(":")
    if not model_id:
        client.fail("ref must be provider:model_id", as_json=json_out)

    body = {
        "provider": provider,
        "model_id": model_id,
        "family": family or provider,
        "protocol": protocol or ("openai_compatible" if base_url else provider),
        "classes": [c.strip() for c in classes.split(",") if c.strip()],
        "context_window": context,
        "base_url": base_url or None,
        "prices": {unit: str(value) for unit, value in (_parse_price(p) for p in price)},
    }
    result = client.request("POST", "/v1/admin/models", body=body, as_json=json_out)
    if enable:  # the catalog adds disabled; --enable is the second, deliberate act
        result = client.request("PATCH", f"/v1/admin/models/{ref}", body={"enabled": True}, as_json=json_out)
    client.emit(
        result,
        json_out,
        human=f"added {ref}  ({'enabled' if enable else 'disabled - enable it with `prismoid models enable ' + ref + '`'})",
    )
    if evaluate and not json_out:  # score it now; the standalone `models evaluate --json` is the JSON path
        outcome = asyncio.run(_evaluate(ref, None))
        typer.echo("")
        typer.echo(
            outcome.error if outcome.report is None else _render_report(outcome.report, outcome.claimed)
        )


@models.command("evaluate")
def models_evaluate(
    ref: str = typer.Argument(..., help="provider:model_id"),
    only: str = typer.Option("", "--class", help="Score just one class; default scores every class."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the conformance check on a model, per class, in this process; a disabled model can be scored."""
    outcome = asyncio.run(_evaluate(ref, only or None))
    if outcome.report is None:
        client.fail(outcome.error, as_json=json_out)
        return  # unreachable - client.fail raises - but it tells pyright report is not None below
    client.emit(_report_json(outcome.report), json_out, human=_render_report(outcome.report, outcome.claimed))


async def _evaluate(ref: str, only: str | None) -> _EvalOutcome:
    from prismoid.ai.gateway.conformance import run_conformance
    from prismoid.ai.gateway.meter import MeterContext
    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops
    from prismoid.schemas.model_class import ModelClass
    from prismoid.schemas.usage import UsageRecord

    async with session_scope() as session:
        catalog = await ops.catalog(session, enabled_only=False)  # score a model that is still disabled
    spec = next((m for m in catalog if m.ref == ref), None)
    if spec is None:
        return _EvalOutcome(error=f"no model {ref!r} in the catalog - `prismoid models add …` first.")
    try:
        classes = (ModelClass(only),) if only else tuple(ModelClass)
    except ValueError:
        return _EvalOutcome(error=f"unknown model class {only!r}")

    async def _drop(_record: UsageRecord) -> None:  # the report carries cost/metered; nothing to persist
        return None

    report = await run_conformance(spec, classes, emit_usage=_drop, context=MeterContext())
    return _EvalOutcome(report=report, claimed=frozenset(spec.classes))


def _render_report(report: ConformanceReport, claimed: frozenset[ModelClass]) -> str:
    """Per-class scores, the number an operator reads before the deliberate `enable`."""
    lines = [f"  conformance · model {report.ref}", "", "  claim  class               score"]
    for s in report.scores:
        mark = "✓" if s.model_class in claimed else "·"
        if s.total == 0:
            lines.append(f"   {mark}     {s.model_class.value:<18} -      {s.note}")
            continue
        filled = round(s.ratio * 5)
        bar = "●" * filled + "○" * (5 - filled)
        lines.append(
            f"   {mark}     {s.model_class.value:<18} {s.passed}/{s.total}   {s.ratio:.2f}  {bar}  {s.note}"
        )
    meter = "metered ✓" if report.metered else "NOT metered ✗ - a price-wiring hole (Rule 8)"
    lines += [
        "",
        f"  {report.calls} calls · ${report.cost_usd} · {meter}  ·  ✓ = the model claims this class",
    ]
    return "\n".join(lines)


def _report_json(report: ConformanceReport) -> dict[str, object]:
    return {
        "ref": report.ref,
        "calls": report.calls,
        "cost_usd": report.cost_usd,
        "metered": report.metered,
        "scores": [
            {
                "class": s.model_class.value,
                "passed": s.passed,
                "total": s.total,
                "ratio": round(s.ratio, 3),
                "note": s.note,
            }
            for s in report.scores
        ],
    }


@models.command("classes")
def models_classes(
    ref: str = typer.Argument(..., help="provider:model_id"),
    classes: str = typer.Option(..., "--classes", help="Comma-separated model classes."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Re-class a model. A model that cannot execute tools should not be a `fast-executor`.

    A thin client of PATCH /admin/models."""
    names = [c.strip() for c in classes.split(",") if c.strip()]
    r = client.request("PATCH", f"/v1/admin/models/{ref}", body={"classes": names}, as_json=json_out)
    client.emit(r, json_out, human=f"{ref} now serves {names}")


@models.command("enable")
def models_enable(ref: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Start routing to a model."""
    r = client.request("PATCH", f"/v1/admin/models/{ref}", body={"enabled": True}, as_json=json_out)
    client.emit(r, json_out, human=f"enabled {ref}")


@models.command("disable")
def models_disable(ref: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Stop routing to a model. It stays in the catalog, and its ledger rows stay priced."""
    r = client.request("PATCH", f"/v1/admin/models/{ref}", body={"enabled": False}, as_json=json_out)
    client.emit(r, json_out, human=f"disabled {ref}")


@models.command("import")
def models_import(
    file: str = typer.Argument(..., help="A catalog YAML (the seed file's shape)."),
    reprice: bool = typer.Option(
        False, "--reprice", help="Route CHANGED prices through a new price-book version (N+1)."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Upsert the catalog from YAML - fleet management at ~70 rows.

    Seed is first-run; import is maintenance: it updates classes/context/base_url on existing rows
    too. It NEVER touches `enabled` on an existing row, and it refuses a silently changed price -
    a changed price is a reprice (`--reprice` writes book version N+1; history stays priced)."""
    from pathlib import Path

    import yaml

    # Read in the SYNC command, not the async half - a blocking read has no place on a loop.
    data = yaml.safe_load(Path(file).read_text())
    entries = list(data.get("models", []) if isinstance(data, dict) else [])
    result = asyncio.run(_import_models(entries, reprice))
    client.emit(
        result,
        json_out,
        human=f"imported: {result['added']} added · {result['updated']} updated · {result['repriced']} repriced",
    )


async def _import_models(entries: list[dict[str, object]], reprice: bool) -> dict[str, int]:
    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops

    async with session_scope() as session:
        return await ops.import_catalog(session, entries, reprice=reprice)


@models.command("doctor")
def models_doctor(json_out: bool = typer.Option(False, "--json")) -> None:
    """The catalog's health: enabled-but-unpriced, missing keys, unserved classes, dead routing
    refs. Instrument-style (DB + local env, like `evaluate`); every finding names its fix.
    Exit 1 when anything is found, so CI can hold the door."""
    findings = asyncio.run(_doctor())
    if not findings:
        client.emit(
            {"findings": []}, json_out, human="catalog clean - every enabled row priced, keyed, served"
        )
        return
    lines = [f"  ✗ {f['check']:<22} {f['ref']}\n      {f['detail']}" for f in findings]
    client.emit({"findings": findings}, json_out, human="\n".join(lines))
    raise typer.Exit(1)


async def _doctor() -> list[dict[str, str]]:
    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops

    async with session_scope() as session:
        return await ops.doctor(session)
