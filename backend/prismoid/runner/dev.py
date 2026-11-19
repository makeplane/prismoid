# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid dev` - the instruments.

`frame-selftest` exercises the whole sandbox lifecycle against a real container and prints the
timings the performance budgets care about. It is the command that answers "is the frame actually a
sandbox, or just
a container I have been telling myself is a sandbox".
"""

from __future__ import annotations

import asyncio
import io
import json as jsonlib
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, TypedDict

import logfire
import typer

from prismoid.schemas.machines import FrameKind, frame_labels

if TYPE_CHECKING:
    from prismoid.ai.frames.driver import Checkpoints

# Every frame is labelled: a self-test's frame that outlives its command (a ^C, a crash)
# is the reconciler's to destroy once it is older than a step can run.
SELFTEST_LABELS = frame_labels(FrameKind.SELFTEST)

dev = typer.Typer(
    help="Instruments: frame-selftest, screen-selftest, device-ping, gateway-ping, media-ping, seed.",
    no_args_is_help=True,
)


@dev.command("frame-selftest")
def frame_selftest(
    egress: bool = typer.Option(False, "--egress", help="Also prove the frame has no way out."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """create → exec → write → archive → destroy → unpack → destroy, with timings.

    The cold-start budget is **<2 s**. The egress check is the one that matters most: a
    sandbox that can reach the internet is not a sandbox.
    """
    result = asyncio.run(_closing(_selftest(egress)))
    if json_out:
        typer.echo(jsonlib.dumps(result, indent=2))
        raise typer.Exit(0 if result["ok"] else 1)

    for line in result["lines"]:
        typer.echo(line)
    raise typer.Exit(0 if result["ok"] else 1)


@dev.command("skill-frame")
def skill_frame(
    name: str = typer.Argument(..., help="A published skill to load, e.g. report"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Prove a skill body copies into a real frame.

    Boots a container, runs the **executor's own** `_materialise_skills` with a pin for `<name>`, then
    reads `/skills/<name>/SKILL.md` back out and confirms the bytes survived the round trip. This is
    the live half of the gate: the tests pin the logic, this pins it to a real Docker frame.
    """
    result = asyncio.run(_closing(_skill_frame(name)))
    if json_out:
        typer.echo(jsonlib.dumps(result, indent=2))
        raise typer.Exit(0 if result["ok"] else 1)
    for line in result["lines"]:
        typer.echo(line)
    raise typer.Exit(0 if result["ok"] else 1)


async def _closing[T](body: Coroutine[object, object, T]) -> T:
    """Run an instrument's body, then close this loop's driver clients if the body made one (a
    channel belongs to its loop, `frames.close_clients`). Never raises: a failed close must not eat the
    instrument's result."""
    try:
        return await body
    finally:
        try:
            from prismoid.ai.frames import close_clients

            await close_clients()
        except Exception as exc:
            logfire.warn("closing a frame driver client failed: {type}", type=type(exc).__name__)


class SkillFrameResult(TypedDict):
    ok: bool
    lines: list[str]


async def _skill_frame(name: str) -> SkillFrameResult:
    from prismoid.ai.frames import ensure_ready, new_frame
    from prismoid.db.session import session_scope
    from prismoid.operations.execution import materialise_skills
    from prismoid.schemas.contract import SkillPin
    from prismoid.schemas.ids import SkillName
    from prismoid.schemas.plan import StepSpec

    lines: list[str] = []
    ensure_ready()
    frame = new_frame(task_id=f"skill-frame-{int(time.time())}")
    ok = True
    try:
        await frame.create(allow_domains=[], labels=SELFTEST_LABELS)
        spec = StepSpec(skill_pins=[SkillPin(name=SkillName(name))])

        t0 = time.perf_counter()
        async with session_scope() as session:
            from prismoid.runner.bots_cli import cli_actor

            await cli_actor(session)  # binds the caller's workspace: skills resolve per workspace
            paths = await materialise_skills(session, spec, frame)
        load_ms = (time.perf_counter() - t0) * 1000
        if not paths:
            return {"ok": False, "lines": [f"  ✗ nothing materialised for {name!r} - is it published?"]}

        path = paths[0]
        lines.append(f"  materialise {load_ms:7.0f} ms   {path}")

        back = await frame.read_file(path)
        lines.append(f"  read back              {len(back.encode())} bytes")
        lines.append(f"    first line: {back.splitlines()[0]!r}")

        # The body is root-owned 0444: the agent cannot edit its own guidance in place (an agent that
        # could would be free to launder a hallucination into a fact it then cites).
        probe = await frame.exec(f"echo tampered >> {path} 2>&1 || echo 'DENIED (0444 ✓)'")
        read_only = "DENIED" in probe.stdout
        ok &= read_only
        lines.append(f"    {'✓' if read_only else '✗'} agent cannot edit in place: {probe.stdout.strip()}")
        return {"ok": ok, "lines": lines}
    finally:
        await frame.destroy()


class SelftestResult(TypedDict):
    ok: bool
    lines: list[str]
    cold_start_budget_ms: int


def _checkpoints(frame: object) -> Checkpoints:
    """The self-test's frame as one that carries `/work` out: a device frame keeps no checkpoint."""
    from prismoid.ai.frames.driver import Checkpoints

    assert isinstance(frame, Checkpoints), type(frame).__name__
    return frame


async def _selftest(check_egress: bool) -> SelftestResult:
    # Through the ONE seam (`new_frame`), never a hardcoded driver: the instrument that answers
    # "is the frame actually a sandbox" must answer for the driver the worker will actually use -
    # a selftest pinned to DockerFrame was blind to the sandbar path by construction.
    from prismoid.ai.frames import ensure_ready, new_frame
    from prismoid.ai.frames.layout import WORK
    from prismoid.conf import settings

    lines: list[str] = [f"  driver     {'':7}      {settings.worker.frame_driver}"]
    ok = True
    ensure_ready()

    frame = new_frame(task_id=f"selftest-{int(time.time())}")
    try:
        t0 = time.perf_counter()
        await frame.create(allow_domains=["example.com"], labels=SELFTEST_LABELS)
        cold_ms = (time.perf_counter() - t0) * 1000
        budget_ok = cold_ms < 2000
        ok &= budget_ok
        lines.append(f"  create     {cold_ms:7.0f} ms   {'✓' if budget_ok else '✗ OVER'} (§15: <2000 ms)")

        t0 = time.perf_counter()
        r = await frame.exec("echo hello from inside the frame")
        lines.append(f"  exec       {(time.perf_counter() - t0) * 1000:7.0f} ms   {r.stdout.strip()!r}")
        ok &= r.ok

        # The layout - four directories, and exactly one of them is harvestable.
        r = await frame.exec("ls -d /task /work /artifacts /skills")
        dirs = r.stdout.split()
        lines.append(f"  layout     {'':7}      {' '.join(dirs)}")
        ok &= sorted(dirs) == ["/artifacts", "/skills", "/task", "/work"]

        # Non-root: an agent with root inside its sandbox is one escape from root outside.
        r = await frame.exec("id -un && id -u")
        who = r.stdout.split()
        lines.append(
            f"  user       {'':7}      {who[0]} (uid {who[1]})   {'✓ non-root' if who[1] != '0' else '✗ ROOT'}"
        )
        ok &= who[1] != "0"

        # The toolchain is IN the image, because a frame that can pip install is a frame with a
        # way out.
        r = await frame.exec(
            "python -c 'import openpyxl, docx, pptx, pandas, reportlab, pypdf; print(\"toolchain ok\")'"
        )
        lines.append(f"  toolchain  {'':7}      {r.stdout.strip()}")
        ok &= r.ok

        await frame.write_file("/work/memo.md", "# Memo\n\nverification beats vibes.\n")
        content = await frame.read_file("/work/memo.md")
        lines.append(f"  write/read {'':7}      {content.splitlines()[0]!r}")
        ok &= "Memo" in content

        # The checkpoint: `/work` as a plain tar, streamed through the checkpoint filter.
        t0 = time.perf_counter()
        checkpoint = io.BytesIO()
        await _checkpoints(frame).archive(WORK, checkpoint)
        archive_ms = (time.perf_counter() - t0) * 1000
        lines.append(f"  archive    {archive_ms:7.0f} ms   {checkpoint.tell():,} bytes of {WORK}")
        ok &= checkpoint.tell() > 0

        if check_egress:
            lines.append("")
            lines.append("  egress - the ONLY way out is the proxy, and it answers per task:")

            # The tool must exist, or a "blocked" result means nothing. This check exists because
            # it did not, and the egress test passed for two micros because `curl` was missing from
            # the image. A security check that passes because the tool is absent is worse than none.
            probe = await frame.exec("command -v curl >/dev/null && echo present || echo MISSING")
            if "MISSING" in probe.stdout:
                lines.append("    ✗ curl is not in the frame image - this test would be meaningless")
                ok = False
            else:
                # ALLOWED: example.com is in this frame's allowlist.
                r = await frame.exec(
                    "curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://example.com || echo FAIL"
                )
                allowed_ok = r.stdout.strip().startswith("2") or r.stdout.strip().startswith("3")
                lines.append(
                    f"    https://example.com     (allowlisted) → {r.stdout.strip()}  "
                    f"{'✓ ALLOWED through the proxy' if allowed_ok else '✗ should have been allowed'}"
                )
                ok &= allowed_ok

                # DENIED: everything else. This is the line that makes it a sandbox.
                r = await frame.exec(
                    "curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://pypi.org || echo FAIL"
                )
                denied = not (r.stdout.strip().startswith("2") or r.stdout.strip().startswith("3"))
                lines.append(
                    f"    https://pypi.org        (NOT listed) → {r.stdout.strip()}  "
                    f"{'✓ DENIED' if denied else '✗ REACHED AN UN-ALLOWLISTED HOST'}"
                )
                ok &= denied

                # The denial is visible on the record - `tool.called ok=false`.
                denials = await frame.denials()
                if denials:
                    # The two drivers journal different shapes (docker: ip/host/reason; sandbar:
                    # host/method/at) - the instrument tolerates both rather than KeyErroring on
                    # the driver it exists to test.
                    d = denials[-1]
                    detail = d.get("reason") or d.get("method") or "?"
                    lines.append(
                        f"    → journalled: tool.called ok=false  host={d.get('host')!r} reason={detail!r}"
                    )
                ok &= bool(denials)

        await frame.destroy()

        # Unpack into a fresh frame - how a Machine wakes from its checkpoint.
        restored = new_frame(task_id=f"selftest-restore-{int(time.time())}")
        await restored.create(allow_domains=[], labels=SELFTEST_LABELS)
        t0 = time.perf_counter()
        checkpoint.seek(0)
        await _checkpoints(restored).unpack(WORK, checkpoint)
        unpack_ms = (time.perf_counter() - t0) * 1000
        back = await restored.read_file("/work/memo.md")
        lines.append("")
        lines.append(
            f"  unpack     {unpack_ms:7.0f} ms   recovered {back.splitlines()[0]!r} into a NEW frame"
        )
        ok &= "Memo" in back
        await restored.destroy()

    except Exception as exc:
        lines.append(f"  ✗ {type(exc).__name__}: {exc}")
        ok = False
        await frame.destroy()

    lines.append("")
    lines.append("  ✓ frame ok" if ok else "  ✗ frame FAILED")
    return SelftestResult(ok=ok, lines=lines, cold_start_budget_ms=2000)


class HelperHealth(TypedDict):
    ok: bool
    device_name: str | None
    socket: str
    error: str | None


async def _helper_health() -> HelperHealth:
    from prismoid.ai.frames.device_driver import helper_name
    from prismoid.ai.frames.driver import FrameUnreachable
    from prismoid.conf import settings

    socket = str(settings.local.home / "run" / "helper.sock")
    try:
        return HelperHealth(ok=True, device_name=await helper_name(), socket=socket, error=None)
    except FrameUnreachable as e:
        return HelperHealth(ok=False, device_name=None, socket=socket, error=str(e))


@dev.command("device-ping")
def device_ping(json_out: bool = typer.Option(False, "--json")) -> None:
    """Ask the device helper whether it is up: the socket and key this Prismoid would use, and the name
    of the computer that answered. `prismoid up` starts the helper; exit 1 when it does not answer.
    """
    result = asyncio.run(_helper_health())
    if json_out:
        typer.echo(jsonlib.dumps(result, indent=2))
        raise typer.Exit(0 if result["ok"] else 1)

    if not result["ok"]:
        typer.secho(f"error: {result['error']}", fg="red", err=True)
        raise typer.Exit(1)

    typer.echo(f"  device   {result['device_name']}")
    typer.secho(f"  ✓ helper up on {result['socket']}", fg="green")


@dev.command("gateway-ping")
def gateway_ping(
    prompt: str = typer.Argument("2+2. Answer with just the number.", help="What to ask."),
    model_class: str = typer.Option("fast-executor", "--class", help="A model class, e.g. fast-executor."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Ask a model class a question; prints the answer, tokens and cost. The catalog picks the model."""
    result = asyncio.run(_ping(model_class, prompt))
    if json_out:
        typer.echo(jsonlib.dumps(result, indent=2, default=str))
        raise typer.Exit(0 if result.get("ok") else 1)

    if not result.get("ok"):
        typer.secho(f"error: {result.get('error')}", fg="red", err=True)
        raise typer.Exit(1)

    typer.echo(f"  class    {model_class}")
    typer.echo(f"  served by {result['provider']}:{result['model']}  (family {result['family']})")
    typer.echo(f"  answer   {result['answer']!r}")
    typer.echo(f"  units    {result['units']}")
    typer.secho(f"  cost_usd {result['cost_usd']}", fg="green")


@dev.command("media-ping")
def media_ping(
    prompt: str = typer.Argument("a small red square on a white background", help="What to depict."),
    ref: str = typer.Option(
        "", "--ref", help="Pin one image model (provider:model_id); default = chain order."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Generate ONE real image through the media lane. Prints who served, the bytes,
    and the **cost** - and fails loudly if no UsageEvent was emitted (the gateway-ping law,
    applied to media). The image lands in a temp file so you can open it."""
    result = asyncio.run(_media_ping(prompt, ref or None))
    if json_out:
        typer.echo(jsonlib.dumps(result, indent=2, default=str))
        raise typer.Exit(0 if result.get("ok") else 1)

    if not result.get("ok"):
        typer.secho(f"error: {result.get('error')}", fg="red", err=True)
        raise typer.Exit(1)

    typer.echo(f"  served by {result['provider']}:{result['model']}")
    typer.echo(f"  bytes    {result['bytes']}  → {result['path']}")
    typer.echo(f"  units    {result['units']}")
    typer.secho(f"  cost_usd {result['cost_usd']}", fg="green")


async def _media_ping(prompt: str, ref: str | None) -> dict[str, object]:
    import tempfile
    from pathlib import Path

    from prismoid.ai.gateway.meter import MeterContext, units_emitter
    from prismoid.ai.gateway.providers import api_key_for
    from prismoid.ai.gateway.resolve import chain_for
    from prismoid.ai.tools.providers.images import ImageCandidate, wire_for
    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops
    from prismoid.operations import routing as routing_ops
    from prismoid.schemas.model_class import ModelClass
    from prismoid.schemas.usage import UsageRecord

    recorded: list[UsageRecord] = []

    async def emit(record: UsageRecord) -> None:
        recorded.append(record)

    async with session_scope() as session:
        catalog = await ops.catalog(session, enabled_only=True)
        order = await routing_ops.order_for(session)

    specs = chain_for(ModelClass.IMAGE_GEN, catalog, order=order.get(ModelClass.IMAGE_GEN.value))
    if ref is not None:
        specs = [s for s in specs if s.ref == ref]
    if not specs:
        return {"ok": False, "error": "no enabled image-gen model. `prismoid models list` - enable one."}

    spec = specs[0]
    candidate = ImageCandidate(
        provider=spec.provider,
        model_id=spec.model_id,
        protocol=str(spec.protocol),
        base_url=spec.base_url,
        api_key=api_key_for(spec.provider),
        emit_units=units_emitter(spec, emit, MeterContext()),
    )
    wire = wire_for(candidate.protocol)
    if wire is None:
        return {"ok": False, "error": f"{spec.ref} declares unknown media protocol {spec.protocol!r}"}

    try:
        images = await wire(candidate, prompt, "square", 1)
    except Exception as exc:
        return {"ok": False, "error": f"{spec.ref}: {type(exc).__name__}: {exc}"}
    if not images:
        return {"ok": False, "error": f"{spec.ref} returned no images"}
    await candidate.emit_units({"image": len(images), "request": 1})

    if not recorded:
        return {"ok": False, "error": "the image arrived but emitted NO UsageEvent (Rule 8)"}

    target = Path(tempfile.mkdtemp(prefix="media-ping-")) / "ping.png"
    target.write_bytes(images[0])
    usage = recorded[-1]
    return {
        "ok": True,
        "provider": usage.provider,
        "model": usage.model,
        "bytes": len(images[0]),
        "path": str(target),
        "units": {str(u): n for u, n in usage.units.items()},
        "cost_usd": str(usage.cost_usd),
    }


async def _ping(model_class: str, prompt: str) -> dict[str, object]:
    from pydantic_ai import Agent

    from prismoid.ai.gateway.meter import MeterContext
    from prismoid.ai.gateway.resolve import resolve
    from prismoid.db.session import session_scope
    from prismoid.operations import models as ops
    from prismoid.schemas.model_class import ModelClass
    from prismoid.schemas.usage import UsageRecord

    recorded: list[UsageRecord] = []

    async def emit(record: UsageRecord) -> None:
        recorded.append(record)

    from prismoid.operations import routing as routing_ops

    async with session_scope() as session:
        catalog = await ops.catalog(session, enabled_only=True)
        order = await routing_ops.order_for(session)  # the routing table; {} ⇒ alphabetical

    if not catalog:
        return {"ok": False, "error": "no enabled models. `prismoid models list` - then enable one."}

    try:
        model = resolve(
            ModelClass(model_class), catalog, emit_usage=emit, context=MeterContext(), order=order
        )
        agent = Agent(model)
        run = await agent.run(prompt)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if not recorded:
        # Rule 8 is not advice. A provider call with no UsageEvent is a hole in the ledger, and
        # the instrument that would have shown you the hole must not paper over it.
        return {"ok": False, "error": "the model answered but emitted NO UsageEvent (Rule 8)"}

    usage = recorded[-1]
    return {
        "ok": True,
        "provider": usage.provider,
        "model": usage.model,
        "family": str(usage.family),
        "answer": str(run.output).strip()[:200],
        "units": {str(u): n for u, n in usage.units.items()},
        "cost_usd": str(usage.cost_usd),
    }
